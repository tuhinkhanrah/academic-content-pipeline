#!/usr/bin/env python3
"""
content_processors.py - Core Content Processing Classes.

Classes:
  1. QuestionPaperExtractor    : Extracts questions & diagrams from exam paper PDFs.
  2. ChapterQuestionGenerator  : Synthesizes calibrated questions from chapter PDFs/MDs.
  3. SyllabusQuestionGenerator : Synthesizes full mock exams or topic banks from syllabi & blueprints.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from PIL import Image

from ai_communicators import BaseAICommunicator, RemoteSandboxBackend
from mistral_ocr import MistralOCREngine
from pipeline_utils import (
    assemble_prompt_files,
    build_language_instructions,
    compile_html_to_pdf,
    compile_tex_to_pdf,
    extract_clean_question_nodes_with_status,
    fix_and_inject_moodle_xml,
    load_file_content,
)

logger = logging.getLogger("moodle_system")


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
                    f"{lang_instruction}\n"
                    f"Extract all completed questions ending on Page {p_num}.\n"
                    f"Target Standards: {self.standards}\n"
                    f"Global Tags:\n"
                    + "\n".join([f"  <tag><text>{t}</text></tag>" for t in all_tags]) + "\n\n"
                    f"Attached Diagram Reference IDs on this page: {list(page_data.images.keys())}\n\n"
                    f"--- PAGE {p_num} OCR MARKDOWN ---\n"
                    f"{page_data.markdown}\n\n"
                    f"CRITICAL RULES:\n"
                    f"1. Output ONLY valid <question ...>...</question> Moodle XML nodes for every question concluding on Page {p_num}.\n"
                    f"2. Every question MUST include full stacked bilingual text in <questiontext> and a detailed 5-step solution in <generalfeedback>.\n"
                    f"3. For diagrams from the attached IDs, embed using `<img src=\"@@PLUGINFILE@@/IMAGE_ID.jpeg\" />`.\n"
                    f"4. If no questions conclude on Page {p_num}, return empty string."
                )

                turn_contents: List[Any] = [turn_prompt]
                for img_name, filepath in page_data.images.items():
                    turn_contents.append(f"Diagram reference ID: {img_name}")
                    try:
                        turn_contents.append(Image.open(filepath))
                    except Exception as e:
                        logger.warning(f"Could not load image {filepath}: {e}")

                raw_turn_output = self.communicator.generate(
                    system_instruction=system_instruction,
                    contents=turn_contents,
                    output_filename=f"page_{p_num}.xml",
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
            for img_name, filepath in ocr_result.all_images.items():
                contents.append(f"Diagram reference ID: {img_name}")
                try:
                    contents.append(Image.open(filepath))
                except Exception:
                    pass

            raw_output = self.communicator.generate(
                system_instruction=system_instruction,
                contents=contents,
                output_filename=f"{pdf_path.stem}_moodle.xml",
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

class ChapterQuestionGenerator:
    """Synthesizes questions from chapter PDFs or Markdown files."""

    def __init__(
        self,
        communicator: BaseAICommunicator,
        ocr_engine: Optional[MistralOCREngine] = None,
        rules_dict: Optional[Dict[str, Any]] = None,
        languages: Optional[List[str]] = None,
        standards: str = "General",
        tags: str = "",
        difficulty: str = "medium",
        num_questions: int = 5,
        output_format: str = "xml",
        pdf_engine: str = "html",
        page_range: Optional[List[int]] = None,
        staging_dir: Path = Path("extracted_data"),
    ):
        self.communicator = communicator
        self.ocr_engine = ocr_engine or MistralOCREngine()
        self.rules_dict = rules_dict or {}
        self.languages = languages or ["english"]
        self.standards = standards
        self.tags = tags
        self.difficulty = difficulty
        self.num_questions = num_questions
        self.output_format = output_format.lower()
        self.pdf_engine = pdf_engine.lower()
        self.page_range = page_range
        self.staging_dir = Path(staging_dir)

    def process_file(self, input_file: Path, output_dir: Path) -> Path:
        """Synthesizes questions from a chapter file (.pdf or .md)."""
        input_file = Path(input_file).resolve()
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        logger.info(f"\n{'='*60}\n📚 [GENERATE-CHAPTER] Processing: {input_file.name} ({self.output_format.upper()})\n{'='*60}")

        # 1. Read or OCR chapter content
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
            mode="generate-chapter",
            output_format=self.output_format,
            pdf_engine=self.pdf_engine,
        )

        lang_instruction, lang_tags = build_language_instructions(
            self.languages, output_format=self.output_format, pdf_engine=self.pdf_engine
        )
        all_tags = [t.strip() for t in self.tags.split(",") if t.strip()] + lang_tags

        if self.output_format == "pdf":
            if self.pdf_engine == "tex":
                format_instruction = (
                    "CRITICAL FORMAT RULE: Output a complete standalone LaTeX document starting with `\\documentclass{article}` "
                    "and ending with `\\end{document}`. DO NOT output HTML tags (<p>, <div>, <hr/>) and DO NOT output Moodle XML (<question>, <quiz>)."
                )
            else:
                format_instruction = (
                    "CRITICAL FORMAT RULE: Output a complete standalone HTML5 document starting with `<!DOCTYPE html><html>` "
                    "and ending with `</html>`. DO NOT output Moodle XML (<question>, <quiz>)."
                )
        else:
            format_instruction = "CRITICAL FORMAT RULE: Generate valid Moodle XML (<quiz>...</quiz>)."

        # 3. Build turn content
        prompt_text = (
            f"### Chapter Content Markdown:\n\n{markdown_text}\n\n"
            f"### Generation Constraints:\n"
            f"- Number of Questions: {self.num_questions}\n"
            f"- Difficulty Level: {self.difficulty.upper()}\n"
            f"- Target Languages: {', '.join(self.languages)}\n"
            f"- Target Standards: {self.standards}\n"
            f"- Global Tags: {', '.join(all_tags)}\n"
            f"- Output Format: {self.output_format.upper()}\n"
            f"- PDF Engine (if applicable): {self.pdf_engine.upper()}\n\n"
            f"{lang_instruction}\n"
            f"{format_instruction}\n"
            f"Synthesize high quality calibrated questions based strictly on the chapter content."
        )

        contents: List[Any] = [prompt_text]
        for img_name, filepath in image_map.items():
            contents.append(f"Diagram reference ID: {img_name}")
            try:
                contents.append(Image.open(filepath))
            except Exception:
                pass

        ext = "pdf" if self.output_format == "pdf" else "xml"
        intermediate_ext = "tex" if self.pdf_engine == "tex" else "html"
        output_artifact_name = f"{input_file.stem}_synthetic.{intermediate_ext if self.output_format == 'pdf' else 'xml'}"

        # 4. Dispatch to communicator
        raw_output = self.communicator.generate(
            system_instruction=system_instruction,
            contents=contents,
            output_filename=output_artifact_name,
        )

        # 5. Handle output format & local PDF compilation
        if self.output_format == "pdf":
            final_pdf_path = output_dir / f"{input_file.stem}_synthetic.pdf"
            if self.pdf_engine == "tex":
                compile_tex_to_pdf(raw_output, final_pdf_path, image_map=image_map)
            else:
                compile_html_to_pdf(raw_output, final_pdf_path, image_map=image_map)
            logger.info(f"✨ Compiled Synthetic PDF to: {final_pdf_path}")
            return final_pdf_path
        else:
            final_xml = fix_and_inject_moodle_xml(raw_output, image_map)
            final_xml_path = output_dir / f"{input_file.stem}_synthetic.xml"
            final_xml_path.write_text(final_xml, encoding="utf-8")
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

class SyllabusQuestionGenerator:
    """Synthesizes complete mock exams or question banks from syllabi & blueprints."""

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
        self.sample_pdf = sample_pdf
        self.staging_dir = Path(staging_dir)

    def process_blueprint(self, blueprint_path: Path, output_dir: Path) -> Path:
        """Synthesizes a full calibrated mock exam from a blueprint JSON or syllabus."""
        blueprint_path = Path(blueprint_path).resolve()
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

        if not blueprint_path.exists():
            raise FileNotFoundError(f"Blueprint file not found: {blueprint_path}")

        blueprint_data = {}
        if blueprint_path.suffix.lower() == ".json":
            blueprint_data = json.loads(blueprint_path.read_text(encoding="utf-8"))
        else:
            blueprint_data = {
                "exam_name": blueprint_path.stem.upper(),
                "subjects": [{"name": blueprint_path.stem, "syllabus_file": str(blueprint_path), "total_questions": 10}],
            }

        exam_name = blueprint_data.get("exam_name", "MOCK_EXAM")
        subjects = blueprint_data.get("subjects", [])

        logger.info(f"\n{'='*60}\n🎓 [GENERATE-SYLLABUS/MOCK] Exam: {exam_name} ({self.output_format.upper()})\n{'='*60}")

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
            mode="generate-mock",
            output_format=self.output_format,
            pdf_engine=self.pdf_engine,
        )

        lang_instruction, lang_tags = build_language_instructions(
            self.languages, output_format=self.output_format, pdf_engine=self.pdf_engine
        )
        all_tags = [t.strip() for t in self.tags.split(",") if t.strip()] + lang_tags

        if self.output_format == "pdf":
            if self.pdf_engine == "tex":
                format_instruction = (
                    "CRITICAL FORMAT RULE: Output a complete standalone LaTeX document starting with `\\documentclass{article}` "
                    "and ending with `\\end{document}`. DO NOT output HTML tags (<p>, <div>, <hr/>) and DO NOT output Moodle XML (<question>, <quiz>)."
                )
            else:
                format_instruction = (
                    "CRITICAL FORMAT RULE: Output a complete standalone HTML5 document starting with `<!DOCTYPE html><html>` "
                    "and ending with `</html>`. DO NOT output Moodle XML (<question>, <quiz>)."
                )
        else:
            format_instruction = "CRITICAL FORMAT RULE: Generate valid Moodle XML (<quiz>...</quiz>)."

        # 3. Build turn content
        prompt_text = (
            f"### Exam Blueprint & Combined Syllabi Scope:\n{all_syllabi_text}\n\n"
            f"### Global Blueprint Constraints:\n"
            f"- Exam Name: {exam_name}\n"
            f"- Target Standards: {self.standards}\n"
            f"- Target Languages: {', '.join(self.languages)}\n"
            f"- Difficulty Breakdown: {self.difficulty_mix}\n"
            f"- Global Tags: {', '.join(all_tags)}\n"
            f"- Output Format: {self.output_format.upper()}\n"
            f"- PDF Engine: {self.pdf_engine.upper()}\n\n"
            f"{lang_instruction}\n"
            f"{format_instruction}\n"
            f"Synthesize the complete exam paper adhering strictly to the blueprint."
        )

        contents: List[Any] = [prompt_text]

        intermediate_ext = "tex" if self.pdf_engine == "tex" else "html"
        output_artifact_name = f"mock_{exam_name.lower()}_bank.{intermediate_ext if self.output_format == 'pdf' else 'xml'}"

        # 4. Dispatch to communicator
        raw_output = self.communicator.generate(
            system_instruction=system_instruction,
            contents=contents,
            output_filename=output_artifact_name,
        )

        # 5. Handle output format & local PDF compilation
        if self.output_format == "pdf":
            final_pdf_path = output_dir / f"mock_{exam_name.lower()}_paper.pdf"
            if self.pdf_engine == "tex":
                compile_tex_to_pdf(raw_output, final_pdf_path)
            else:
                compile_html_to_pdf(raw_output, final_pdf_path)
            logger.info(f"✨ Compiled Mock Exam PDF to: {final_pdf_path}")
            return final_pdf_path
        else:
            final_xml = fix_and_inject_moodle_xml(raw_output, {})
            final_xml_path = output_dir / f"mock_{exam_name.lower()}_bank.xml"
            final_xml_path.write_text(final_xml, encoding="utf-8")
            logger.info(f"✅ Saved Mock Exam Moodle XML to: {final_xml_path}")
            return final_xml_path
