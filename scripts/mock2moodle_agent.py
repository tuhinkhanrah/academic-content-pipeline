#!/usr/bin/env python3
"""
mock2moodle_agent.py - Synthetic Mock Exam Paper Generator Agent.

Generates complete, calibrated mock question papers based on:
  1. Exam Blueprint JSON (Subject counts, batch sizes, syllabus file paths, difficulty breakdown)
  2. Exam Instructions & Blueprint Rules (.md or .pdf)
  3. Sample/Reference Exam Paper (.pdf) for visual pattern matching
"""

import argparse
import json
import logging
import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pymupdf
from google import genai

from moodle_utils import (
    build_language_instructions,
    encode_bytes_to_base64,
    extract_clean_question_nodes_with_status,
    load_file_content,
    setup_logger,
)

logger = logging.getLogger("moodle_system")


def extract_text_from_file_or_pdf(file_path: Optional[Path]) -> str:
    """Utility to load text from either a Markdown/Text file or a PDF document."""
    if not file_path or not file_path.exists():
        return ""

    if file_path.suffix.lower() == ".pdf":
        try:
            doc = pymupdf.open(file_path)
            extracted_text = "\n".join([page.get_text("text") for page in doc]).strip()
            doc.close()
            return extracted_text
        except Exception as e:
            logger.error(f"Failed to extract text from PDF file {file_path}: {e}")
            return ""
    else:
        return load_file_content(file_path)


