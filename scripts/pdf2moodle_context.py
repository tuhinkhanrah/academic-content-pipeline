#!/usr/bin/env python3
"""
Question Paper to Moodle XML Extractor CLI (Context-Aware Chat Edition)

Uses Google GenAI SDK Chat Sessions to maintain cross-page continuity.
Allows the AI to stitch together exam questions that start on one page 
and end on the next.
"""

import argparse
import base64
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import List, Optional

import fitz  # PyMuPDF
from google import genai
from google.genai import types
from google.genai.errors import APIError
import xml.etree.ElementTree as ET

logger = logging.getLogger("pdf2moodle")


def setup_logger(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)
    logger.setLevel(level)
    logger.addHandler(handler)


def encode_bytes_to_base64(raw_bytes: bytes) -> str:
    return base64.b64encode(raw_bytes).decode("utf-8")


def load_prompt(prompt_path: Path) -> str:
    try:
        logger.info(f"📄 Loading external system prompt from: {prompt_path}")
        return prompt_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.critical(f"Failed to load required prompt file at {prompt_path}: {e}")
        sys.exit(1)


def crop_diagram_from_page(
    page: fitz.Page, ymin: int, xmin: int, ymax: int, xmax: int, dpi: int = 150
) -> bytes:
    rect = page.rect
    page_width, page_height = rect.width, rect.height

    x0 = (xmin / 1000.0) * page_width
    y0 = (ymin / 1000.0) * page_height
    x1 = (xmax / 1000.0) * page_width
    y1 = (ymax / 1000.0) * page_height

    crop_rect = fitz.Rect(x0, y0, x1, y1)
    pix = page.get_pixmap(clip=crop_rect, dpi=dpi)
    return pix.tobytes("png")


