#!/usr/bin/env python3
"""
Question Paper to Moodle XML Extractor CLI (Context-Aware Chat Edition)

Uses Google GenAI SDK Chat Sessions to maintain cross-page continuity.
Settings Precedence: Explicit CLI Argument --> JSON Config File --> Built-in Default
"""

import argparse
import logging
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

import pymupdf
from google import genai
from google.genai import types
from google.genai.errors import APIError
import xml.etree.ElementTree as ET

from moodle_utils import (
    build_language_instructions,
    encode_bytes_to_base64,
    load_combined_prompt,
    load_file_content,
    setup_logger,
    get_or_create_prompt_cache,
    render_page_to_image_bytes
)

logger = logging.getLogger("moodle_system")

def crop_diagram_from_page(
    page: pymupdf.Page, 
    ymin: int, 
    xmin: int, 
    ymax: int, 
    xmax: int, 
    dpi: int = 200,
    padding_cm: float = 0.5
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
        logging.warning(
            f"Invalid bounding box ignored: x0={x0:.1f}, x1={x1:.1f}, y0={y0:.1f}, y1={y1:.1f}"
        )
        return b"" 

    crop_rect = pymupdf.Rect(x0, y0, x1, y1).intersect(rect)

    if crop_rect.is_empty:
        logging.warning("Cropping rectangle is completely outside page boundaries.")
        return b""

    pix = page.get_pixmap(clip=crop_rect, dpi=dpi)
    return pix.tobytes("png")


class StatefulPaperExtractor:
    """Manages multi-page chat memory, question extraction, and XML assembly."""

    def __init__(
        self,
        client: genai.Client,
        prompt_text: str,
        standards: List[str],
        tags: List[str],
        languages: List[str],
        memory_span: int = 3,
        model_name: str = "gemini-3.6-flash",
        dpi: int = 200,
        zoom: float = 1.0,
        enhance: bool = False,
        rate_limit_delay: float = 4.0,
        retry_base_delay: float = 4.0,
        attempt_limit: int = 10,
        padding_cm: float = 0.5,
        temperature: float = 0.1,
        thinking_level: Optional[str] = None,
        instruction_text: str = "",
        instruction_page: int = 1,
        page_range: Optional[Tuple[int, int]] = None,
        cached_prompt_name: Optional[str] = None,
        verify_online: bool = False,
    ):
        self.client = client
        self.prompt_text = prompt_text
        self.standards = standards
        self.tags = tags
        self.languages = languages
        self.memory_span = memory_span
        self.model_name = model_name
        self.dpi = dpi
        self.zoom = zoom
        self.enhance = enhance
        self.rate_limit_delay = rate_limit_delay
        self.retry_base_delay = retry_base_delay
        self.attempt_limit = attempt_limit
        self.padding_cm = padding_cm
        self.temperature = temperature
        self.thinking_level = thinking_level
        self.instruction_text = instruction_text
        self.instruction_page = instruction_page
        self.page_range = page_range
        self.cached_prompt_name = cached_prompt_name
        self.verify_online = verify_online

    def _build_thinking_config(self) -> Optional[types.ThinkingConfig]:
        if not self.thinking_level:
            return None

        level_map = {
            "minimal": types.ThinkingLevel.MINIMAL,
            "low": types.ThinkingLevel.LOW,
            "medium": types.ThinkingLevel.MEDIUM,
            "high": types.ThinkingLevel.HIGH,
        }

        return types.ThinkingConfig(thinking_level=level_map[self.thinking_level])

    def _build_chat_config(self) -> types.GenerateContentConfig:
        # Override safety settings to prevent false positives on biology/medical diagrams
        safety_settings = [
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                threshold=types.HarmBlockThreshold.BLOCK_NONE,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                threshold=types.HarmBlockThreshold.BLOCK_NONE,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                threshold=types.HarmBlockThreshold.BLOCK_NONE,
            ),
            types.SafetySetting(
                category=types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                threshold=types.HarmBlockThreshold.BLOCK_NONE,
            ),
        ]

        tool_config = types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode=types.FunctionCallingConfigMode.NONE,
            )
        )
        auto_fc = types.AutomaticFunctionCallingConfig(disable=True)
        tools = [types.Tool(google_search=types.GoogleSearch())] if self.verify_online else None
        thinking_config = self._build_thinking_config()

        if self.cached_prompt_name:
            return types.GenerateContentConfig(
                cached_content=self.cached_prompt_name,
                temperature=self.temperature,
                thinking_config=thinking_config,
                safety_settings=safety_settings,
                tools=tools,
                tool_config=tool_config,
                automatic_function_calling=auto_fc,
            )

        return types.GenerateContentConfig(
            system_instruction=self.prompt_text,
            temperature=self.temperature,
            thinking_config=thinking_config,
            safety_settings=safety_settings,
            tools=tools,
            tool_config=tool_config,
            automatic_function_calling=auto_fc,
        )

    def process_pdf(self, pdf_path: Path, output_dir: Path) -> bool:
        pdf_stem = pdf_path.stem
        pdf_work_dir = output_dir / pdf_stem
        image_output_dir = pdf_work_dir / "extracted_assets"
        pages_output_dir = pdf_work_dir / "page_images"
        xml_output_path = pdf_work_dir / f"{pdf_stem}_moodle_extracted_bank.xml"

        logger.info(f"Starting stateful extraction pipeline for: {pdf_path.name}")
        image_output_dir.mkdir(parents=True, exist_ok=True)
        pages_output_dir.mkdir(parents=True, exist_ok=True)

        try:
            doc = pymupdf.open(pdf_path)
        except Exception as e:
            logger.error(f"Failed to open PDF {pdf_path}: {e}")
            return False

        total_pdf_pages = len(doc)

        if self.instruction_page < 0 or self.instruction_page > total_pdf_pages:
            logger.error(
                f"[{pdf_stem}] Invalid instruction_page={self.instruction_page}. "
                f"Valid range is 0 to {total_pdf_pages}."
            )
            doc.close()
            return False

        start_page = 1
        end_page = total_pdf_pages

        if self.page_range:
            start_page = max(1, self.page_range[0])
            end_page = min(total_pdf_pages, self.page_range[1])

        if start_page > end_page:
            logger.error(
                f"[{pdf_stem}] Invalid target page range after bounds check: {start_page} to {end_page}."
            )
            doc.close()
            return False

        logger.info(f"[{pdf_stem}] Target page range: {start_page} to {end_page}")

        out_of_bounds_instruction_text = ""
        if (
            self.instruction_page > 0
            and self.instruction_page <= total_pdf_pages
            and (self.instruction_page < start_page or self.instruction_page > end_page)
        ):
            inst_page_obj = doc[self.instruction_page - 1]
            out_of_bounds_instruction_text = inst_page_obj.get_text("text").strip()

        all_questions_xml: List[str] = []

        logger.info(f"[{pdf_stem}] Initializing chat session with memory span: {self.memory_span} pages.")
        chat = self.client.chats.create(
            model=self.model_name,
            config=self._build_chat_config(),
        )

        for page_idx in range(start_page, end_page + 1):
            page_num_zero_based = page_idx - 1

            logger.info(f"[{pdf_stem}] Processing Page {page_idx}/{total_pdf_pages} (Context-Aware)...")
            page = doc[page_num_zero_based]

            raw_img_bytes = render_page_to_image_bytes(
                page, 
                dpi=self.dpi, 
                zoom=self.zoom, 
                enhance=False
            )
            raw_path = pages_output_dir / f"page_{page_idx:03d}_raw.png"
            raw_path.write_bytes(raw_img_bytes)

            if self.enhance:
                enhanced_img_bytes = render_page_to_image_bytes(
                    page, 
                    dpi=self.dpi, 
                    zoom=self.zoom, 
                    enhance=True
                )
                enhanced_path = pages_output_dir / f"page_{page_idx:03d}_enhanced.png"
                enhanced_path.write_bytes(enhanced_img_bytes)
                page_img_bytes = enhanced_img_bytes
            else:
                page_img_bytes = raw_img_bytes

            chat = self._prune_chat_history(chat)

            is_instruction_page = (page_idx == self.instruction_page)
            is_first_target_page = (page_idx == start_page)

            turn_prompt = self._build_turn_prompt(
                current_page_num=page_idx,
                is_instruction_page=is_instruction_page,
                is_first_target_page=is_first_target_page,
                out_of_bounds_instruction_text=out_of_bounds_instruction_text if is_first_target_page else "",
            )

            ai_output = self._send_message_with_retry(
                chat=chat,
                page_img_bytes=page_img_bytes,
                prompt=turn_prompt,
                attempt_limit=self.attempt_limit
            )

            if not ai_output:
                logger.warning(f"[{pdf_stem}] Page {page_idx}: Empty response. Skipping.")
                continue

            extracted_questions = self._extract_clean_question_nodes(ai_output)
            if not extracted_questions:
                logger.info(f"[{pdf_stem}] Page {page_idx}: No complete questions found or all deferred.")
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
            logger.info(f"[{pdf_stem}] Page {page_idx}: Extracted {len(processed_page_questions)} question(s).")
            
            time.sleep(self.rate_limit_delay)

        doc.close()

        if not all_questions_xml:
            logger.error(f"[{pdf_stem}] No valid questions extracted across target pages. Aborting.")
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

    def _build_turn_prompt(
        self, 
        current_page_num: int,
        is_instruction_page: bool,
        is_first_target_page: bool,
        out_of_bounds_instruction_text: str = ""
    ) -> str:
        formatted_standards = ", ".join(self.standards) if self.standards else "General"
        lang_instruction, lang_tags = build_language_instructions(self.languages)

        all_tags = set(self.tags)
        all_tags.update(lang_tags)
        
        tag_xml_nodes = [f"      <tag><text>{t}</text></tag>" for t in all_tags]
        #for std in self.standards:
            #tag_xml_nodes.append(f"      <tag><text>standard:{std}</text></tag>")
        tags_block = "\n".join(tag_xml_nodes) if tag_xml_nodes else "      <tag><text>extracted:ai</text></tag>"

        instruction_block = ""
        if self.instruction_text:
            instruction_block = f"=== EXTERNAL INSTRUCTIONS ===\n{self.instruction_text}\n\n"

        page_instruction_marker = ""
        if is_instruction_page:
            page_instruction_marker = f"NOTE: Page {current_page_num} is designated as the instruction page.\n\n"
        elif is_first_target_page and out_of_bounds_instruction_text:
            page_instruction_marker = f"=== FRONT-PAGE INSTRUCTIONS ===\n{out_of_bounds_instruction_text}\n\n"

        online_verify_block = ""
        if self.verify_online:
            online_verify_block = (
                "=== MANDATORY ONLINE VERIFICATION ===\n"
                "For each extracted question, perform Google Search verification before finalizing answer fraction.\n"
                "Use reliable sources and keep confidence high; if conflicting sources, keep existing answer and flag uncertainty in generalfeedback.\n\n"
            )

        return (
            f"=== PAGE {current_page_num} EXTRACTION ===\n"
            f"{instruction_block}"
            f"{page_instruction_marker}"
            f"{online_verify_block}"
            f"{lang_instruction}\n"
            f"Analyze Page {current_page_num}. Extract ALL complete questions that conclude on this page.\n"
            f"If a question starts here but ends on the next page, DEFER it.\n\n"
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
        for attempt in range(attempt_limit):
            try:
                if attempt == 0:
                    message_parts = [
                        types.Part.from_bytes(data=page_img_bytes, mime_type="image/png"),
                        prompt,
                    ]
                else:
                    message_parts = [
                        types.Part.from_bytes(data=page_img_bytes, mime_type="image/png"),
                        (
                            "CRITICAL: Do not call any tool or function. "
                            "Output only raw <question> XML nodes or empty string.\n\n"
                            + prompt
                        ),
                    ]

                response = chat.send_message(
                    message=[
                        *message_parts,
                    ]
                )

                if response.candidates:
                    candidate = response.candidates[0]
                    
                    # Log if the model was blocked or hit token limits
                    if candidate.finish_reason != types.FinishReason.STOP:
                        if candidate.finish_reason == types.FinishReason.MALFORMED_FUNCTION_CALL:
                            delay = self.retry_base_delay * (2 ** attempt)
                            logger.info(f"Transient malformed function call. Retrying in {delay:.1f}s...")
                            time.sleep(delay)
                            continue
                        logger.warning(f"⚠️ Model stopped unexpectedly. Finish Reason: {candidate.finish_reason.name}")

                    if candidate.content and candidate.content.parts:
                        text_parts = [
                            part.text for part in candidate.content.parts
                            if getattr(part, "text", None) is not None
                        ]
                        if text_parts:
                            return "".join(text_parts).strip()

                if getattr(response, "text", None):
                    return response.text.strip()

                delay = self.retry_base_delay * (2 ** attempt)
                logger.warning(f"Empty non-text response. Retrying in {delay:.1f}s...")
                time.sleep(delay)
                continue
            except APIError as e:
                if e.code in (429, 503):
                    delay = self.retry_base_delay * (2 ** attempt)
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
        self, page: pymupdf.Page, question_xml: str, image_dir: Path
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
            unique_tokens = list(set(crop_tokens))

            for token in unique_tokens:
                ymin, xmin, ymax, xmax = map(int, token)
                cropped_bytes = crop_diagram_from_page(
                    page, ymin, xmin, ymax, xmax, dpi=self.dpi, padding_cm=self.padding_cm
                )

                if not cropped_bytes:
                    continue

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

            if file_nodes_to_inject:
                files_block = "\n" + "\n".join(file_nodes_to_inject) + "\n"
                return prefix + files_block + suffix

            return prefix + suffix

        question_xml = re.sub(block_pattern, block_replacer, question_xml, flags=re.IGNORECASE | re.DOTALL)
        return question_xml


def parse_args_and_config() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract Moodle XML banks from Question Paper PDFs using Context-Aware Chat.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # File & Directory CLI Arguments
    parser.add_argument("-c", "--config-file", type=Path, help="Path to JSON config file.")
    parser.add_argument("-i", "--input-dir", type=Path, help="Directory containing exam paper PDFs.")
    parser.add_argument("-o", "--output-dir", type=Path, help="Output directory.")
    parser.add_argument("-p", "--prompt", type=Path, help="Path to prompt markdown file.")
    
    # Core Prompt Rules CLI Arguments
    parser.add_argument("--xml-rules", type=Path, help="Path to moodle_xml_rules.md.")
    parser.add_argument("--tags-rules", type=Path, help="Path to naming_and_tags_rules.md.")
    parser.add_argument("--templates", type=Path, help="Path to moodle_xml_templates.md.")
    parser.add_argument("--template", dest="templates", type=Path, help="Alias of --templates.")

    # Exam Context Arguments
    parser.add_argument("-l", "--languages", type=str, help="Comma-separated target languages.")
    parser.add_argument("--instruction-file", type=Path, help="Standalone instruction/chapter markdown file.")
    parser.add_argument("--instruction-page", type=int, help="PDF page containing instructions (default: 1).")
    parser.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"), help="Process specific page range.")
    parser.add_argument("-s", "--standards", type=str, help="Target standards.")
    parser.add_argument("-t", "--tags", type=str, help="Global tags.")

    # Engine Configuration Arguments
    parser.add_argument("-m", "--model", type=str, help="Gemini model.")
    parser.add_argument("--memory-span", type=int, help="Pages memory (default: 3).")
    parser.add_argument("--temperature", type=float, help="Sampling temperature.")
    parser.add_argument(
        "--thinking-level",
        choices=["minimal", "low", "medium", "high"],
        help="Thinking level for supported models (Flash supports minimal, low, medium, high).",
    )
    
    # Timing & Performance Options
    parser.add_argument("--padding-cm", type=float, help="Diagram crop padding in centimeters.")
    parser.add_argument("--rate-limit-delay", type=float, help="Inter-request delay in seconds between pages.")
    parser.add_argument("--retry-base-delay", type=float, help="Base delay in seconds for API error retries.")
    parser.add_argument("--attempt-limit", type=int, help="Maximum retry attempts per page.")
    parser.add_argument("--dpi", type=int, help="DPI resolution.")
    parser.add_argument("--zoom", type=float, help="Zoom factor for enhanced image rendering (default: 1.0).")
    parser.add_argument("--enhance", action="store_true", help="Apply contrast and sharpness enhancements to page images.")

    # System Prompt Caching Options
    parser.add_argument("--cache-prompt", action="store_true", help="Cache system prompt file.")
    parser.add_argument("--refresh-prompt-cache", action="store_true", help="Force recreate prompt cache.")
    parser.add_argument("--prompt-ttl", type=int, help="Prompt Cache TTL in seconds.")

    # Verification Controls
    parser.add_argument("--verify-online", action="store_true", help="Ask model to verify answers via Google Search tool before finalizing.")

    # Logging & Auth
    parser.add_argument("--log-file", type=Path, help="Path to write rotated log file.")
    parser.add_argument("--api-key", type=str, default=os.getenv("GEMINI_API_KEY"), help="API Key.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logs.")

    args = parser.parse_args()

    config_data = {}
    if args.config_file and args.config_file.exists():
        try:
            with open(args.config_file, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception as e:
            print(f"Error reading config file {args.config_file}: {e}", file=sys.stderr)

    resolved = argparse.Namespace()
    
    resolved.input_dir = args.input_dir or Path(config_data.get("input_dir", "./pdfs"))
    if args.output_dir is not None:
        resolved.output_dir = args.output_dir
    elif "output_dir" in config_data:
        resolved.output_dir = Path(config_data["output_dir"])
    else:
        resolved.output_dir = resolved.input_dir
    resolved.prompt = args.prompt or Path(config_data.get("prompt", "./prompts/extractor/neet.md"))

    resolved.xml_rules = args.xml_rules or (Path(config_data["xml_rules"]) if "xml_rules" in config_data else None)
    resolved.tags_rules = args.tags_rules or (Path(config_data["tags_rules"]) if "tags_rules" in config_data else None)
    resolved.templates = args.templates or (Path(config_data["templates"]) if "templates" in config_data else None)

    langs_val = args.languages or config_data.get("languages", "english")
    if isinstance(langs_val, str):
        resolved.languages = [l.strip() for l in langs_val.split(",") if l.strip()]
    else:
        resolved.languages = langs_val

    inst_file_val = args.instruction_file or config_data.get("instruction_file")
    resolved.instruction_file = Path(inst_file_val) if inst_file_val else None

    resolved.instruction_page = args.instruction_page if args.instruction_page is not None else config_data.get("instruction_page", 1)

    page_range_val = args.page_range or config_data.get("page_range")
    resolved.page_range = tuple(page_range_val) if page_range_val else None

    resolved.standards = args.standards or config_data.get("standards", "NEET")
    resolved.tags = args.tags or config_data.get("tags", "")

    resolved.model = args.model or config_data.get("model", "gemini-3.6-flash")
    resolved.memory_span = args.memory_span if args.memory_span is not None else config_data.get("memory_span", 3)
    resolved.temperature = args.temperature if args.temperature is not None else config_data.get("temperature", 0.1)
    resolved.thinking_level = args.thinking_level or config_data.get("thinking_level")
    
    resolved.padding_cm = args.padding_cm if args.padding_cm is not None else config_data.get("padding_cm", 0.5)
    resolved.rate_limit_delay = args.rate_limit_delay if args.rate_limit_delay is not None else config_data.get("rate_limit_delay", 4.0)
    resolved.retry_base_delay = args.retry_base_delay if args.retry_base_delay is not None else config_data.get("retry_base_delay", 4.0)
    resolved.attempt_limit = args.attempt_limit if args.attempt_limit is not None else config_data.get("attempt_limit", 10)
    resolved.dpi = args.dpi if args.dpi is not None else config_data.get("dpi", 200)
    resolved.zoom = args.zoom if args.zoom is not None else config_data.get("zoom", 1.0)
    resolved.enhance = args.enhance or config_data.get("enhance", False)

    resolved.cache_prompt = args.cache_prompt or config_data.get("cache_prompt", False)
    resolved.refresh_prompt_cache = args.refresh_prompt_cache or config_data.get("refresh_prompt_cache", False)
    resolved.prompt_ttl = args.prompt_ttl if args.prompt_ttl is not None else config_data.get("prompt_ttl", 86400)
    resolved.verify_online = args.verify_online or config_data.get("verify_online", False)

    resolved.log_file = args.log_file or (Path(config_data["log_file"]) if "log_file" in config_data else (resolved.output_dir / "paper2moodle.log"))
    resolved.api_key = args.api_key or config_data.get("api_key")
    resolved.verbose = args.verbose or config_data.get("verbose", False)

    return resolved


def main() -> None:
    args = parse_args_and_config()
    setup_logger(log_file=args.log_file, verbose=args.verbose)

    if not args.api_key:
        logger.critical("GEMINI_API_KEY is missing. Set GEMINI_API_KEY env var or pass --api-key.")
        sys.exit(1)

    client = genai.Client(api_key=args.api_key)
    
    prompt_text = load_combined_prompt(
        main_prompt_path=args.prompt,
        xml_rules_path=args.xml_rules,
        tags_rules_path=args.tags_rules,
        templates_path=args.templates,
    )

    instruction_text = load_file_content(args.instruction_file)

    cached_prompt_name = None
    if args.cache_prompt:
        cached_prompt_name = get_or_create_prompt_cache(
            client=client,
            prompt_path=args.prompt,
            prompt_text=prompt_text,
            model_name=args.model,
            ttl_seconds=args.prompt_ttl,
            force_refresh=args.refresh_prompt_cache,
        )

    extractor = StatefulPaperExtractor(
        client=client,
        prompt_text=prompt_text,
        standards=[s.strip() for s in args.standards.split(",") if s.strip()],
        tags=[t.strip() for t in args.tags.split(",") if t.strip()],
        languages=args.languages,
        memory_span=args.memory_span,
        model_name=args.model,
        dpi=args.dpi,
        zoom=args.zoom,
        enhance=args.enhance,
        rate_limit_delay=args.rate_limit_delay,
        retry_base_delay=args.retry_base_delay,
        attempt_limit=args.attempt_limit,
        padding_cm=args.padding_cm,
        temperature=args.temperature,
        thinking_level=args.thinking_level,
        instruction_text=instruction_text,
        instruction_page=args.instruction_page,
        page_range=args.page_range,
        cached_prompt_name=cached_prompt_name,
        verify_online=args.verify_online,
    )

    if not args.input_dir.exists():
        logger.error(f"Input directory missing: {args.input_dir}")
        sys.exit(1)

    pdf_files = sorted([f for f in args.input_dir.iterdir() if f.suffix.lower() == ".pdf"])
    for pdf_path in pdf_files:
        extractor.process_pdf(pdf_path, args.output_dir)

if __name__ == "__main__":
    main()