#!/usr/bin/env python3
"""
Chapter to Moodle XML Question Generator CLI (Context-Aware Chat Edition)

Generates new, original Moodle XML questions from textbook chapters using 
Google GenAI SDK Chat Sessions to maintain cross-page continuity.
Supports system prompt caching & Python code execution.
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

from prompt_cache_manager import get_or_create_prompt_cache

logger = logging.getLogger("chapter2moodle")


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


def log_cli_args(args: argparse.Namespace) -> None:
    logger.info("CLI arguments:")
    for key in sorted(vars(args)):
        value = getattr(args, key)
        if key == "api_key" and value:
            value = "***MASKED***"
        logger.info("  %s=%s", key, value)


def crop_diagram_from_page(
    page: fitz.Page, ymin: int, xmin: int, ymax: int, xmax: int, dpi: int = 200
) -> bytes:
    rect = page.rect
    page_width, page_height = rect.width, rect.height

    # Convert 1000-scale coordinates to PyMuPDF points
    x0 = (xmin / 1000.0) * page_width
    y0 = (ymin / 1000.0) * page_height
    x1 = (xmax / 1000.0) * page_width
    y1 = (ymax / 1000.0) * page_height

    # 1. Catch inverted coordinates (which cause negative width/height)
    if x0 >= x1 or y0 >= y1:
        logging.warning(
            f"Invalid bounding box ignored: x0={x0:.1f}, x1={x1:.1f}, y0={y0:.1f}, y1={y1:.1f}"
        )
        return b"" # Return empty bytes to prevent crashing

    crop_rect = fitz.Rect(x0, y0, x1, y1)

    # 2. Intersect with the page boundary so PyMuPDF doesn't try to draw outside the page
    crop_rect = crop_rect.intersect(rect)

    # 3. Check if the intersection resulted in an empty rectangle (e.g., box was off-page)
    if crop_rect.is_empty:
        logging.warning("Cropping rectangle is completely outside page boundaries.")
        return b""

    # Generate the image safely
    pix = page.get_pixmap(clip=crop_rect, dpi=dpi)
    return pix.tobytes("png")


class StatefulChapterGenerator:
    """Manages chat memory, question generation, and XML output."""

    def __init__(
        self,
        client: genai.Client,
        prompt_text: str,
        num_questions: int,
        standards: List[str],
        default_grade: float,
        penalty: float,
        negative_fraction: int,
        tags: List[str],
        memory_span: int = 3,
        model_name: str = "gemini-2.0-flash-thinking-exp",
        dpi: int = 200,
        rate_limit_delay: float = 4.0,
        temperature: float = 0.1,
        thinking_level: Optional[str] = None,
        thinking_budget: Optional[int] = None,
        include_thoughts: bool = False,
        enable_code_execution: bool = False,
        cached_prompt_name: Optional[str] = None,
    ):
        self.client = client
        self.prompt_text = prompt_text
        self.num_questions = num_questions
        self.standards = standards
        self.default_grade = default_grade
        self.penalty = penalty
        self.negative_fraction = negative_fraction
        self.tags = tags
        self.memory_span = memory_span
        self.model_name = model_name
        self.dpi = dpi
        self.rate_limit_delay = rate_limit_delay
        self.temperature = temperature
        self.thinking_level = thinking_level
        self.thinking_budget = thinking_budget
        self.include_thoughts = include_thoughts
        self.enable_code_execution = enable_code_execution
        self.cached_prompt_name = cached_prompt_name

    def _build_chat_config(self) -> types.GenerateContentConfig:
        thinking_config = None
        if "thinking" in self.model_name.lower() or (
            self.thinking_level is not None
            or self.thinking_budget is not None
            or self.include_thoughts
        ):
            level_map = {
                "minimal": types.ThinkingLevel.MINIMAL,
                "low": types.ThinkingLevel.LOW,
                "medium": types.ThinkingLevel.MEDIUM,
                "high": types.ThinkingLevel.HIGH,
            }
            thinking_kwargs = {}
            if self.thinking_level is not None:
                thinking_kwargs["thinking_level"] = level_map[self.thinking_level]
            if self.thinking_budget is not None:
                thinking_kwargs["thinking_budget"] = self.thinking_budget
            elif "thinking" in self.model_name.lower() and self.thinking_level is None:
                thinking_kwargs["thinking_budget"] = 2048
            if self.include_thoughts:
                thinking_kwargs["include_thoughts"] = True
            thinking_config = types.ThinkingConfig(**thinking_kwargs)

        # STRICT API RULE: Do NOT pass tools or system_instruction when cached_content is present!
        if self.cached_prompt_name:
            return types.GenerateContentConfig(
                cached_content=self.cached_prompt_name,
                temperature=self.temperature,
                thinking_config=thinking_config,
            )

        tools = None
        if self.enable_code_execution:
            tools = [types.Tool(code_execution=types.ToolCodeExecution())]

        return types.GenerateContentConfig(
            system_instruction=self.prompt_text,
            temperature=self.temperature,
            thinking_config=thinking_config,
            tools=tools,
        )

    def process_pdf(self, pdf_path: Path, output_dir: Path) -> bool:
        pdf_stem = pdf_path.stem
        pdf_work_dir = output_dir / pdf_stem
        image_output_dir = pdf_work_dir / "extracted_assets"
        xml_output_path = pdf_work_dir / f"{pdf_stem}_moodle_generated_bank.xml"

        logger.info(f"Starting question generation pipeline for: {pdf_path.name}")
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
            config=self._build_chat_config(),
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
                logger.info(f"[{pdf_stem}] Page {page_num + 1}: Non-content page or no questions generated.")
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
            logger.info(f"[{pdf_stem}] Page {page_num + 1}: Generated {len(processed_page_questions)} question(s).")
            
            time.sleep(self.rate_limit_delay)

        doc.close()

        if not all_questions_xml:
            logger.error(f"[{pdf_stem}] No valid questions generated. Aborting.")
            return False

        final_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<quiz>\n"
            f'{chr(10).join(all_questions_xml)}\n'
            "</quiz>"
        )

        try:
            xml_output_path.write_text(final_xml, encoding="utf-8")
            logger.info(f"✅ Successfully written {len(all_questions_xml)} questions to: {xml_output_path}")
            return True
        except Exception as e:
            logger.error(f"[{pdf_stem}] Failed to write XML output: {e}")
            return False

    def _prune_chat_history(self, chat: genai.chats.Chat) -> genai.chats.Chat:
        if self.memory_span <= 0:
            return self.client.chats.create(
                model=self.model_name,
                config=self._build_chat_config(),
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
                config=self._build_chat_config(),
            )
        return chat

    def _build_turn_prompt(self, current_page_num: int) -> str:
        formatted_standards = ", ".join(self.standards) if self.standards else "General"
        tag_xml_nodes = [f"      <tag><text>{t}</text></tag>" for t in self.tags]
        for std in self.standards:
            tag_xml_nodes.append(f"      <tag><text>standard:{std}</text></tag>")
        tags_block = "\n".join(tag_xml_nodes) if tag_xml_nodes else "      <tag><text>generated:ai</text></tag>"

        return (
            f"=== PAGE {current_page_num} GENERATION ===\n"
            f"Analyze Page {current_page_num}. Generate exactly {self.num_questions} original question(s) "
            f"based on the concepts on this page.\n\n"
            f"1. Target Exam Standard(s): {formatted_standards}\n"
            f"2. Default Question Grade: <defaultgrade>{self.default_grade}</defaultgrade>\n"
            f"3. Incorrect Answer Penalty: <penalty>{self.penalty}</penalty>\n"
            f"4. Incorrect Answer Fraction: fraction=\"{self.negative_fraction}\"\n"
            f"5. Required Global Tags:\n"
            f"    <tags>\n"
            f"{tags_block}\n"
            f"    </tags>\n\n"
            f"=== STRICT SILENCE DIRECTIVE ===\n"
            f"If this page is non-academic or blank, return an empty string (\"\"). Output ONLY valid <question> XML nodes."
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
        raw_text = raw_text.replace("```xml", "").replace("```", "").strip()
        pattern = re.compile(r"(<question\b[^>]*>.*?</question>)", re.DOTALL | re.IGNORECASE)
        matches = pattern.findall(raw_text)
        
        valid_nodes = []
        for node in matches:
            node = node.strip()
            try:
                ET.fromstring(node)
                valid_nodes.append(node)
            except ET.ParseError as e:
                logger.error(f"❌ Dropping malformed XML question due to Parse Error: {e}")
                continue
        return valid_nodes

    def _process_diagram_tokens_in_question(
        self, page: fitz.Page, question_xml: str, image_dir: Path
    ) -> str:
        # Define the block types that can contain text/images in Moodle XML
        block_tags = "questiontext|generalfeedback|correctfeedback|partiallycorrectfeedback|incorrectfeedback|answer"
        block_pattern = rf"(<({block_tags})\b[^>]*>.*?)(</\2>)"

        def block_replacer(match):
            prefix = match.group(1)  # Everything from opening tag up to just before closing tag
            tag_name = match.group(2)
            suffix = match.group(3)  # The closing tag itself, e.g., </answer>

            # Find all crop tokens inside this specific block
            crop_tokens = re.findall(r"\[CROP_BOX:(\d+),(\d+),(\d+),(\d+)\]", prefix)
            if not crop_tokens:
                return match.group(0)

            file_nodes_to_inject = []

            # Use a set to process each unique token in this block only once
            unique_tokens = list(set(crop_tokens))

            for token in unique_tokens:
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
                prefix = prefix.replace(raw_token_str, moodle_img_tag)

            # Inject the <file> tags right before the closing tag of the block they belong to
            if file_nodes_to_inject:
                files_block = "\n" + "\n".join(file_nodes_to_inject) + "\n"
                return prefix + files_block + suffix

            return prefix + suffix

        # Process each relevant Moodle XML block individually
        question_xml = re.sub(block_pattern, block_replacer, question_xml, flags=re.IGNORECASE | re.DOTALL)

        return question_xml


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Moodle XML questions from textbook PDFs using Context-Aware Chat.")
    parser.add_argument("-i", "--input-dir", type=Path, required=True, help="Directory containing textbook PDFs.")
    parser.add_argument("-o", "--output-dir", type=Path, required=True, help="Output directory.")
    parser.add_argument("-p", "--prompt", type=Path, required=True, help="Path to system instruction prompt markdown file.")
    
    parser.add_argument("--exam", type=str, required=True, choices=["NEET", "JEE Main", "JEE Advanced", "WBJEE", "CBSE"], help="Target exam standard.")
    parser.add_argument("--difficulty", type=str, required=True, choices=["Easy", "Medium", "Hard"], help="Target difficulty level.")

    parser.add_argument("-n", "--num-questions", type=int, default=2, help="Number of questions per page (default: 2).")
    parser.add_argument("-s", "--standards", type=str, default="JEE-Main", help="Comma-separated standards.")
    parser.add_argument("--memory-span", type=int, default=3, help="Pages memory (default: 3).")
    parser.add_argument("--default-grade", type=float, default=4.0, help="Default grade.")
    parser.add_argument("--penalty", type=float, default=0.25, help="Penalty.")
    parser.add_argument("--negative-fraction", type=int, default=-25, help="Negative fraction.")
    parser.add_argument("-t", "--tags", type=str, default="", help="Global tags.")
    parser.add_argument("-m", "--model", type=str, default="gemini-2.0-flash-thinking-exp", help="Gemini model.")
    parser.add_argument("--temperature", type=float, default=0.1, help="Sampling temperature (default: 0.1).")

    # SYSTEM PROMPT CACHING OPTIONS
    parser.add_argument("--cache-prompt", action="store_true", help="Cache system prompt file to reduce token upload overhead.")
    parser.add_argument("--refresh-prompt-cache", action="store_true", help="Force recreate prompt cache even if local hash matches.")
    parser.add_argument("--prompt-ttl", type=int, default=86400, help="Prompt Cache TTL in seconds (default: 86400 = 24 hrs).")

    parser.add_argument("--thinking-level", type=str, choices=["minimal", "low", "medium", "high"], default=None, help="Thinking level.")
    parser.add_argument("--thinking-budget", type=int, default=None, help="Thinking budget in tokens.")
    parser.add_argument("--include-thoughts", action="store_true", help="Include thought traces.")
    parser.add_argument("--code-execution", action="store_true", help="Enable Python Code Execution tool.")
    parser.add_argument("--api-key", type=str, default=os.getenv("GEMINI_API_KEY"), help="API Key.")
    parser.add_argument("--dpi", type=int, default=200, help="Cropping DPI (default: 200).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logs.")

    args = parser.parse_args()
    setup_logger(args.verbose)
    log_cli_args(args)

    if not args.api_key:
        logger.critical("No API key provided.")
        sys.exit(1)

    client = genai.Client(api_key=args.api_key)
    
    prompt_text = load_prompt(args.prompt)
    prompt_text = prompt_text.replace("{{TARGET_EXAM}}", args.exam)
    prompt_text = prompt_text.replace("{{TARGET_DIFFICULTY}}", args.difficulty)

    cached_prompt_name = None
    if args.cache_prompt:
        cached_prompt_name = get_or_create_prompt_cache(
            client=client,
            prompt_path=args.prompt,
            prompt_text=prompt_text,
            model_name=args.model,
            ttl_seconds=args.prompt_ttl,
            force_refresh=args.refresh_prompt_cache,
            enable_code_execution=args.code_execution,
        )

    generator = StatefulChapterGenerator(
        client=client,
        prompt_text=prompt_text,
        num_questions=args.num_questions,
        standards=[s.strip() for s in args.standards.split(",") if s.strip()],
        default_grade=args.default_grade,
        penalty=args.penalty,
        negative_fraction=args.negative_fraction,
        tags=[t.strip() for t in args.tags.split(",") if t.strip()],
        memory_span=args.memory_span,
        model_name=args.model,
        dpi=args.dpi,
        temperature=args.temperature,
        thinking_level=args.thinking_level,
        thinking_budget=args.thinking_budget,
        include_thoughts=args.include_thoughts,
        enable_code_execution=args.code_execution,
        cached_prompt_name=cached_prompt_name,
    )

    pdf_files = sorted([f for f in args.input_dir.iterdir() if f.suffix.lower() == ".pdf"])
    for pdf_path in pdf_files:
        generator.process_pdf(pdf_path, args.output_dir)


if __name__ == "__main__":
    main()