class StatefulPaperExtractor:
    """Manages multi-page chat memory, question extraction, and XML assembly."""

    def __init__(
        self,
        client: genai.Client,
        prompt_text: str,
        standards: List[str],
        default_grade: float,
        penalty: float,
        negative_fraction: int,
        tags: List[str],
        memory_span: int = 3,
        model_name: str = "gemini-2.5-pro",
        dpi: int = 150,
        rate_limit_delay: float = 4.0,
    ):
        self.client = client
        self.prompt_text = prompt_text
        self.standards = standards
        self.default_grade = default_grade
        self.penalty = penalty
        self.negative_fraction = negative_fraction
        self.tags = tags
        self.memory_span = memory_span
        self.model_name = model_name
        self.dpi = dpi
        self.rate_limit_delay = rate_limit_delay

    def process_pdf(self, pdf_path: Path, output_dir: Path) -> bool:
        pdf_stem = pdf_path.stem
        pdf_work_dir = output_dir / pdf_stem
        image_output_dir = pdf_work_dir / "extracted_assets"
        xml_output_path = pdf_work_dir / f"{pdf_stem}_moodle_extracted_bank.xml"

        logger.info(f"Starting stateful extraction pipeline for: {pdf_path.name}")
        image_output_dir.mkdir(parents=True, exist_ok=True)

        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            logger.error(f"Failed to open PDF {pdf_path}: {e}")
            return False

        all_questions_xml: List[str] = []
        total_pages = len(doc)

        logger.info(f"[{pdf_stem}] Initializing chat session with memory span: {self.memory_span} pages.")
        chat = self.client.chats.create(
            model=self.model_name,
            config=types.GenerateContentConfig(
                system_instruction=self.prompt_text,
                temperature=0.2,
            ),
        )

        for page_num in range(total_pages):
            logger.info(f"[{pdf_stem}] Processing Page {page_num + 1}/{total_pages} (Context-Aware)...")
            page = doc[page_num]

            pix = page.get_pixmap(dpi=self.dpi)
            page_img_bytes = pix.tobytes("png")

            chat = self._prune_chat_history(chat)
            turn_prompt = self._build_turn_prompt(page_num + 1)

            ai_output = self._send_message_with_retry(
                chat=chat,
                page_img_bytes=page_img_bytes,
                prompt=turn_prompt,
                attempt_limit=5
            )

            if not ai_output:
                logger.warning(f"[{pdf_stem}] Page {page_num + 1}: Empty response. Skipping.")
                continue

            extracted_questions = self._extract_clean_question_nodes(ai_output)
            if not extracted_questions:
                logger.info(f"[{pdf_stem}] Page {page_num + 1}: No complete questions found or all deferred.")
                continue

            processed_page_questions = []
            for question_xml in extracted_questions:
                processed_xml = self._process_diagram_tokens_in_question(
                    page=page,
                    question_xml=question_xml,
                    image_dir=image_output_dir
                )
                processed_page_questions.append(processed_xml)

            all_questions_xml.extend(processed_page_questions)
            logger.info(f"[{pdf_stem}] Page {page_num + 1}: Extracted {len(processed_page_questions)} question(s).")
            
            time.sleep(self.rate_limit_delay)

        doc.close()

        if not all_questions_xml:
            logger.error(f"[{pdf_stem}] No valid questions extracted across entire paper. Aborting.")
            return False

        final_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<quiz>\n"
            f'{chr(10).join(all_questions_xml)}\n'
            "</quiz>"
        )

        try:
            xml_output_path.write_text(final_xml, encoding="utf-8")
            logger.info(f"✅ Successfully compiled {len(all_questions_xml)} questions to: {xml_output_path}")
            return True
        except Exception as e:
            logger.error(f"[{pdf_stem}] Failed to write XML output: {e}")
            return False

    def _prune_chat_history(self, chat: genai.chats.Chat) -> genai.chats.Chat:
        if self.memory_span <= 0:
            return self.client.chats.create(
                model=self.model_name,
                config=types.GenerateContentConfig(
                    system_instruction=self.prompt_text,
                    temperature=0.2,
                ),
            )

        history = chat.get_history()
        max_messages = self.memory_span * 2
        
        if len(history) > max_messages:
            trimmed_history = history[-max_messages:]
            while trimmed_history and trimmed_history[0].role != "user":
                trimmed_history.pop(0)
                
            return self.client.chats.create(
                model=self.model_name,
                history=trimmed_history,
                config=types.GenerateContentConfig(
                    system_instruction=self.prompt_text,
                    temperature=0.2,
                ),
            )
        return chat

    def _build_turn_prompt(self, current_page_num: int) -> str:
        formatted_standards = ", ".join(self.standards) if self.standards else "General"
        
        tag_xml_nodes = [f"      <tag><text>{t}</text></tag>" for t in self.tags]
        for std in self.standards:
            tag_xml_nodes.append(f"      <tag><text>standard:{std}</text></tag>")
        tags_block = "\n".join(tag_xml_nodes) if tag_xml_nodes else "      <tag><text>extracted:ai</text></tag>"

        return (
            f"=== PAGE {current_page_num} EXTRACTION ===\n"
            f"Analyze Page {current_page_num}. Extract ALL complete questions that conclude on this page.\n"
            f"If a question starts here but ends on the next page, DEFER it.\n\n"
            f"MARKING SCHEME INSTRUCTION:\n"
            f"- Read the section header or document rules on this page (e.g., Section 1, Category 2, etc.).\n"
            f"- Dynamically calculate and populate `<defaultgrade>`, `<penalty>`, and option `fraction=\"...\"` values matching the exact section rules.\n\n"
            f"1. Target Exam Standard(s): {formatted_standards}\n"
            f"2. Required Global Tags:\n"
            f"    <tags>\n"
            f"{tags_block}\n"
            f"    </tags>\n\n"
            f"=== STRICT SILENCE DIRECTIVE ===\n"
            f"If this page contains no concluding questions, return an empty string (\"\"). Output ONLY valid <question> XML nodes."
        )

    def _send_message_with_retry(
        self, chat: genai.chats.Chat, page_img_bytes: bytes, prompt: str, attempt_limit: int = 5
    ) -> Optional[str]:
        base_delay = 5.0
        for attempt in range(attempt_limit):
            try:
                response = chat.send_message(
                    message=[
                        types.Part.from_bytes(data=page_img_bytes, mime_type="image/png"),
                        prompt,
                    ]
                )
                return response.text
            except APIError as e:
                if e.code in (429, 503):
                    delay = base_delay * (2 ** attempt)
                    logger.warning(f"Rate limit hit. Retrying in {delay}s...")
                    time.sleep(delay)
                else:
                    logger.error(f"API Error: {e}")
                    break
            except Exception as e:
                logger.error(f"Unexpected error: {e}")
                break
        return None

    @staticmethod
    def _extract_clean_question_nodes(raw_text: str) -> List[str]:
        if not raw_text:
            return []
            
        # 1. Clean up common LLM markdown artifacts
        raw_text = raw_text.replace("```xml", "").replace("```", "").strip()
        
        # 2. Extract the question nodes
        pattern = re.compile(r"(<question\b[^>]*>.*?</question>)", re.DOTALL | re.IGNORECASE)
        matches = pattern.findall(raw_text)
        
        valid_nodes = []
        for node in matches:
            node = node.strip()
            # 3. Validate the XML node
            try:
                # If ElementTree can parse it, it is structurally valid XML
                ET.fromstring(node)
                valid_nodes.append(node)
            except ET.ParseError as e:
                logger.error(f"❌ Dropping malformed XML question due to Parse Error: {e}")
                # Optional: You can print a snippet of the broken node for debugging
                # print(f"Broken snippet: {node[:100]}...") 
                continue
                
        return valid_nodes

    def _process_diagram_tokens_in_question(
        self, page: fitz.Page, question_xml: str, image_dir: Path
    ) -> str:
        crop_tokens = re.findall(r"\[CROP_BOX:(\d+),(\d+),(\d+),(\d+)\]", question_xml)
        if not crop_tokens:
            return question_xml

        file_nodes_to_inject = []
        for token in crop_tokens:
            ymin, xmin, ymax, xmax = map(int, token)
            cropped_bytes = crop_diagram_from_page(page, ymin, xmin, ymax, xmax, dpi=self.dpi)

            asset_uuid = str(uuid.uuid4())
            filename = f"diagram_{asset_uuid}.png"
            file_path = image_dir / filename

            try:
                file_path.write_bytes(cropped_bytes)
            except Exception as e:
                logger.error(f"Failed to save image {filename}: {e}")
                continue

            b64_str = encode_bytes_to_base64(cropped_bytes)
            file_node = f'    <file name="{filename}" path="/" encoding="base64">{b64_str}</file>'
            file_nodes_to_inject.append(file_node)

            raw_token_str = f"[CROP_BOX:{ymin},{xmin},{ymax},{xmax}]"
            moodle_img_tag = (
                f'<p><img src="@@PLUGINFILE@@/{filename}" alt="Question Diagram" '
                f'class="img-responsive" style="max-width: 100%; height: auto;" /></p>'
            )
            question_xml = question_xml.replace(raw_token_str, moodle_img_tag)

        if file_nodes_to_inject:
            files_block = "\n" + "\n".join(file_nodes_to_inject)
            question_xml = re.sub(
                r"(</questiontext>)",
                f"{files_block}\n\\1",
                question_xml,
                flags=re.IGNORECASE
            )

        return question_xml


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract Moodle XML banks from Question Paper PDFs using Context-Aware Chat.")
    parser.add_argument("-i", "--input-dir", type=Path, required=True, help="Directory containing exam paper PDFs.")
    parser.add_argument("-o", "--output-dir", type=Path, required=True, help="Output directory.")
    parser.add_argument("-p", "--prompt", type=Path, required=True, help="Path to prompt markdown file.")
    parser.add_argument("-s", "--standards", type=str, default="JEE-Main", help="Target standards.")
    parser.add_argument("--memory-span", type=int, default=3, help="Pages memory (default: 3).")
    parser.add_argument("--default-grade", type=float, default=4.0, help="Default grade (default: 4.0).")
    parser.add_argument("--penalty", type=float, default=0.25, help="Penalty (default: 0.25).")
    parser.add_argument("--negative-fraction", type=int, default=-25, help="Negative fraction (default: -25).")
    parser.add_argument("-t", "--tags", type=str, default="", help="Global tags.")
    parser.add_argument("-m", "--model", type=str, default="gemini-3.5-flash", help="Gemini model.")
    parser.add_argument("--api-key", type=str, default=os.getenv("GEMINI_API_KEY"), help="API Key.")
    parser.add_argument("--dpi", type=int, default=150, help="DPI resolution.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logs.")

    args = parser.parse_args()
    setup_logger(args.verbose)

    if not args.api_key:
        logger.critical("No API key provided.")
        sys.exit(1)

    client = genai.Client(api_key=args.api_key)
    prompt_text = load_prompt(args.prompt)

    extractor = StatefulPaperExtractor(
        client=client,
        prompt_text=prompt_text,
        standards=[s.strip() for s in args.standards.split(",") if s.strip()],
        default_grade=args.default_grade,
        penalty=args.penalty,
        negative_fraction=args.negative_fraction,
        tags=[t.strip() for t in args.tags.split(",") if t.strip()],
        memory_span=args.memory_span,
        model_name=args.model,
        dpi=args.dpi,
    )

    pdf_files = sorted([f for f in args.input_dir.iterdir() if f.suffix.lower() == ".pdf"])
    for pdf_path in pdf_files:
        extractor.process_pdf(pdf_path, args.output_dir)


if __name__ == "__main__":
    main()