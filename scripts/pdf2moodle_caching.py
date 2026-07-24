#!/usr/bin/env python3
"""
Question Paper to Moodle XML Extractor CLI (Explicit Context Caching Edition)

Uploads entire Question Paper PDFs to Google's GenAI server-side Context Cache once.
Queries the cache page-by-page to extract complete MCQs and Numerical questions.
Supports --reuse-cache and Python code execution inside cached content.
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


class CachedPaperExtractor:
    """Manages server-side PDF caching and question extraction queries."""

    def __init__(
        self,
        client: genai.Client,
        prompt_text: str,
        standards: List[str],
        default_grade: float,
        penalty: float,
        negative_fraction: int,
        tags: List[str],
        ttl_seconds: int = 3600,
        model_name: str = "gemini-2.0-flash-thinking-exp",
        dpi: int = 200,
        rate_limit_delay: float = 4.0,
        reuse_cache: bool = False,
        temperature: float = 0.1,
        thinking_level: Optional[str] = None,
        thinking_budget: Optional[int] = None,
        include_thoughts: bool = False,
        enable_code_execution: bool = False,
    ):
        self.client = client
        self.prompt_text = prompt_text
        self.standards = standards
        self.default_grade = default_grade
        self.penalty = penalty
        self.negative_fraction = negative_fraction
        self.tags = tags
        self.ttl_seconds = ttl_seconds
        self.model_name = model_name
        self.dpi = dpi
        self.rate_limit_delay = rate_limit_delay
        self.reuse_cache = reuse_cache
        self.temperature = temperature
        self.thinking_level = thinking_level
        self.thinking_budget = thinking_budget
        self.include_thoughts = include_thoughts
        self.enable_code_execution = enable_code_execution

    def _build_generation_config(self, cached_content: Optional[str] = None) -> types.GenerateContentConfig:
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

        # STRICT API RULE: Do NOT pass tools or system_instruction when cached_content is used!
        return types.GenerateContentConfig(
            cached_content=cached_content,
            temperature=self.temperature,
            thinking_config=thinking_config,
        )

    def process_pdf(self, pdf_path: Path, output_dir: Path) -> bool:
        pdf_stem = pdf_path.stem
        pdf_work_dir = output_dir / pdf_stem
        image_output_dir = pdf_work_dir / "extracted_assets"
        xml_output_path = pdf_work_dir / f"{pdf_stem}_moodle_extracted_bank.xml"
        cache_display_name = f"cache-{pdf_stem}"

        logger.info(f"Starting Context Caching extraction pipeline for: {pdf_path.name}")
        image_output_dir.mkdir(parents=True, exist_ok=True)

        uploaded_file = None
        cache = None

        try:
            if self.reuse_cache:
                try:
                    logger.info(f"[{pdf_stem}] 🔍 Checking for existing server-side cache ('{cache_display_name}')...")
                    for active_cache in self.client.caches.list():
                        if getattr(active_cache, "display_name", None) == cache_display_name:
                            cache = active_cache
                            logger.info(f"[{pdf_stem}] ⚡ Found existing cache ({cache.name})! Reusing without re-uploading.")
                            break
                except Exception as cache_err:
                    logger.warning(f"Failed to query server caches: {cache_err}")

            if not cache:
                logger.info(f"[{pdf_stem}] ☁️  Uploading PDF to Google GenAI storage...")
                uploaded_file = self.client.files.upload(file=pdf_path)
                
                while uploaded_file.state.name == "PROCESSING":
                    time.sleep(2)
                    uploaded_file = self.client.files.get(name=uploaded_file.name)

                if uploaded_file.state.name == "FAILED":
                    logger.error(f"[{pdf_stem}] Server-side file processing failed.")
                    return False

                # Embed tools directly into CreateCachedContentConfig
                tools = None
                if self.enable_code_execution:
                    logger.info("🛠️ Enabling Python Code Execution inside PDF Context Cache...")
                    tools = [types.Tool(code_execution=types.ToolCodeExecution())]

                logger.info(f"[{pdf_stem}] 📦 Creating server-side context cache (TTL: {self.ttl_seconds}s)...")
                cache = self.client.caches.create(
                    model=self.model_name,
                    config=types.CreateCachedContentConfig(
                        contents=[uploaded_file],
                        system_instruction=self.prompt_text,
                        display_name=cache_display_name,
                        ttl=f"{self.ttl_seconds}s",
                        tools=tools,
                    ),
                )

            doc = fitz.open(pdf_path)
            total_pages = len(doc)
            all_questions_xml: List[str] = []

            for page_num in range(total_pages):
                current_page = page_num + 1
                logger.info(f"[{pdf_stem}] Querying Cache for Page {current_page}/{total_pages}...")

                turn_prompt = self._build_turn_prompt(current_page)
                ai_output = self._query_cache_with_retry(
                    prompt=turn_prompt,
                    cache_name=cache.name,
                    attempt_limit=5
                )

                if not ai_output:
                    continue

                extracted_questions = self._extract_clean_question_nodes(ai_output)
                if not extracted_questions:
                    continue

                processed_page_questions = []
                for question_xml in extracted_questions:
                    processed_xml = self._process_diagram_tokens_in_question(
                        page=doc[page_num],
                        question_xml=question_xml,
                        image_dir=image_output_dir
                    )
                    processed_page_questions.append(processed_xml)

                all_questions_xml.extend(processed_page_questions)
                logger.info(f"[{pdf_stem}] Page {current_page}: Extracted {len(processed_page_questions)} question(s).")
                
                time.sleep(self.rate_limit_delay)

            doc.close()

            if not all_questions_xml:
                logger.error(f"[{pdf_stem}] No valid questions extracted. Aborting.")
                return False

            final_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<quiz>\n"
                f'{chr(10).join(all_questions_xml)}\n'
                "</quiz>"
            )

            xml_output_path.write_text(final_xml, encoding="utf-8")
            logger.info(f"✅ Successfully written {len(all_questions_xml)} questions to: {xml_output_path}")
            return True

        except Exception as e:
            logger.error(f"[{pdf_stem}] Fatal pipeline error: {e}", exc_info=True)
            return False

        finally:
            if not self.reuse_cache:
                logger.info(f"[{pdf_stem}] 🧹 Cleaning up server-side cache and files...")
                try:
                    if cache:
                        self.client.caches.delete(name=cache.name)
                    if uploaded_file:
                        self.client.files.delete(name=uploaded_file.name)
                except Exception as cleanup_err:
                    logger.warning(f"Cleanup warning: {cleanup_err}")

    def _build_turn_prompt(self, current_page_num: int) -> str:
        formatted_standards = ", ".join(self.standards) if self.standards else "General"
        tag_xml_nodes = [f"      <tag><text>{t}</text></tag>" for t in self.tags]
        for std in self.standards:
            tag_xml_nodes.append(f"      <tag><text>standard:{std}</text></tag>")
        tags_block = "\n".join(tag_xml_nodes) if tag_xml_nodes else "      <tag><text>extracted:ai</text></tag>"

        return (
            f"=== PAGE {current_page_num} EXTRACTION ===\n"
            f"Focus specifically on **Page {current_page_num}** of the cached question paper. "
            f"Extract ALL complete questions that conclude on Page {current_page_num}.\n"
            f"If a question starts here but ends on the next page, DEFER it.\n\n"
            f"1. Target Exam Standard(s): {formatted_standards}\n"
            f"2. Required Global Tags:\n"
            f"    <tags>\n"
            f"{tags_block}\n"
            f"    </tags>\n\n"
            f"=== STRICT SILENCE DIRECTIVE ===\n"
            f"If Page {current_page_num} contains no concluding questions, return an empty string (\"\"). Output ONLY valid <question> XML nodes."
        )

    def _query_cache_with_retry(
        self, prompt: str, cache_name: str, attempt_limit: int = 5
    ) -> Optional[str]:
        base_delay = 5.0
        for attempt in range(attempt_limit):
            try:
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                    config=self._build_generation_config(cached_content=cache_name),
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
    parser = argparse.ArgumentParser(description="Extract Moodle XML banks from Question Paper PDFs using Explicit Context Caching.")
    parser.add_argument("-i", "--input-dir", type=Path, required=True, help="Directory containing exam paper PDFs.")
    parser.add_argument("-o", "--output-dir", type=Path, required=True, help="Output directory.")
    parser.add_argument("-p", "--prompt", type=Path, required=True, help="Path to prompt markdown file.")
    parser.add_argument("-s", "--standards", type=str, default="JEE-Main", help="Target standards.")
    parser.add_argument("--ttl", type=int, default=3600, help="PDF Cache TTL in seconds (default: 3600).")
    parser.add_argument("--default-grade", type=float, default=4.0, help="Default grade.")
    parser.add_argument("--penalty", type=float, default=0.25, help="Penalty.")
    parser.add_argument("--negative-fraction", type=int, default=-25, help="Negative fraction.")
    parser.add_argument("-t", "--tags", type=str, default="", help="Global tags.")
    parser.add_argument("-m", "--model", type=str, default="gemini-2.0-flash-thinking-exp", help="Gemini model.")
    parser.add_argument("--temperature", type=float, default=0.1, help="Sampling temperature (default: 0.1).")

    parser.add_argument("--thinking-level", type=str, choices=["minimal", "low", "medium", "high"], default=None, help="Thinking level.")
    parser.add_argument("--thinking-budget", type=int, default=None, help="Thinking budget in tokens.")
    parser.add_argument("--include-thoughts", action="store_true", help="Include thought traces.")
    parser.add_argument("--code-execution", action="store_true", help="Enable Python Code Execution tool.")
    parser.add_argument("--api-key", type=str, default=os.getenv("GEMINI_API_KEY"), help="API Key.")
    parser.add_argument("--dpi", type=int, default=200, help="DPI resolution (default: 200).")
    parser.add_argument("--reuse-cache", action="store_true", help="Reuse existing server-side PDF cache.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logs.")

    args = parser.parse_args()
    setup_logger(args.verbose)
    log_cli_args(args)

    if not args.api_key:
        logger.critical("No API key provided.")
        sys.exit(1)

    client = genai.Client(api_key=args.api_key)
    prompt_text = load_prompt(args.prompt)

    extractor = CachedPaperExtractor(
        client=client,
        prompt_text=prompt_text,
        standards=[s.strip() for s in args.standards.split(",") if s.strip()],
        default_grade=args.default_grade,
        penalty=args.penalty,
        negative_fraction=args.negative_fraction,
        tags=[t.strip() for t in args.tags.split(",") if t.strip()],
        ttl_seconds=args.ttl,
        model_name=args.model,
        dpi=args.dpi,
        reuse_cache=args.reuse_cache,
        temperature=args.temperature,
        thinking_level=args.thinking_level,
        thinking_budget=args.thinking_budget,
        include_thoughts=args.include_thoughts,
        enable_code_execution=args.code_execution,
    )

    pdf_files = sorted([f for f in args.input_dir.iterdir() if f.suffix.lower() == ".pdf"])
    for pdf_path in pdf_files:
        extractor.process_pdf(pdf_path, args.output_dir)


if __name__ == "__main__":
    main()