class ManagedAgentMockGenerator:
    def __init__(
        self,
        client: genai.Client,
        prompt_text: str,
        standards: List[str],
        tags: List[str],
        languages: List[str],
        difficulty_mix: Dict[str, float],
        agent_name: str = "antigravity-preview-05-2026",
        agent_type: str = "antigravity",
        model_name: str = "gemini-3.6-flash",
        dpi: int = 150,
        rate_limit_delay: float = 45.0,
        retry_base_delay: float = 4.0,
        attempt_limit: int = 10,
        instruction_text: str = "",
        sample_pdf_path: Optional[Path] = None,
        verbose: bool = False,
    ):
        self.client = client
        self.prompt_text = prompt_text
        self.standards = standards
        self.tags = tags
        self.languages = languages
        self.difficulty_mix = difficulty_mix
        self.agent_name = agent_name
        self.agent_type = agent_type
        self.model_name = model_name
        self.dpi = dpi
        self.rate_limit_delay = rate_limit_delay
        self.retry_base_delay = retry_base_delay
        self.attempt_limit = attempt_limit
        self.instruction_text = instruction_text
        self.sample_pdf_path = sample_pdf_path
        self.verbose = verbose

    def _calculate_difficulty_quotas(self, total_questions: int) -> List[Tuple[str, int]]:
        """Calculates exact integer question counts per difficulty bucket to match total_questions."""
        quotas = []
        accumulated = 0
        sorted_mix = sorted(self.difficulty_mix.items(), key=lambda x: x[1], reverse=True)

        for i, (diff_name, ratio) in enumerate(sorted_mix):
            if i == len(sorted_mix) - 1:
                # Assign remainder to last bucket to ensure sum equals exact total
                count = total_questions - accumulated
            else:
                count = int(round(total_questions * ratio))
                accumulated += count
            if count > 0:
                quotas.append((diff_name.capitalize(), count))

        return quotas

    def build_full_mock_paper(self, blueprint_data: Dict, output_dir: Path) -> bool:
        """Processes an exam blueprint JSON file and generates a complete mock paper XML."""
        exam_title = blueprint_data.get("exam_name", "MOCK_EXAM")
        default_batch_size = blueprint_data.get("batch_size", 15)
        subjects = blueprint_data.get("subjects", [])

        work_dir = output_dir / f"mock_{exam_title.lower()}"
        xml_output_path = work_dir / f"mock_{exam_title.lower()}_exam_bank.xml"

        work_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"🎯 Starting Full Exam Generation: {exam_title}")

        # Attach sample reference PDF pages if provided for context
        sample_inputs = []
        sample_doc = None
        if self.sample_pdf_path and self.sample_pdf_path.exists():
            try:
                sample_doc = pymupdf.open(self.sample_pdf_path)
                logger.info(f"📄 Attached sample reference PDF: {self.sample_pdf_path.name} ({len(sample_doc)} pages)"))
                for i in range(min(4, len(sample_doc))):
                    pix = sample_doc[i].get_pixmap(dpi=self.dpi)
                    b64_img = encode_bytes_to_base64(pix.tobytes("png"))
                    sample_inputs.append({"type": "image", "data": b64_img, "mime_type": "image/png"})
            except Exception as e:
                logger.warning(f"Failed to load sample reference PDF {self.sample_pdf_path}: {e}")
        else:
            logger.info("ℹ️ No sample reference PDF attached. Generating strictly from blueprint and syllabus text.")

        all_exam_questions: List[str] = []

        # DETERMINISTIC SUBJECT & DIFFICULTY DISTRIBUTION CONTROL LOOP
        for subj in subjects:
            subj_name = subj["name"]
            total_needed = subj["total_questions"]
            grade = subj.get("default_grade", 4.0)
            penalty = subj.get("penalty", 0.25)
            neg_frac = subj.get("negative_fraction", -25)
            s_file = Path(subj.get("syllabus_file", ""))

            syllabus_content = extract_text_from_file_or_pdf(s_file)
            if not syllabus_content:
                logger.warning(f"⚠️ No syllabus content found at '{s_file}'. Skipping subject {subj_name}...")
                continue

            diff_quotas = self._calculate_difficulty_quotas(total_needed)
            logger.info(f"📌 [SUBJECT]: {subj_name} | Total Quota: {total_needed} Qs | Breakdown: {diff_quotas}")

            # DIFFICULTY BUCKET LOOP
            for diff_level, level_quota in diff_quotas:
                generated_count = 0
                batch_index = 1

                logger.info(f"  🎯 Target Mix -> Subject: {subj_name} | Level: {diff_level} | Target: {level_quota} Qs")

                while generated_count < level_quota:
                    count_to_request = min(default_batch_size, level_quota - generated_count)
                    logger.info(
                        f"    👉 Generating {subj_name} ({diff_level}) Batch {batch_index}: Requesting {count_to_request} Qs "
                        f"({generated_count}/{level_quota} completed)..."
                    )

                    turn_prompt = self._build_subject_batch_prompt(
                        subject_name=subj_name,
                        questions_to_request=count_to_request,
                        default_grade=grade,
                        penalty=penalty,
                        negative_fraction=neg_frac,
                        syllabus_text=syllabus_content,
                        difficulty_level=diff_level,
                    )

                    multimodal_input = sample_inputs + [{"type": "text", "text": turn_prompt}]

                    interaction, batch_questions = self._send_and_validate_with_retry(
                        multimodal_input=multimodal_input,
                        label=f"{subj_name}_{diff_level}_Batch_{batch_index}",
                    )

                    if not batch_questions:
                        logger.error(f"❌ Failed to receive valid questions for {subj_name} ({diff_level}) Batch {batch_index}.")
                        break

                    # Append generated XML question nodes directly
                    all_exam_questions.extend(batch_questions)
                    generated_count += len(batch_questions)
                    logger.info(f"    ✅ Batch {batch_index} complete: Received {len(batch_questions)} valid XML nodes.")

                    batch_index += 1
                    time.sleep(self.rate_limit_delay)

        if sample_doc:
            sample_doc.close()

        if not all_exam_questions:
            logger.error("❌ Failed to generate any questions for the mock exam.")
            return False

        final_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<quiz>\n"
            f"{chr(10).join(all_exam_questions)}\n"
            "</quiz>"
        )

        xml_output_path.write_text(final_xml, encoding="utf-8")
        logger.info(f"🏆 SUCCESS: Full Exam Bank Generated ({len(all_exam_questions)} total questions) -> {xml_output_path}")
        return True

    def _build_subject_batch_prompt(
        self,
        subject_name: str,
        questions_to_request: int,
        default_grade: float,
        penalty: float,
        negative_fraction: int,
        syllabus_text: str,
        difficulty_level: str,
    ) -> str:
        """Constructs the exact turn prompt passed to Gemini for each subject batch and difficulty bucket."""
        formatted_standards = ", ".join(self.standards) if self.standards else "General"
        lang_instruction, lang_tags = build_language_instructions(self.languages)

        all_tags = set(self.tags)
        all_tags.add(f"subject:{subject_name.lower()}")
        all_tags.add(f"difficulty:{difficulty_level.lower()}")
        all_tags.add("type:mock_exam")
        all_tags.update(lang_tags)

        tags_block = "\n".join([f"      <tag><text>{t}</text></tag>" for t in all_tags])

        instruction_block = ""
        if self.instruction_text:
            instruction_block = f"=== EXAM INSTRUCTIONS & BLUEPRINT ===\n{self.instruction_text}\n\n"

        sample_marker = ""
        if self.sample_pdf_path and self.sample_pdf_path.exists():
            sample_marker = (
                f"=== SAMPLE EXAM REFERENCE ===\n"
                f"The attached images show the visual pattern, complexity, and question format of actual {formatted_standards} exam papers. "
                f"Model your synthesized question structures directly after these attached sample pages.\n\n"
            )

        return (
            f"=== TARGET SUBJECT: {subject_name.upper()} ===\n"
            f"=== TURN CONSTRAINTS ===\n"
            f"- Target Exam Standard: {formatted_standards}\n"
            f"- Subject: {subject_name}\n"
            f"- Questions to Synthesize in THIS Turn: EXACTLY {questions_to_request}\n"
            f"- Target Difficulty Level: {difficulty_level.upper()}\n"
            f"- Default Grade (<defaultgrade>): {default_grade}\n"
            f"- Question Penalty (<penalty>): {penalty}\n"
            f"- Incorrect Choice Fraction: {negative_fraction}%\n\n"

            f"{instruction_block}"
            f"{sample_marker}"

            f"=== {subject_name.upper()} SYLLABUS SCOPE ===\n"
            f"{syllabus_text}\n\n"

            f"{lang_instruction}\n"

            f"=== GLOBAL TAGS ===\n"
            f"<tags>\n{tags_block}\n</tags>\n\n"

            f"Synthesize EXACTLY {questions_to_request} authentic {difficulty_level.upper()} LEVEL {subject_name} questions strictly covering the syllabus scope above. "
            f"Output ONLY valid <question> XML nodes."
        )

    def _log_interaction_steps(self, interaction) -> None:
        if not hasattr(interaction, "steps") or not interaction.steps:
            return

        for step in interaction.steps:
            step_type = getattr(step, "type", "")
            step_str = str(step)

            if "thought" in step_type or "thought" in step_str.lower():
                summary = getattr(step, "summary", None)
                if summary:
                    logger.info(f"  🧠 [Mock Thought]: {summary}")

            if "search" in step_type.lower() or "google_search" in step_str.lower():
                logger.info(f"  🔍 [Mock Search]: Google Search tool invoked.")

            if "code" in step_type.lower() or "code_execution" in step_str.lower():
                logger.info(f"  🧮 [Mock Code]: Python Code Execution tool invoked.")

    def _send_and_validate_with_retry(
        self,
        multimodal_input: List[dict],
        label: str,
    ) -> Tuple[Optional[object], Optional[List[str]]]:
        agent_config_payload = {
            "type": self.agent_type,
            "model": self.model_name,
        }

        for attempt in range(1, self.attempt_limit + 1):
            try:
                kwargs = {
                    "agent": self.agent_name,
                    "agent_config": agent_config_payload,
                    "environment": "remote",
                    "system_instruction": self.prompt_text,
                    "input": multimodal_input,
                }

                interaction = self.client.interactions.create(**kwargs)

                if not interaction or not getattr(interaction, "output_text", None):
                    time.sleep(self.retry_base_delay)
                    continue

                if self.verbose:
                    self._log_interaction_steps(interaction)

                ai_output = interaction.output_text
                extracted_questions, parse_error = extract_clean_question_nodes_with_status(ai_output)

                if parse_error:
                    logger.warning(
                        f"⚠️ [{label}] Attempt {attempt}/{self.attempt_limit}: Malformed XML -> {parse_error}. Retrying..."
                    )
                    time.sleep(self.retry_base_delay)
                    continue

                return interaction, extracted_questions

            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "503" in err_str or "Quota exceeded" in err_str:
                    match = re.search(r"retry in (\d+(?:\.\d+)?)s", err_str, re.IGNORECASE)
                    suggested_delay = float(match.group(1)) + 2.0 if match else max(self.retry_base_delay * (2 ** (attempt - 1)), 35.0)
                    logger.warning(f"⚠️ Rate/Quota Limit Hit. Retrying in {suggested_delay:.1f}s...")
                    time.sleep(suggested_delay)
                elif any(code in err_str for code in ["401", "404"]):
                    logger.error(
                        f"❌ API Error on [{label}] (Attempt {attempt}/{self.attempt_limit}): {e}. "
                        "Non-retriable auth/resource error."
                    )
                    break
                else:
                    retry_delay = max(self.retry_base_delay * (2 ** (attempt - 1)), self.retry_base_delay)
                    logger.warning(
                        f"⚠️ API Error on [{label}] (Attempt {attempt}/{self.attempt_limit}): {e}. "
                        f"Retrying same request in {retry_delay:.1f}s..."
                    )
                    time.sleep(retry_delay)
                    continue

        return None, None


