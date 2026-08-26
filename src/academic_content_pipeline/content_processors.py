#!/usr/bin/env python3
"""
content_processors.py - Core Content Processing Classes.

Classes:
  1. QuestionPaperExtractor : Extracts questions & diagrams from exam paper PDFs.
  2. QuestionGenerator      : Synthesizes calibrated questions from source PDFs/MDs.
  3. PaperGenerator         : Synthesizes full mock exam papers from syllabi & specs.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from PIL import Image

try:
    from .ai_communicators import BaseAICommunicator, RemoteSandboxBackend
    from .mistral_ocr import MistralOCREngine
    from .output_renderers import OutputRenderer
    from .pipeline_utils import (
        assemble_prompt_files,
        build_language_instructions,
        extract_clean_question_nodes_with_status,
        load_file_content,
        fix_and_inject_moodle_xml,
        unique_image_items,
        write_prompt_snapshot,
    )
except ImportError:  # pragma: no cover - fallback for direct script execution
    from ai_communicators import BaseAICommunicator, RemoteSandboxBackend
    from mistral_ocr import MistralOCREngine
    from output_renderers import OutputRenderer
    from pipeline_utils import (
        assemble_prompt_files,
        build_language_instructions,
        extract_clean_question_nodes_with_status,
        load_file_content,
        fix_and_inject_moodle_xml,
        unique_image_items,
        write_prompt_snapshot,
    )

logger = logging.getLogger("academic_content_pipeline")


def build_paper_metadata(
    spec_data: Dict[str, Any],
    subjects: List[Dict[str, Any]],
    duration_override: Optional[int],
) -> Dict[str, Any]:
    """Validate paper metadata and return the normalized prompt contract."""
    duration_minutes = duration_override
    if duration_minutes is None:
        duration_minutes = spec_data.get("duration_minutes")
    if duration_minutes is not None and duration_minutes <= 0:
        raise ValueError("duration_minutes must be greater than zero")

    if not subjects and spec_data.get("total_questions") is not None:
        subjects = [
            {
                "name": spec_data.get("paper_title", "Question Set"),
                "total_questions": spec_data["total_questions"],
                "marks_per_question": spec_data.get("marks_per_question", 1),
            }
        ]

    section_totals = []
    calculated_total_marks = 0.0
    sections = spec_data.get("sections", [])
    declared_total_marks = spec_data.get("total_marks")
    if sections:
        subject_count = len(subjects)
        for section in sections:
            scoring = section.get("scoring", {})
            question_count = int(section.get("question_count", section.get("total_questions", 0)))
            marks_per_question = float(scoring.get("correct", section.get("marks_per_question", 0)))
            multiplier = subject_count if section.get("subject", "all") == "all" else 1
            section_marks = question_count * marks_per_question * multiplier
            calculated_total_marks += section_marks
            section_totals.append(
                {
                    "name": section.get("name", section.get("id", "Section")),
                    "questions": question_count * multiplier,
                    "marks_per_question": marks_per_question,
                    "section_marks": section_marks,
                    "negative_marks": scoring.get("incorrect"),
                }
            )
    else:
        for subject in subjects:
            question_count = int(subject.get("total_questions", 0))
            marks_per_question = float(
                subject.get("marks_per_question", subject.get("default_grade", 0))
            )
            section_marks = question_count * marks_per_question
            calculated_total_marks += section_marks
            section_totals.append(
                {
                    "name": subject.get("name", "Subject"),
                    "questions": question_count,
                    "marks_per_question": marks_per_question,
                    "section_marks": section_marks,
                    "negative_marks": subject.get("negative_marks", subject.get("penalty")),
                }
            )
    if declared_total_marks is not None and abs(float(declared_total_marks) - calculated_total_marks) > 1e-9:
        raise ValueError(
            f"Spec total_marks ({declared_total_marks}) does not match calculated total "
            f"({calculated_total_marks:g})"
        )

    return {
        "duration_minutes": duration_minutes,
        "total_marks": declared_total_marks if declared_total_marks is not None else calculated_total_marks,
        "instructions": [
            str(item)
            for item in spec_data.get(
                "paper_instructions", spec_data.get("instructions", [])
            )
        ],
        "header_fields": spec_data.get(
            "header_fields",
            {"candidate_name": "", "roll_number": "", "date": ""},
        ),
        "section_totals": section_totals,
    }


def build_question_type_contract(
    spec_data: Dict[str, Any],
    registry_path: Path,
    max_questions: int,
) -> str:
    """Resolve question-type configuration into an explicit AI contract."""
    registry = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    entries = registry.get("mcq_types", [])
    by_key = {entry["id"]: entry for entry in entries}
    by_key.update({entry["name"]: entry for entry in entries})
    config = spec_data.get("question_types")
    if config is None:
        if spec_data.get("sections"):
            allocations: Dict[str, int] = {}
            for section in spec_data["sections"]:
                multiplier = max_questions // sum(
                    int(item.get("question_count", item.get("total_questions", 0)))
                    for item in spec_data["sections"]
                ) if section.get("subject", "all") == "all" else 1
                for item in section.get("question_types", []):
                    type_key = item.get("type") if isinstance(item, dict) else item
                    allocations[type_key] = allocations.get(type_key, 0) + int(item.get("count", 0)) * multiplier
            config = {
                "mode": "exact",
                "types": [{"type": key, "count": count} for key, count in allocations.items()],
            }
        else:
            config = {"mode": "automatic", "allowed": [entry["name"] for entry in entries]}
    elif isinstance(config, list):
        config = {"mode": "exact", "types": config}

    forbidden = set(config.get("forbidden", []))
    requested = config.get("types", config.get("allowed", []))
    if isinstance(requested, dict):
        requested = list(requested)
    if not requested:
        requested = [entry["name"] for entry in entries]

    resolved = []
    for item in requested:
        type_key = item.get("type") if isinstance(item, dict) else item
        if type_key in forbidden:
            continue
        if type_key not in by_key:
            raise ValueError(f"Unknown MCQ type: {type_key}")
        entry = by_key[type_key]
        if entry["name"] in forbidden or entry["id"] in forbidden:
            continue
        resolved.append((entry, item if isinstance(item, dict) else {}))

    mode = config.get("mode", "automatic").lower()
    if mode not in {"exact", "weighted", "automatic"}:
        raise ValueError(f"Unsupported question_types mode: {mode}")

    if mode == "exact":
        total = sum(int(item.get("count", 0)) for _, item in resolved)
        if total != max_questions:
            raise ValueError(
                f"Question type counts ({total}) must equal total_questions ({max_questions})"
            )
    if mode == "weighted":
        weights = [float(item.get("weight", 0)) for _, item in resolved]
        if not weights or abs(sum(weights) - 1.0) > 1e-9:
            raise ValueError("Question type weights must sum to 1.0")

    lines = [
        "### QUESTION TYPE CONTRACT",
        f"Generate at most {max_questions} questions using only the configured types.",
        f"Allocation mode: {mode}",
    ]
    for entry, allocation in resolved:
        line = f"- {entry.get('display_name', entry['name'])} ({entry['name']}): {entry['description']}"
        constraints = entry.get("constraints", {})
        if constraints:
            line += f" Constraints: {json.dumps(constraints, sort_keys=True)}."
        if mode == "exact":
            line += f" Requested count: {int(allocation.get('count', 0))}."
        elif mode == "weighted":
            line += f" Weight: {float(allocation.get('weight', 0)):g}."
        lines.append(line)

    minimum = config.get("minimum", {})
    if minimum:
        lines.append(f"Minimum counts: {json.dumps(minimum, sort_keys=True)}.")
    lines.extend(
        [
            "Do not force a type when the source cannot support it; report any substitution.",
            "Randomize option order independently for every MCQ and vary the correct option position across A-D.",
        ]
    )
    return "\n".join(lines)


# =======================================================================
# 1. Question Paper Extractor Class
# =======================================================================

class QuestionPaperExtractor:
    """Extracts questions from PDF exam papers using Mistral OCR and AI Communicator."""

    def __init__(
        self,
        communicator: BaseAICommunicator,
        ocr_engine: Optional[MistralOCREngine] = None,
        rules_dict: Optional[Dict[str, Any]] = None,
        languages: Optional[List[str]] = None,
        standards: str = "General",
        tags: str = "",
        page_range: Optional[List[int]] = None,
        no_instruction_page: bool = False,
        instruction_page: int = 1,
        verify_online: bool = False,
        rate_limit_delay: float = 4.0,
        staging_dir: Path = Path("extracted_data"),
    ):
        self.communicator = communicator
        self.ocr_engine = ocr_engine or MistralOCREngine()
        self.rules_dict = rules_dict or {}
        self.languages = languages or ["english"]
        self.standards = standards
        self.tags = tags
        self.page_range = page_range
        self.no_instruction_page = no_instruction_page
        self.instruction_page = instruction_page
        self.verify_online = verify_online
        self.rate_limit_delay = rate_limit_delay
        self.staging_dir = Path(staging_dir)

    def process_file(self, pdf_path: Path, output_dir: Path) -> Path:
        """Extracts questions from a single PDF and writes Moodle XML."""
        pdf_path = Path(pdf_path).resolve()
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"\n{'='*60}\n📄 [EXTRACT] Processing: {pdf_path.name}\n{'='*60}")
        img_output_dir = self.staging_dir / pdf_path.stem / "images"

        # 1. Convert PDF to Markdown & extract isolated diagrams via Mistral OCR
        ocr_result = self.ocr_engine.process_pdf(
            pdf_path=pdf_path,
            img_output_dir=img_output_dir,
            page_range=self.page_range,
        )

        # 2. Assemble system prompt rules
        system_instruction = assemble_prompt_files(
            self.rules_dict,
            mode="extract",
            output_format="xml",
            verify_online=self.verify_online,
        )

        lang_instruction, lang_tags = build_language_instructions(self.languages)
        all_tags = [t.strip() for t in self.tags.split(",") if t.strip()] + lang_tags
        system_instruction += (
            "\n\n=== EXTRACTION SESSION CONTRACT ===\n"
            f"{lang_instruction}\n"
            "Extract only complete questions that conclude on each supplied page.\n"
            f"Target Standards: {self.standards}\n"
            "Global Tags:\n"
            + "\n".join(f"  <tag><text>{tag}</text></tag>" for tag in all_tags)
            + "\nFor each page turn, output only valid Moodle XML question nodes. "
            "If no question concludes on that page, return an empty string."
        )

        all_questions_xml: List[str] = []
        is_remote_mode = isinstance(self.communicator, RemoteSandboxBackend)
        pages_to_process = ocr_result.pages if hasattr(ocr_result, "pages") and ocr_result.pages else []

        # For interactive context/agent sessions: process sequentially per page to avoid token limits
        # For remote execution mode: send entire document markdown in one single shot to GCS sandbox
        if pages_to_process and not is_remote_mode:
            logger.info(f"Processing {len(pages_to_process)} page(s) sequentially (with multi-turn context preservation)...")
            for page_data in pages_to_process:
                p_num = page_data.page_num
                if self.no_instruction_page and p_num == self.instruction_page:
                    logger.info(f"⏩ Skipping instruction page {p_num}...")
                    continue

                logger.info(f"\n--- 📄 Processing Page {p_num} ---")

                turn_prompt = (
                    f"=== PAGE {p_num} EXTRACTION ===\n"
                    f"Process only this page. Extract questions that conclude on Page {p_num}.\n"
                    f"Attached Diagram Reference IDs on this page: {list(page_data.images.keys())}\n\n"
                    f"--- PAGE {p_num} OCR MARKDOWN ---\n"
                    f"{page_data.markdown}\n\n"
                    "For diagrams, use the attached reference IDs and embed them according to the session contract."
                )

                turn_contents: List[Any] = [turn_prompt]
                for img_name, filepath in unique_image_items(page_data.images):
                    turn_contents.append(f"Diagram reference ID: {img_name}")
                    try:
                        turn_contents.append(Image.open(filepath))
                    except Exception as e:
                        logger.warning(f"Could not load image {filepath}: {e}")

                write_prompt_snapshot(
                    output_dir / f"{pdf_path.stem}_page_{p_num}_prompt.md",
                    system_instruction,
                    turn_contents,
                )
                raw_turn_output = self.communicator.generate(
                    system_instruction=system_instruction,
                    contents=turn_contents,
                    output_filename=f"page_{p_num}.xml",
                    prompt_snapshot_path=output_dir / f"{pdf_path.stem}_page_{p_num}_prompt.md",
                )

                valid_nodes, _ = extract_clean_question_nodes_with_status(raw_turn_output)
                if valid_nodes:
                    logger.info(f"  ✓ Page {p_num}: Extracted {len(valid_nodes)} question(s).")
                    all_questions_xml.extend(valid_nodes)
                else:
                    logger.info(f"  ℹ Page {p_num}: No complete questions concluded on this page.")

                if self.rate_limit_delay > 0:
                    time.sleep(self.rate_limit_delay)
        else:
            # Single-blob processing for Remote Agent Sandbox
            logger.info("Sending full document OCR Markdown in a single payload for remote sandbox execution...")
            prompt_text = (
                f"### Exam Paper OCR Markdown:\n\n{ocr_result.full_markdown}\n\n"
                f"### Extraction Parameters:\n"
                f"- Target Languages: {', '.join(self.languages)}\n"
                f"- Target Standards: {self.standards}\n"
                f"- Global Tags: {', '.join(all_tags)}\n"
                f"- Attached Diagram Reference IDs: {list(ocr_result.all_images.keys())}\n\n"
                f"{lang_instruction}\n"
                f"Extract all questions into valid Moodle XML format."
            )
            contents = [prompt_text]
            for img_name, filepath in unique_image_items(ocr_result.all_images):
                contents.append(f"Diagram reference ID: {img_name}")
                try:
                    contents.append(Image.open(filepath))
                except Exception:
                    pass

            write_prompt_snapshot(
                output_dir / f"{pdf_path.stem}_prompt.md",
                system_instruction,
                contents,
            )
            raw_output = self.communicator.generate(
                system_instruction=system_instruction,
                contents=contents,
                output_filename=f"{pdf_path.stem}_moodle.xml",
                prompt_snapshot_path=output_dir / f"{pdf_path.stem}_prompt.md",
            )
            valid_nodes, _ = extract_clean_question_nodes_with_status(raw_output)
            all_questions_xml.extend(valid_nodes)

        combined_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<quiz>\n'
            + '\n'.join(all_questions_xml) +
            '\n</quiz>'
        )

        final_xml = fix_and_inject_moodle_xml(combined_xml, ocr_result.all_images)
        output_filepath = output_dir / f"{pdf_path.stem}_moodle.xml"
        output_filepath.write_text(final_xml, encoding="utf-8")

        logger.info(f"✅ Extracted {len(all_questions_xml)} total question(s) -> {output_filepath}")
        return output_filepath

    def process_directory(self, input_dir: Path, output_dir: Path) -> List[Path]:
        """Processes all PDFs in an input directory."""
        input_dir = Path(input_dir).resolve()
        pdf_files = [
            f for f in input_dir.rglob("*.pdf")
            if not f.name.startswith("sliced_") and not f.name.startswith("temp_")
        ]
        if not pdf_files:
            logger.warning(f"No PDF files found in {input_dir}")
            return []

        results = []
        for pdf_path in pdf_files:
            out_file = self.process_file(pdf_path, output_dir)
            results.append(out_file)
        return results


# =======================================================================
# 2. Chapter Question Generator Class
# =======================================================================

class QuestionGenerator:
    """Synthesizes questions from source PDFs or Markdown files."""

    def __init__(
        self,
        communicator: BaseAICommunicator,
        ocr_engine: Optional[MistralOCREngine] = None,
        rules_dict: Optional[Dict[str, Any]] = None,
        languages: Optional[List[str]] = None,
        standards: str = "General",
        tags: str = "",
        difficulty_mix: str = "easy:0.2,medium:0.5,hard:0.3",
        num_questions: int = 5,
        output_format: str = "xml",
        pdf_engine: str = "html",
        page_range: Optional[List[int]] = None,
        staging_dir: Path = Path("extracted_data"),
        exam_duration_minutes: Optional[int] = None,
        spec_path: Optional[Path] = None,
        mcq_types_path: Path = Path("prompts/generator/mcq_types.json"),
    ):
        self.communicator = communicator
        self.ocr_engine = ocr_engine or MistralOCREngine()
        self.rules_dict = rules_dict or {}
        self.languages = languages or ["english"]
        self.standards = standards
        self.tags = tags
        self.difficulty_mix = difficulty_mix
        self.num_questions = num_questions
        self.output_format = output_format.lower()
        self.pdf_engine = pdf_engine.lower()
        self.renderer = OutputRenderer(self.output_format, self.pdf_engine)
        self.page_range = page_range
        self.staging_dir = Path(staging_dir)
        self.exam_duration_minutes = exam_duration_minutes
        self.spec_path = Path(spec_path).resolve() if spec_path else None
        self.mcq_types_path = Path(mcq_types_path)

    def process_file(self, input_file: Path, output_dir: Path) -> Path:
        """Synthesizes questions from a chapter file (.pdf or .md)."""
        input_file = Path(input_file).resolve()
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"\n{'='*60}\n📚 [GENERATE-QUESTIONS] Processing: {input_file.name} ({self.output_format.upper()})\n{'='*60}")

        paper_spec: Dict[str, Any] = {}
        if self.spec_path is not None:
            if not self.spec_path.exists():
                raise FileNotFoundError(f"Paper spec file not found: {self.spec_path}")
            if self.spec_path.suffix.lower() != ".json":
                raise ValueError("generate-questions --spec must point to a JSON paper spec")
            paper_spec = json.loads(self.spec_path.read_text(encoding="utf-8"))
            spec_subjects = paper_spec.get("subjects") or [
                {
                    "name": paper_spec.get("paper_title", input_file.stem),
                    "total_questions": paper_spec.get("total_questions", self.num_questions),
                    "marks_per_question": paper_spec.get("marks_per_question", 1),
                }
            ]
            paper_metadata = build_paper_metadata(
                paper_spec,
                spec_subjects,
                self.exam_duration_minutes,
            )
            if paper_spec.get("instruction_profile"):
                self.rules_dict["instruction_profile"] = paper_spec["instruction_profile"]
                self.rules_dict["instruction_file"] = None
        else:
            paper_metadata = None
        generation_question_count = (
            int(paper_spec.get("total_questions", self.num_questions))
            if paper_spec
            else self.num_questions
        )
        question_type_contract = build_question_type_contract(
            paper_spec,
            self.mcq_types_path,
            generation_question_count,
        )

        # 1. Read or OCR source content
        image_map: Dict[str, str] = {}
        if input_file.suffix.lower() == ".pdf":
            img_output_dir = self.staging_dir / input_file.stem / "images"
            markdown_text, image_map = self.ocr_engine.process_pdf(
                pdf_path=input_file,
                img_output_dir=img_output_dir,
                page_range=self.page_range,
            )
        else:
            markdown_text = load_file_content(input_file)

        # 2. Assemble system prompt rules
        system_instruction = assemble_prompt_files(
            self.rules_dict,
            mode="generate-questions",
            output_format=self.output_format,
            pdf_engine=self.pdf_engine,
        )

        lang_instruction, lang_tags = build_language_instructions(
            self.languages, output_format=self.output_format, pdf_engine=self.pdf_engine
        )
        all_tags = [t.strip() for t in self.tags.split(",") if t.strip()] + lang_tags

        format_instruction = self.renderer.format_instruction()

        duration_line = ""
        if paper_metadata is None and self.output_format == "pdf" and self.exam_duration_minutes is not None:
            duration_line = f"- Exam Duration: {self.exam_duration_minutes} minutes\n"

        # 3. Build turn content
        paper_header = ""
        if paper_metadata is not None and self.output_format == "pdf":
            paper_header = (
                f"- Paper Title: {paper_spec.get('paper_title', input_file.stem)}\n\n"
                f"{PaperGenerator._format_paper_metadata(paper_metadata)}\n\n"
            )
        prompt_text = (
            f"### Chapter Content Markdown:\n\n{markdown_text}\n\n"
            f"{paper_header}"
            f"{question_type_contract}\n\n"
            f"### Generation Constraints:\n"
            f"- Number of Questions: {generation_question_count}\n"
            f"- Difficulty Breakdown: {self.difficulty_mix}\n"
            f"- Target Languages: {', '.join(self.languages)}\n"
            f"- Target Standards: {self.standards}\n"
            f"- Global Tags: {', '.join(all_tags)}\n"
            f"- Output Format: {self.output_format.upper()}\n"
            f"- PDF Engine (if applicable): {self.pdf_engine.upper()}\n"
            f"{duration_line}"
            f"\n{lang_instruction}\n"
            f"{format_instruction}\n"
            f"Synthesize high quality calibrated questions based strictly on the chapter content."
        )

        contents: List[Any] = [prompt_text]
        for img_name, filepath in unique_image_items(image_map):
            contents.append(f"Diagram reference ID: {img_name}")
            try:
                contents.append(Image.open(filepath))
            except Exception:
                pass

        ext = "pdf" if self.output_format == "pdf" else "xml"
        intermediate_ext = "tex" if self.pdf_engine == "tex" else "html"
        output_artifact_name = f"{input_file.stem}_synthetic.{intermediate_ext if self.output_format == 'pdf' else 'xml'}"

        # 4. Dispatch to communicator
        write_prompt_snapshot(
            output_dir / f"{input_file.stem}_synthetic_prompt.md",
            system_instruction,
            contents,
        )
        raw_output = self.communicator.generate(
            system_instruction=system_instruction,
            contents=contents,
            output_filename=output_artifact_name,
            prompt_snapshot_path=output_dir / f"{input_file.stem}_synthetic_prompt.md",
        )

        # 5. Render the generated artifact
        if self.output_format == "pdf":
            final_pdf_path = self.renderer.render(
                raw_output,
                output_dir,
                f"{input_file.stem}_synthetic",
                image_map,
            )
            logger.info(f"✨ Compiled Synthetic PDF to: {final_pdf_path}")
            return final_pdf_path
        else:
            final_xml_path = self.renderer.render(
                raw_output,
                output_dir,
                f"{input_file.stem}_synthetic",
                image_map,
            )
            logger.info(f"✅ Saved Synthetic Moodle XML to: {final_xml_path}")
            return final_xml_path

    def process_directory(self, input_dir: Path, output_dir: Path) -> List[Path]:
        """Processes all chapter PDFs and MDs in an input directory."""
        input_dir = Path(input_dir).resolve()
        source_files = [
            f for f in input_dir.rglob("*")
            if f.suffix.lower() in [".pdf", ".md"]
            and not f.name.startswith("sliced_")
            and not f.name.startswith("temp_")
        ]
        if not source_files:
            logger.warning(f"No valid chapter files (.pdf, .md) found in {input_dir}")
            return []

        results = []
        for file_path in source_files:
            out_path = self.process_file(file_path, output_dir)
            results.append(out_path)
        return results


# =======================================================================
# 3. Syllabus & Mock Exam Question Generator Class
# =======================================================================

class PaperGenerator:
    """Synthesizes complete mock exam papers or question banks from syllabi & specs."""

    @staticmethod
    def _build_paper_metadata(
        spec_data: Dict[str, Any],
        subjects: List[Dict[str, Any]],
        duration_override: Optional[int],
    ) -> Dict[str, Any]:
        """Backward-compatible wrapper around the shared metadata builder."""
        return build_paper_metadata(spec_data, subjects, duration_override)

    @staticmethod
    def _format_paper_metadata(metadata: Dict[str, Any]) -> str:
        """Format normalized metadata as an explicit AI generation contract."""
        lines = [
            "### PAPER HEADER CONTRACT",
            "The following values are authoritative. Do not omit, change, or invent them.",
            (
                f"- Time Allowed: {metadata['duration_minutes']} minutes"
                if metadata["duration_minutes"] is not None
                else "- Time Allowed: Not specified"
            ),
            f"- Total Marks: {metadata['total_marks']:g}",
            "- Candidate Fields: " + ", ".join(metadata["header_fields"].keys()),
            "",
            "### EXAMINATION INSTRUCTIONS",
        ]
        instructions = metadata["instructions"] or [
            "Answer each question according to the instructions printed in the paper.",
            "Select the best answer for every multiple-choice question.",
        ]
        lines.extend(
            f"{index}. {instruction}"
            for index, instruction in enumerate(instructions, 1)
        )
        lines.extend(["", "### MARKING SCHEME"])
        for section in metadata["section_totals"]:
            line = (
                f"- {section['name']}: {section['questions']} questions x "
                f"{section['marks_per_question']:g} marks = {section['section_marks']:g} marks"
            )
            if section["negative_marks"] is not None:
                line += f"; negative marking: {section['negative_marks']}"
            lines.append(line)
        lines.extend(
            [
                "",
                "### PAPER HEADER OUTPUT REQUIREMENT",
                "For PDF output, render the exam name, paper title when provided, time allowed, total marks, candidate fields, and examination instructions before the first question.",
                "Keep the header visually separate from all questions, solutions, and the final answer key.",
            ]
        )
        return "\n".join(lines)

    def __init__(
        self,
        communicator: BaseAICommunicator,
        ocr_engine: Optional[MistralOCREngine] = None,
        rules_dict: Optional[Dict[str, Any]] = None,
        languages: Optional[List[str]] = None,
        standards: str = "General",
        tags: str = "",
        difficulty_mix: str = "easy:0.2,medium:0.5,hard:0.3",
        output_format: str = "xml",
        pdf_engine: str = "html",
        sample_pdf: Optional[Path] = None,
        staging_dir: Path = Path("extracted_data"),
        exam_duration_minutes: Optional[int] = None,
        mcq_types_path: Path = Path("prompts/generator/mcq_types.json"),
    ):
        self.communicator = communicator
        self.ocr_engine = ocr_engine or MistralOCREngine()
        self.rules_dict = rules_dict or {}
        self.languages = languages or ["english"]
        self.standards = standards
        self.tags = tags
        self.difficulty_mix = difficulty_mix
        self.output_format = output_format.lower()
        self.pdf_engine = pdf_engine.lower()
        self.renderer = OutputRenderer(self.output_format, self.pdf_engine)
        self.sample_pdf = sample_pdf
        self.staging_dir = Path(staging_dir)
        self.exam_duration_minutes = exam_duration_minutes
        self.mcq_types_path = Path(mcq_types_path)

    def process_spec(self, spec_path: Path, output_dir: Path) -> Path:
        """Synthesizes a full calibrated mock exam from a spec JSON or syllabus."""
        spec_path = Path(spec_path).resolve()
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        if not spec_path.exists():
            raise FileNotFoundError(f"Spec file not found: {spec_path}")

        spec_data = {}
        if spec_path.suffix.lower() == ".json":
            spec_data = json.loads(spec_path.read_text(encoding="utf-8"))
        else:
            spec_data = {
                "exam_name": spec_path.stem.upper(),
                "subjects": [{"name": spec_path.stem, "syllabus_file": str(spec_path), "total_questions": 10}],
            }

        exam_name = spec_data.get("exam_name", "MOCK_EXAM")
        subjects = spec_data.get("subjects", [])
        metadata = build_paper_metadata(
            spec_data,
            subjects,
            self.exam_duration_minutes,
        )
        if spec_data.get("instruction_profile"):
            self.rules_dict["instruction_profile"] = spec_data["instruction_profile"]
            self.rules_dict["instruction_file"] = None
        question_type_contract = build_question_type_contract(
            spec_data,
            self.mcq_types_path,
            sum(int(subject.get("total_questions", 0)) for subject in subjects),
        )

        logger.info(f"\n{'='*60}\n🎓 [GENERATE-PAPER] Exam: {exam_name} ({self.output_format.upper()})\n{'='*60}")

        # 1. Aggregate syllabi
        syllabus_blocks = []
        for subj in subjects:
            subj_name = subj.get("name", "Subject")
            total_qs = subj.get("total_questions", 0)
            s_file = Path(subj.get("syllabus_file", ""))

            content = ""
            if s_file.exists():
                if s_file.suffix.lower() == ".pdf":
                    content, _ = self.ocr_engine.process_pdf(s_file, self.staging_dir / "syllabus" / s_file.stem)
                else:
                    content = load_file_content(s_file)
                logger.info(f"  ✓ Loaded syllabus for {subj_name} ({s_file.name})")
            else:
                logger.warning(f"  ⚠️ Syllabus file '{s_file}' not found.")

            syllabus_blocks.append(
                f"### SUBJECT: {subj_name.upper()}\n"
                f"- Total Questions Required: {total_qs}\n"
                f"Syllabus Scope:\n{content}\n"
            )

        all_syllabi_text = "\n" + "=" * 40 + "\n\n".join(syllabus_blocks)

        # 2. Assemble system prompt rules
        system_instruction = assemble_prompt_files(
            self.rules_dict,
            mode="generate-paper",
            output_format=self.output_format,
            pdf_engine=self.pdf_engine,
        )

        lang_instruction, lang_tags = build_language_instructions(
            self.languages, output_format=self.output_format, pdf_engine=self.pdf_engine
        )
        all_tags = [t.strip() for t in self.tags.split(",") if t.strip()] + lang_tags

        format_instruction = self.renderer.format_instruction()

        # 3. Build turn content
        paper_header = ""
        if self.output_format == "pdf":
            paper_header = f"- Paper Title: {spec_data.get('paper_title', exam_name)}\n\n{self._format_paper_metadata(metadata)}\n\n"
        prompt_text = (
            f"### Exam Spec & Combined Syllabi Scope:\n{all_syllabi_text}\n\n"
            f"{paper_header}"
            f"{question_type_contract}\n\n"
            f"### Global Spec Constraints:\n"
            f"- Exam Name: {exam_name}\n"
            f"- Target Standards: {self.standards}\n"
            f"- Target Languages: {', '.join(self.languages)}\n"
            f"- Difficulty Breakdown: {self.difficulty_mix}\n"
            f"- Global Tags: {', '.join(all_tags)}\n"
            f"- Output Format: {self.output_format.upper()}\n"
            f"- PDF Engine: {self.pdf_engine.upper()}\n"
            f"\n{lang_instruction}\n"
            f"{format_instruction}\n"
            f"Synthesize the complete exam paper adhering strictly to the spec."
        )

        contents: List[Any] = [prompt_text]

        intermediate_ext = "tex" if self.pdf_engine == "tex" else "html"
        output_artifact_name = f"mock_{exam_name.lower()}_bank.{intermediate_ext if self.output_format == 'pdf' else 'xml'}"

        # 4. Dispatch to communicator
        write_prompt_snapshot(
            output_dir / f"mock_{exam_name.lower()}_prompt.md",
            system_instruction,
            contents,
        )
        raw_output = self.communicator.generate(
            system_instruction=system_instruction,
            contents=contents,
            output_filename=output_artifact_name,
            prompt_snapshot_path=output_dir / f"mock_{exam_name.lower()}_prompt.md",
        )

        # 5. Render the generated artifact
        if self.output_format == "pdf":
            final_pdf_path = self.renderer.render(
                raw_output,
                output_dir,
                f"mock_{exam_name.lower()}_paper",
            )
            logger.info(f"✨ Compiled Mock Exam PDF to: {final_pdf_path}")
            return final_pdf_path
        else:
            final_xml_path = self.renderer.render(
                raw_output,
                output_dir,
                f"mock_{exam_name.lower()}_bank",
            )
            logger.info(f"✅ Saved Mock Exam Moodle XML to: {final_xml_path}")
            return final_xml_path
