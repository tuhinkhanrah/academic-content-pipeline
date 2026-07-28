#!/usr/bin/env python3
"""
chapter2moodle_agent.py - Flexible Synthetic Question Generator from Chapter Documents (.pdf and .md).

Settings Precedence:
  Explicit CLI Argument  -->  JSON Config File  -->  Built-in Default
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import fitz  # PyMuPDF
from google import genai

from moodle_utils import (
    encode_bytes_to_base64,
    extract_clean_question_nodes_with_status,
    load_file_content,
    setup_logger,
)

logger = logging.getLogger("moodle_system")


def crop_diagram_from_page(
    page: fitz.Page,
    ymin: int,
    xmin: int,
    ymax: int,
    xmax: int,
    dpi: int = 150,
    padding_cm: float = 0.5,
) -> bytes:
    rect = page.rect
    page_width, page_height = rect.width, rect.height

    x0 = (xmin / 1000.0) * page_width
    y0 = (ymin / 1000.0) * page_height
    x1 = (xmax / 1000.0) * page_width
    y1 = (ymax / 1000.0) * page_height

    padding_pts = padding_cm * 28.3465
    x0 = max(0, x0 - padding_pts)
    y0 = max(0, y0 - padding_pts)
    x1 = min(page_width, x1 + padding_pts)
    y1 = min(page_height, y1 + padding_pts)

    if x0 >= x1 or y0 >= y1:
        return b""

    crop_rect = fitz.Rect(x0, y0, x1, y1).intersect(rect)
    if crop_rect.is_empty:
        return b""

    pix = page.get_pixmap(clip=crop_rect, dpi=dpi)
    return pix.tobytes("png")


class ManagedAgentChapterGenerator:
    def __init__(
        self,
        client: genai.Client,
        prompt_text: str,
        standards: List[str],
        tags: List[str],
        difficulty: str = "Medium",
        num_questions: int = 2,
        default_grade: float = 4.0,
        penalty: float = 0.25,
        negative_fraction: int = -25,
        agent_name: str = "antigravity-preview-05-2026",
        agent_type: str = "antigravity",
        model_name: str = "gemini-3.6-flash",
        dpi: int = 150,
        rate_limit_delay: float = 45.0,
        retry_base_delay: float = 4.0,
        attempt_limit: int = 10,
        context_reset_interval: int = 7,
        padding_cm: float = 0.5,
        page_range: Optional[Tuple[int, int]] = None,
        verbose: bool = False,
    ):
        self.client = client
        self.prompt_text = prompt_text
        self.standards = standards
        self.tags = tags
        self.difficulty = difficulty
        self.num_questions = num_questions
        self.default_grade = default_grade
        self.penalty = penalty
        self.negative_fraction = negative_fraction
        self.agent_name = agent_name
        self.agent_type = agent_type
        self.model_name = model_name
        self.dpi = dpi
        self.rate_limit_delay = rate_limit_delay
        self.retry_base_delay = retry_base_delay
        self.attempt_limit = attempt_limit
        self.context_reset_interval = context_reset_interval
        self.padding_cm = padding_cm
        self.page_range = page_range
        self.verbose = verbose

    def process_chapter_file(self, chapter_path: Path, output_dir: Path) -> bool:
        if chapter_path.suffix.lower() == ".pdf":
            return self._process_pdf_chapter(chapter_path, output_dir)
        else:
            return self._process_markdown_chapter(chapter_path, output_dir)

    def _process_markdown_chapter(self, md_path: Path, output_dir: Path) -> bool:
        content_text = load_file_content(md_path)
        if not content_text:
            logger.error(f"Empty or missing chapter file: {md_path}")
            return False

        logger.info(f"📖 Generating Moodle questions from Markdown: {md_path.name}")
        file_stem = md_path.stem
        work_dir = output_dir / file_stem
        xml_output_path = work_dir / f"{file_stem}_generated_bank.xml"
        work_dir.mkdir(parents=True, exist_ok=True)

        turn_prompt = self._build_turn_prompt(content_text=content_text)
        multimodal_input = [{"type": "text", "text": turn_prompt}]

        interaction, questions = self._send_and_validate_with_retry(
            multimodal_input=multimodal_input,
            label=md_path.name,
        )

        if not questions:
            logger.error(f"❌ Failed to generate valid questions for {md_path.name}")
            return False

        final_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<quiz>\n"
            f"{chr(10).join(questions)}\n"
            "</quiz>"
        )

        xml_output_path.write_text(final_xml, encoding="utf-8")
        logger.info(f"✅ Generated {len(questions)} total questions -> {xml_output_path}")
        time.sleep(self.rate_limit_delay)
        return True

    def _process_pdf_chapter(self, pdf_path: Path, output_dir: Path) -> bool:
        pdf_stem = pdf_path.stem
        pdf_work_dir = output_dir / pdf_stem
        image_output_dir = pdf_work_dir / "extracted_assets"
        xml_output_path = pdf_work_dir / f"{pdf_stem}_generated_bank.xml"

        logger.info(f"🚀 Processing chapter PDF: {pdf_path.name}")
        image_output_dir.mkdir(parents=True, exist_ok=True)

        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            logger.error(f"Failed to open PDF {pdf_path}: {e}")
            return False

        total_pdf_pages = len(doc)
        start_page = 1
        end_page = total_pdf_pages

        if self.page_range:
            start_page = max(1, self.page_range[0])
            end_page = min(total_pdf_pages, self.page_range[1])

        logger.info(f"[{pdf_stem}] Target page range: {start_page} to {end_page}")

        all_questions_xml: List[str] = []
        environment_id: Optional[str] = None
        last_interaction_id: Optional[str] = None

        processed_count = 0
        for page_idx in range(start_page, end_page + 1):
            page_num_zero_based = page_idx - 1
            processed_count += 1

            logger.info(f"[{pdf_stem}] Processing Page {page_idx}/{total_pdf_pages}...")
            page = doc[page_num_zero_based]
            pix = page.get_pixmap(dpi=self.dpi)
            b64_image = encode_bytes_to_base64(pix.tobytes("png"))
            page_text = page.get_text("text").strip()

            if processed_count > 1 and (processed_count - 1) % self.context_reset_interval == 0:
                logger.info(f"[{pdf_stem}] Pruning turn history to keep session lean...")
                last_interaction_id = None

            turn_prompt = self._build_turn_prompt(
                content_text=f"=== PAGE {page_idx} TEXT ===\n{page_text}",
                current_page_num=page_idx,
            )

            multimodal_input = [
                {"type": "image", "data": b64_image, "mime_type": "image/png"},
                {"type": "text", "text": turn_prompt},
            ]

            interaction, extracted_questions = self._send_and_validate_with_retry(
                multimodal_input=multimodal_input,
                label=f"{pdf_stem} (p.{page_idx})",
                page_num=page_idx,
                environment_id=environment_id,
                previous_interaction_id=last_interaction_id,
            )

            if not interaction or extracted_questions is None:
                logger.warning(f"[{pdf_stem}] Page {page_idx}: Skipped due to unresolvable errors.")
                continue

            if self.verbose:
                self._log_interaction_steps(interaction, page_idx)

            environment_id = interaction.environment_id
            last_interaction_id = interaction.id

            if not extracted_questions:
                logger.info(f"[{pdf_stem}] Page {page_idx}: No questions generated.")
                continue

            processed_page_questions = []
            for question_xml in extracted_questions:
                processed_xml = self._process_diagram_tokens_in_question(
                    page=page, question_xml=question_xml, image_dir=image_output_dir
                )
                processed_page_questions.append(processed_xml)

            all_questions_xml.extend(processed_page_questions)
            logger.info(f"[{pdf_stem}] Page {page_idx}: Generated {len(processed_page_questions)} question(s).")

            time.sleep(self.rate_limit_delay)

        doc.close()

        if not all_questions_xml:
            logger.error(f"[{pdf_stem}] Generation failed across target pages.")
            return False

        final_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<quiz>\n"
            f"{chr(10).join(all_questions_xml)}\n"
            "</quiz>"
        )

        xml_output_path.write_text(final_xml, encoding="utf-8")
        logger.info(f"✅ Extracted {len(all_questions_xml)} total questions -> {xml_output_path}")
        return True

    def _build_turn_prompt(self, content_text: str, current_page_num: Optional[int] = None) -> str:
        formatted_standards = ", ".join(self.standards) if self.standards else "General"
        all_tags = set(self.tags)
        all_tags.add(f"difficulty:{self.difficulty.lower()}")
        tags_block = "\n".join([f"      <tag><text>{t}</text></tag>" for t in all_tags])

        page_marker = f"=== PAGE {current_page_num} ===\n" if current_page_num else ""

        return (
            f"{page_marker}"
            f"=== CHAPTER SYLLABUS CONTENT ===\n"
            f"{content_text}\n\n"
            f"=== GENERATION CONSTRAINTS ===\n"
            f"- Target Exam: {formatted_standards}\n"
            f"- Target Difficulty Level: {self.difficulty}\n"
            f"- Number of Questions to Generate: {self.num_questions}\n"
            f"- Question Volume Strategy: Dynamically generate between 1 and {self.num_questions} high-quality questions based on the depth of concepts present on this page.\n"
            f"- Default Grade (<defaultgrade>): {self.default_grade}\n"
            f"- Question Penalty (<penalty>): {self.penalty}\n"
            f"- Incorrect Choice Fraction: {self.negative_fraction}%\n"
            f"- Standards: {formatted_standards}\n"
            f"- Global Tags:\n<tags>\n{tags_block}\n</tags>\n\n"
            f'- Generate questions covering key topics above in valid Moodle XML format. Output ONLY valid <question> nodes.'
        )

    def _log_interaction_steps(self, interaction, page_num: Optional[int] = None) -> None:
        if not hasattr(interaction, "steps") or not interaction.steps:
            return

        label = f"Page {page_num}" if page_num is not None else "Document"
        for step in interaction.steps:
            step_type = getattr(step, "type", "")
            step_str = str(step)

            if "thought" in step_type or "thought" in step_str.lower():
                summary = getattr(step, "summary", None)
                if summary:
                    logger.info(f"  🧠 [{label} Thought]: {summary}")

            if "search" in step_type.lower() or "google_search" in step_str.lower():
                logger.info(f"  🔍 [{label} Search]: Google Search tool invoked.")

            if "code" in step_type.lower() or "code_execution" in step_str.lower():
                logger.info(f"  🧮 [{label} Code]: Python Code Execution tool invoked.")

    def _send_and_validate_with_retry(
        self,
        multimodal_input: List[dict],
        label: str,
        page_num: Optional[int] = None,
        environment_id: Optional[str] = None,
        previous_interaction_id: Optional[str] = None,
    ) -> Tuple[Optional[object], Optional[List[str]]]:
        formatted_standards = ", ".join(self.standards) if self.standards else "General"
        formatted_system_instruction = (
            self.prompt_text
            .replace("{{TARGET_EXAM}}", formatted_standards)
            .replace("{{TARGET_DIFFICULTY}}", self.difficulty)
        )

        agent_config_payload = {
            "type": self.agent_type,
            "model": self.model_name,
        }

        for attempt in range(1, self.attempt_limit + 1):
            try:
                env_param = environment_id if environment_id else "remote"
                kwargs = {
                    "agent": self.agent_name,
                    "agent_config": agent_config_payload,
                    "environment": env_param,
                    "system_instruction": formatted_system_instruction,
                    "input": multimodal_input,
                }
                if previous_interaction_id:
                    kwargs["previous_interaction_id"] = previous_interaction_id

                interaction = self.client.interactions.create(**kwargs)

                if not interaction or not getattr(interaction, "output_text", None):
                    time.sleep(self.retry_base_delay)
                    continue

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
                else:
                    logger.error(f"❌ API Error on [{label}]: {e}")
                    break

        return None, None

    def _process_diagram_tokens_in_question(
        self, page: fitz.Page, question_xml: str, image_dir: Path
    ) -> str:
        block_tags = "questiontext|generalfeedback|correctfeedback|partiallycorrectfeedback|incorrectfeedback|answer"
        block_pattern = rf"(<({block_tags})\b[^>]*>.*?)(</\2>)"

        def block_replacer(match):
            prefix = match.group(1)
            suffix = match.group(3)

            crop_tokens = re.findall(r"\[CROP_BOX:(\d+),(\d+),(\d+),(\d+)\]", prefix)
            if not crop_tokens:
                return match.group(0)

            file_nodes_to_inject = []
            for token in list(set(crop_tokens)):
                ymin, xmin, ymax, xmax = map(int, token)
                cropped_bytes = crop_diagram_from_page(
                    page, ymin, xmin, ymax, xmax, dpi=self.dpi, padding_cm=self.padding_cm
                )

                if not cropped_bytes:
                    continue

                filename = f"diagram_{uuid.uuid4().hex[:8]}.png"
                file_path = image_dir / filename
                file_path.write_bytes(cropped_bytes)

                b64_str = encode_bytes_to_base64(cropped_bytes)
                file_nodes_to_inject.append(
                    f'    <file name="{filename}" path="/" encoding="base64">{b64_str}</file>'
                )

                raw_token_str = f"[CROP_BOX:{ymin},{xmin},{ymax},{xmax}]"
                moodle_img_tag = (
                    f'<p><img src="@@PLUGINFILE@@/{filename}" alt="Question Diagram" '
                    f'class="img-responsive" style="max-width: 100%; height: auto;" /></p>'
                )
                prefix = prefix.replace(raw_token_str, moodle_img_tag)

            if file_nodes_to_inject:
                return prefix + "\n" + "\n".join(file_nodes_to_inject) + "\n" + suffix
            return prefix + suffix

        return re.sub(block_pattern, block_replacer, question_xml, flags=re.IGNORECASE | re.DOTALL)


def parse_args_and_config() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="chapter2moodle_agent.py - Synthetic Question Generator from Chapter Files",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # File & Directory CLI Arguments
    parser.add_argument("-c", "--config-file", type=Path, help="Path to JSON config file.")
    parser.add_argument("-i", "--input-dir", type=Path, help="Input directory containing chapter markdown/pdf files.")
    parser.add_argument("-o", "--output-dir", type=Path, help="Output directory for generated Moodle XML.")
    parser.add_argument("-p", "--prompt", type=Path, help="Path to system prompt markdown file.")
    parser.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"), help="Process specific page range (e.g. 10 15).")
    parser.add_argument("-s", "--standards", type=str, help="Comma-separated standards (e.g. NEET, JEE-Main, WBJEE).")
    parser.add_argument("-t", "--tags", type=str, help="Comma-separated global tags.")

    # Question Synthesis Constraints
    parser.add_argument("--difficulty", type=str, choices=["Easy", "Medium", "Hard"], help="Target difficulty level.")
    parser.add_argument("-n", "--num-questions", type=int, help="Number of questions to generate per page/section.")
    parser.add_argument("--default-grade", type=float, help="Default grade for generated questions.")
    parser.add_argument("--penalty", type=float, help="Penalty fraction for multiple attempts.")
    parser.add_argument("--negative-fraction", type=int, help="Negative percentage for wrong answers (e.g., -25).")

    # Engine & Agent Configuration CLI Arguments
    parser.add_argument("-a", "--agent-name", type=str, help="Managed agent resource name.")
    parser.add_argument("--agent-type", type=str, help="Agent configuration type.")
    parser.add_argument("-m", "--model-name", type=str, help="Underlying LLM model name.")

    # Timing & Performance CLI Arguments
    parser.add_argument("--padding-cm", type=float, help="Diagram crop padding in centimeters.")
    parser.add_argument("--rate-limit-delay", type=float, help="Inter-request delay in seconds between pages.")
    parser.add_argument("--retry-base-delay", type=float, help="Base delay in seconds for API error retries.")
    parser.add_argument("--attempt-limit", type=int, help="Maximum retry attempts per page.")
    parser.add_argument("--context-reset-interval", type=int, help="Number of pages before resetting history.")
    parser.add_argument("--dpi", type=int, help="Page rendering DPI for vision processing.")

    # Logging & Auth
    parser.add_argument("--log-file", type=Path, help="Path to write rotated log file.")
    parser.add_argument("--api-key", type=str, default=os.getenv("GEMINI_API_KEY"))
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging.")

    args = parser.parse_args()

    # Load Config JSON if provided
    config_data = {}
    if args.config_file and args.config_file.exists():
        try:
            with open(args.config_file, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception as e:
            print(f"Error reading config file {args.config_file}: {e}", file=sys.stderr)

    agent_cfg = config_data.get("agent_config", {})
    resolved = argparse.Namespace()

    # PRECEDENCE RESOLUTION RULE: Explicit CLI Flag -> JSON Config File -> Default Fallback
    resolved.input_dir = args.input_dir or Path(config_data.get("input_dir", "./chapters"))
    resolved.output_dir = args.output_dir or Path(config_data.get("output_dir", "./output"))
    resolved.prompt = args.prompt or Path(config_data.get("prompt", "./prompts/prompt_chapter_generation.md"))

    page_range_val = args.page_range or config_data.get("page_range")
    resolved.page_range = tuple(page_range_val) if page_range_val else None

    resolved.standards = args.standards or config_data.get("standards", "JEE-Main")
    resolved.tags = args.tags or config_data.get("tags", "")

    resolved.difficulty = args.difficulty or config_data.get("difficulty", "Medium")
    resolved.num_questions = args.num_questions if args.num_questions is not None else config_data.get("num_questions", 2)
    resolved.default_grade = args.default_grade if args.default_grade is not None else config_data.get("default_grade", 4.0)
    resolved.penalty = args.penalty if args.penalty is not None else config_data.get("penalty", 0.25)
    resolved.negative_fraction = args.negative_fraction if args.negative_fraction is not None else config_data.get("negative_fraction", -25)

    resolved.agent_name = args.agent_name or config_data.get("agent_name", "antigravity-preview-05-2026")
    resolved.agent_type = args.agent_type or agent_cfg.get("type", "antigravity")
    resolved.model_name = args.model_name or agent_cfg.get("model", "gemini-3.6-flash")

    resolved.padding_cm = args.padding_cm if args.padding_cm is not None else config_data.get("padding_cm", 0.5)
    resolved.rate_limit_delay = args.rate_limit_delay if args.rate_limit_delay is not None else config_data.get("rate_limit_delay", 45.0)
    resolved.retry_base_delay = args.retry_base_delay if args.retry_base_delay is not None else config_data.get("retry_base_delay", 4.0)
    resolved.attempt_limit = args.attempt_limit if args.attempt_limit is not None else config_data.get("attempt_limit", 10)
    resolved.context_reset_interval = args.context_reset_interval if args.context_reset_interval is not None else config_data.get("context_reset_interval", 7)
    resolved.dpi = args.dpi if args.dpi is not None else config_data.get("dpi", 150)

    log_file_val = args.log_file or config_data.get("log_file")
    resolved.log_file = Path(log_file_val) if log_file_val else (resolved.output_dir / "chapter2moodle.log")

    resolved.api_key = args.api_key or config_data.get("api_key")
    resolved.verbose = args.verbose or config_data.get("verbose", True)

    return resolved


def main() -> None:
    args = parse_args_and_config()
    setup_logger(log_file=args.log_file, verbose=args.verbose)

    if not args.api_key:
        logger.critical("GEMINI_API_KEY is missing. Set GEMINI_API_KEY env var or pass --api-key.")
        sys.exit(1)

    client = genai.Client(api_key=args.api_key)
    prompt_text = load_file_content(args.prompt)

    generator = ManagedAgentChapterGenerator(
        client=client,
        prompt_text=prompt_text,
        standards=[s.strip() for s in args.standards.split(",") if s.strip()],
        tags=[t.strip() for t in args.tags.split(",") if t.strip()],
        difficulty=args.difficulty,
        num_questions=args.num_questions,
        default_grade=args.default_grade,
        penalty=args.penalty,
        negative_fraction=args.negative_fraction,
        agent_name=args.agent_name,
        agent_type=args.agent_type,
        model_name=args.model_name,
        dpi=args.dpi,
        rate_limit_delay=args.rate_limit_delay,
        retry_base_delay=args.retry_base_delay,
        attempt_limit=args.attempt_limit,
        context_reset_interval=args.context_reset_interval,
        padding_cm=args.padding_cm,
        page_range=args.page_range,
        verbose=args.verbose,
    )

    chapter_files = sorted([
        f for f in args.input_dir.iterdir()
        if f.suffix.lower() in [".md", ".pdf"] and not f.name.startswith(".")
    ])

    if not chapter_files:
        logger.warning(f"No .pdf or .md files found in {args.input_dir}")
        sys.exit(1)

    overall_success = True
    for chapter_path in chapter_files:
        success = generator.process_chapter_file(chapter_path, args.output_dir)
        if not success:
            overall_success = False

    # Exit with code 1 if any chapter file failed to generate questions
    if not overall_success:
        logger.error("❌ One or more files failed during question generation.")
        sys.exit(1)


if __name__ == "__main__":
    main()