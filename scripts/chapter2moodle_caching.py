#!/usr/bin/env python3
"""
Chapter to Moodle XML Question Generator CLI (Explicit Context Caching Edition)

Uploads entire textbook chapter PDFs to Google Context Cache and generates
new Moodle XML question banks page-by-page at 90% lower token costs.
Supports --reuse-cache to prevent re-uploading identical files within the TTL window.
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


class CachedChapterGenerator:
    """Manages server-side PDF caching and question generation queries."""

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
        ttl_seconds: int = 3600,
        model_name: str = "gemini-2.5-pro",
        dpi: int = 150,
        rate_limit_delay: float = 4.0,
        reuse_cache: bool = False,
    ):
        self.client = client
        self.prompt_text = prompt_text
        self.num_questions = num_questions
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

    def process_pdf(self, pdf_path: Path, output_dir: Path) -> bool:
        pdf_stem = pdf_path.stem
        pdf_work_dir = output_dir / pdf_stem
        image_output_dir = pdf_work_dir / "extracted_assets"
        xml_output_path = pdf_work_dir / f"{pdf_stem}_moodle_generated_bank.xml"
        cache_display_name = f"cache-{pdf_stem}"

        logger.info(f"Starting Context Caching generation pipeline for: {pdf_path.name}")
        image_output_dir.mkdir(parents=True, exist_ok=True)

        uploaded_file = None
        cache = None

        try:
            # 1. Check for existing cache if --reuse-cache is enabled
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

            # 2. Upload and create cache ONLY if no reusable cache was found
            if not cache:
                logger.info(f"[{pdf_stem}] ☁️  Uploading PDF to Google GenAI storage...")
                uploaded_file = self.client.files.upload(file=pdf_path)
                
                while uploaded_file.state.name == "PROCESSING":
                    time.sleep(2)
                    uploaded_file = self.client.files.get(name=uploaded_file.name)

                if uploaded_file.state.name == "FAILED":
                    logger.error(f"[{pdf_stem}] Server-side file processing failed.")
                    return False

                logger.info(f"[{pdf_stem}] 📦 Creating server-side context cache (TTL: {self.ttl_seconds}s)...")
                cache = self.client.caches.create(
                    model=self.model_name,
                    config=types.CreateCachedContentConfig(
                        contents=[uploaded_file],
                        system_instruction=self.prompt_text,
                        display_name=cache_display_name,
                        ttl=f"{self.ttl_seconds}s",
                    ),
                )

            # 3. Process Pages Page-by-Page
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
                logger.info(f"[{pdf_stem}] Page {current_page}: Generated {len(processed_page_questions)} question(s).")
                
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

            xml_output_path.write_text(final_xml, encoding="utf-8")
            logger.info(f"✅ Successfully written {len(all_questions_xml)} questions to: {xml_output_path}")
            return True

        except Exception as e:
            logger.error(f"[{pdf_stem}] Fatal pipeline error: {e}", exc_info=True)
            return False

        finally:
            # 4. Clean up ONLY if --reuse-cache is NOT set
            if not self.reuse_cache:
                logger.info(f"[{pdf_stem}] 🧹 Cleaning up server-side cache and files...")
                try:
                    if cache:
                        self.client.caches.delete(name=cache.name)
                    if uploaded_file:
                        self.client.files.delete(name=uploaded_file.name)
                except Exception as cleanup_err:
                    logger.warning(f"Cleanup warning: {cleanup_err}")
            else:
                logger.info(f"[{pdf_stem}] 📌 Cache '{cache_display_name}' kept active on Google servers for reuse.")

    def _build_turn_prompt(self, current_page_num: int) -> str:
        formatted_standards = ", ".join(self.standards) if self.standards else "General"
        
        tag_xml_nodes = [f"      <tag><text>{t}</text></tag>" for t in self.tags]
        for std in self.standards:
            tag_xml_nodes.append(f"      <tag><text>standard:{std}</text></tag>")
        tags_block = "\n".join(tag_xml_nodes) if tag_xml_nodes else "      <tag><text>generated:ai</text></tag>"

        return (
            f"=== PAGE {current_page_num} GENERATION ===\n"
            f"Focus specifically on **Page {current_page_num}** of the cached chapter. "
            f"Generate exactly {self.num_questions} original question(s) based on concepts on this page.\n\n"
            f"MARKING SCHEME INSTRUCTION:\n"
            f"- If section instructions exist, derive `<defaultgrade>`, `<penalty>`, and option `fraction=\"...\"` directly from them.\n"
            f"- Otherwise, use the fallback defaults provided below.\n\n"
            f"1. Target Exam Standard(s): {formatted_standards}\n"
            f"2. Default Question Grade: <defaultgrade>{self.default_grade}</defaultgrade>\n"
            f"3. Incorrect Answer Penalty: <penalty>{self.penalty}</penalty>\n"
            f"4. Incorrect Answer Fraction: fraction=\"{self.negative_fraction}\"\n"
            f"5. Required Global Tags:\n"
            f"    <tags>\n"
            f"{tags_block}\n"
            f"    </tags>\n\n"
            f"=== STRICT SILENCE DIRECTIVE ===\n"
            f"If Page {current_page_num} contains no academic concepts, return an empty string (\"\"). Output ONLY valid <question> XML nodes."
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
                    config=types.GenerateContentConfig(
                        cached_content=cache_name,
                        temperature=0.4,
                    ),
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
    parser = argparse.ArgumentParser(description="Generate Moodle XML questions from textbook PDFs using Explicit Context Caching.")
    parser.add_argument("-i", "--input-dir", type=Path, required=True, help="Directory containing textbook PDFs.")
    parser.add_argument("-o", "--output-dir", type=Path, required=True, help="Output directory.")
    parser.add_argument("-p", "--prompt", type=Path, required=True, help="Path to system instruction prompt markdown file.")
    
    # NEW DYNAMIC PLACEHOLDER ARGUMENTS
    parser.add_argument("--exam", type=str, required=True, choices=["NEET", "JEE Main", "JEE Advanced", "WBJEE", "CBSE"], help="Target exam standard (e.g. NEET, JEE Main).")
    parser.add_argument("--difficulty", type=str, required=True, choices=["Easy", "Medium", "Hard"], help="Target difficulty level (Easy, Medium, Hard).")

    parser.add_argument("-n", "--num-questions", type=int, default=2, help="Number of questions per page (default: 2).")
    parser.add_argument("-s", "--standards", type=str, default="JEE-Main", help="Comma-separated standards (default: JEE-Main).")
    parser.add_argument("--ttl", type=int, default=3600, help="Cache TTL in seconds (default: 3600).")
    parser.add_argument("--default-grade", type=float, default=4.0, help="Default grade (default: 4.0).")
    parser.add_argument("--penalty", type=float, default=0.25, help="Penalty (default: 0.25).")
    parser.add_argument("--negative-fraction", type=int, default=-25, help="Negative fraction (default: -25).")
    parser.add_argument("-t", "--tags", type=str, default="", help="Global tags.")
    parser.add_argument("-m", "--model", type=str, default="gemini-2.5-pro", help="Gemini model.")
    parser.add_argument("--api-key", type=str, default=os.getenv("GEMINI_API_KEY"), help="API Key.")
    parser.add_argument("--dpi", type=int, default=150, help="Cropping DPI.")
    parser.add_argument("--reuse-cache", action="store_true", help="Check for and reuse existing server-side cache if available (skips PDF re-upload).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logs.")

    args = parser.parse_args()
    setup_logger(args.verbose)

    if not args.api_key:
        logger.critical("No API key provided.")
        sys.exit(1)

    client = genai.Client(api_key=args.api_key)
    
    # INJECT DYNAMIC EXAM & DIFFICULTY PLACEHOLDERS HERE
    prompt_text = load_prompt(args.prompt)
    prompt_text = prompt_text.replace("{{TARGET_EXAM}}", args.exam)
    prompt_text = prompt_text.replace("{{TARGET_DIFFICULTY}}", args.difficulty)

    generator = CachedChapterGenerator(
        client=client,
        prompt_text=prompt_text,
        num_questions=args.num_questions,
        standards=[s.strip() for s in args.standards.split(",") if s.strip()],
        default_grade=args.default_grade,
        penalty=args.penalty,
        negative_fraction=args.negative_fraction,
        tags=[t.strip() for t in args.tags.split(",") if t.strip()],
        ttl_seconds=args.ttl,
        model_name=args.model,
        dpi=args.dpi,
        reuse_cache=args.reuse_cache,
    )

    pdf_files = sorted([f for f in args.input_dir.iterdir() if f.suffix.lower() == ".pdf"])
    for pdf_path in pdf_files:
        generator.process_pdf(pdf_path, args.output_dir)


if __name__ == "__main__":
    main()