def parse_difficulty_mix(mix_str: str) -> Dict[str, float]:
    """Parses a comma-separated ratio string (e.g. 'easy:0.2,medium:0.5,hard:0.3') into a normalized float dict."""
    default_mix = {"easy": 0.2, "medium": 0.5, "hard": 0.3}
    if not mix_str:
        return default_mix

    parsed = {}
    try:
        parts = mix_str.split(",")
        for part in parts:
            if ":" in part:
                k, v = part.split(":")
                parsed[k.strip().lower()] = float(v.strip())

        total = sum(parsed.values())
        if total > 0:
            return {k: v / total for k, v in parsed.items()}
    except Exception as e:
        logger.warning(f"Failed to parse difficulty mix '{mix_str}': {e}. Falling back to default mix {default_mix}")

    return default_mix


def parse_args_and_config() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="mock2moodle_agent.py - Synthetic Mock Exam Paper Generator Agent",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # Core CLI Arguments
    parser.add_argument("-b", "--blueprint", type=Path, required=True, help="Path to exam blueprint JSON file.")
    parser.add_argument("-o", "--output-dir", type=Path, help="Output directory for generated Moodle XML.")
    parser.add_argument("-p", "--prompt", type=Path, help="Path to system prompt markdown file.")
    parser.add_argument("-l", "--languages", type=str, help="Comma-separated languages (e.g. 'english', 'english,bengali').")
    parser.add_argument("--instruction-file", type=Path, help="Exam instruction & blueprint rules file (.md or .pdf).")
    parser.add_argument("--sample-pdf", type=Path, help="Sample reference exam paper PDF.")
    parser.add_argument("-s", "--standards", type=str, help="Comma-separated standards (e.g. NEET, JEE-Main).")
    parser.add_argument("-t", "--tags", type=str, help="Comma-separated global tags.")
    parser.add_argument(
        "--difficulty-mix",
        type=str,
        default="easy:0.2,medium:0.5,hard:0.3",
        help="Difficulty breakdown ratios (e.g. 'easy:0.2,medium:0.5,hard:0.3').",
    )

    # Engine & Performance CLI Arguments
    parser.add_argument("-a", "--agent-name", type=str, help="Managed agent resource name.")
    parser.add_argument("--agent-type", type=str, help="Agent configuration type.")
    parser.add_argument("-m", "--model-name", type=str, help="Underlying LLM model name.")
    parser.add_argument("--rate-limit-delay", type=float, help="Inter-request delay in seconds.")
    parser.add_argument("--retry-base-delay", type=float, help="Base delay in seconds for API error retries.")
    parser.add_argument("--attempt-limit", type=int, help="Maximum retry attempts per request.")
    parser.add_argument("--dpi", type=int, help="Page rendering DPI for vision processing.")

    # Logging & Auth
    parser.add_argument("--log-file", type=Path, help="Path to write rotated log file.")
    parser.add_argument("--api-key", type=str, default=os.getenv("GEMINI_API_KEY"))
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging.")

    args = parser.parse_args()

    resolved = argparse.Namespace()
    resolved.blueprint = args.blueprint
    resolved.output_dir = args.output_dir or Path("./output")
    resolved.prompt = args.prompt or Path("./prompts/generator/paper_generator.md")

    langs_val = args.languages or "english"
    resolved.languages = [l.strip() for l in langs_val.split(",") if l.strip()]

    resolved.instruction_file = args.instruction_file
    resolved.sample_pdf = args.sample_pdf
    resolved.standards = args.standards or "NEET"
    resolved.tags = args.tags or ""
    resolved.difficulty_mix = parse_difficulty_mix(args.difficulty_mix)

    resolved.agent_name = args.agent_name or "antigravity-preview-05-2026"
    resolved.agent_type = args.agent_type or "antigravity"
    resolved.model_name = args.model_name or "gemini-3.6-flash"

    resolved.dpi = args.dpi or 150
    resolved.rate_limit_delay = args.rate_limit_delay if args.rate_limit_delay is not None else 45.0
    resolved.retry_base_delay = args.retry_base_delay if args.retry_base_delay is not None else 4.0
    resolved.attempt_limit = args.attempt_limit if args.attempt_limit is not None else 10

    resolved.log_file = args.log_file or (resolved.output_dir / "mock2moodle.log")
    resolved.api_key = args.api_key
    resolved.verbose = args.verbose

    return resolved


def main() -> None:
    args = parse_args_and_config()
    setup_logger(log_file=args.log_file, verbose=args.verbose)

    if not args.api_key:
        logger.critical("GEMINI_API_KEY is missing. Set GEMINI_API_KEY env var or pass --api-key.")
        sys.exit(1)

    if not args.blueprint.exists():
        logger.critical(f"Blueprint JSON file not found: {args.blueprint}")
        sys.exit(1)

    try:
        with open(args.blueprint, "r", encoding="utf-8") as f:
            blueprint_data = json.load(f)
    except Exception as e:
        logger.critical(f"Failed to read blueprint JSON file {args.blueprint}: {e}")
        sys.exit(1)

    client = genai.Client(api_key=args.api_key)
    prompt_text = load_file_content(args.prompt)
    instruction_text = extract_text_from_file_or_pdf(args.instruction_file)

    generator = ManagedAgentMockGenerator(
        client=client,
        prompt_text=prompt_text,
        standards=[s.strip() for s in args.standards.split(",") if s.strip()],
        tags=[t.strip() for t in args.tags.split(",") if t.strip()],
        languages=args.languages,
        difficulty_mix=args.difficulty_mix,
        agent_name=args.agent_name,
        agent_type=args.agent_type,
        model_name=args.model_name,
        dpi=args.dpi,
        rate_limit_delay=args.rate_limit_delay,
        retry_base_delay=args.retry_base_delay,
        attempt_limit=args.attempt_limit,
        instruction_text=instruction_text,
        sample_pdf_path=args.sample_pdf,
        verbose=args.verbose,
    )

    success = generator.build_full_mock_paper(blueprint_data=blueprint_data, output_dir=args.output_dir)
    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()