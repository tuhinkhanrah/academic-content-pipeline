#!/usr/bin/env python3
"""
pdf2moodle_agent.py - Flexible Moodle XML Extractor powered by Antigravity Agent.

Key Features:
  - Validated payload schema for client.interactions.create (agent key + string system_instruction)
  - Out-of-bounds instruction handling (e.g., page_range=[5, 10] with instruction_page=1)
  - Standalone instruction/chapter markdown file support (--instruction-file)
  - Config file input (JSON) with explicit CLI argument overrides
  - Configurable automatic diagram crop padding in cm (--padding-cm)
  - Configurable inter-turn delay (--rate-limit-delay) and exponential backoff (--retry-base-delay)
  - Configurable history reset interval (--context-reset-interval)
"""

import argparse
import base64
import json
import logging
import os
import re
import sys
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

import fitz  # PyMuPDF
from google import genai
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


def load_file_content(file_path: Optional[Path]) -> str:
    if not file_path or not file_path.exists():
        return ""
    try:
        return file_path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.error(f"Failed to read file {file_path}: {e}")
        return ""


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

    # Convert 0-1000 scale to PDF points
    x0 = (xmin / 1000.0) * page_width
    y0 = (ymin / 1000.0) * page_height
    x1 = (xmax / 1000.0) * page_width
    y1 = (ymax / 1000.0) * page_height

    # Convert padding from cm to PDF points (1 cm ≈ 28.3465 pt)
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


class ManagedAgentPaperExtractor:
    def __init__(
        self,
        client: genai.Client,
        prompt_text: str,
        standards: List[str],
        tags: List[str],
        agent_name: str = "antigravity-preview-05-2026",
        dpi: int = 150,
        rate_limit_delay: float = 3.0,
        retry_base_delay: float = 4.0,
        context_reset_interval: int = 4,
        padding_cm: float = 0.5,
        instruction_text: str = "",
        instruction_page: int = 1,
        page_range: Optional[Tuple[int, int]] = None,
    ):
        self.client = client
        self.prompt_text = prompt_text
        self.standards = standards
        self.tags = tags
        self.agent_name = agent_name
        self.dpi = dpi
        self.rate_limit_delay = rate_limit_delay
        self.retry_base_delay = retry_base_delay
        self.context_reset_interval = context_reset_interval
        self.padding_cm = padding_cm
        self.instruction_text = instruction_text
        self.instruction_page = instruction_page
        self.page_range = page_range

    def process_pdf(self, pdf_path: Path, output_dir: Path) -> bool:
        pdf_stem = pdf_path.stem
        pdf_work_dir = output_dir / pdf_stem
        image_output_dir = pdf_work_dir / "extracted_assets"
        xml_output_path = pdf_work_dir / f"{pdf_stem}_moodle_extracted_bank.xml"

        logger.info(f"🚀 Processing paper: {pdf_path.name}")
        image_output_dir.mkdir(parents=True, exist_ok=True)

        try:
            doc = fitz.open(pdf_path)
        except Exception as e:
            logger.error(f"Failed to open PDF {pdf_path}: {e}")
            return False

        total_pdf_pages = len(doc)

        # Determine target page window
        start_page = 1
        end_page = total_pdf_pages

        if self.page_range:
            start_page = max(1, self.page_range[0])
            end_page = min(total_pdf_pages, self.page_range[1])

        logger.info(
            f"[{pdf_stem}] Target page range: {start_page} to {end_page} (Total PDF Pages: {total_pdf_pages})"
        )

        # Extract instruction page text upfront if it lies outside target page_range
        out_of_bounds_instruction_text = ""
        if (
            self.instruction_page > 0
            and self.instruction_page <= total_pdf_pages
            and (self.instruction_page < start_page or self.instruction_page > end_page)
        ):
            logger.info(
                f"[{pdf_stem}] Instruction page ({self.instruction_page}) is outside target page range "
                f"({start_page}-{end_page}). Pre-extracting its content for context..."
            )
            inst_page_obj = doc[self.instruction_page - 1]
            out_of_bounds_instruction_text = inst_page_obj.get_text("text").strip()

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
            page_img_bytes = pix.tobytes("png")

            # Prune conversation history periodically to avoid token accumulation
            if processed_count > 1 and (processed_count - 1) % self.context_reset_interval == 0:
                logger.info(f"[{pdf_stem}] Pruning turn history to keep session lean...")
                last_interaction_id = None

            is_instruction_page = (page_idx == self.instruction_page)
            is_first_target_page = (page_idx == start_page)

            turn_prompt = self._build_turn_prompt(
                current_page_num=page_idx,
                is_instruction_page=is_instruction_page,
                is_first_target_page=is_first_target_page,
                out_of_bounds_instruction_text=out_of_bounds_instruction_text
                if is_first_target_page
                else "",
            )

            interaction = self._send_interaction_with_retry(
                page_img_bytes=page_img_bytes,
                prompt=turn_prompt,
                environment_id=environment_id,
                previous_interaction_id=last_interaction_id,
            )

            if not interaction or not getattr(interaction, "output_text", None):
                logger.warning(f"[{pdf_stem}] Page {page_idx}: No response returned. Skipping.")
                continue

            environment_id = interaction.environment_id
            last_interaction_id = interaction.id

            ai_output = interaction.output_text
            extracted_questions = self._extract_clean_question_nodes(ai_output)

            if not extracted_questions:
                logger.info(f"[{pdf_stem}] Page {page_idx}: No completed questions found.")
                continue

            processed_page_questions = []
            for question_xml in extracted_questions:
                processed_xml = self._process_diagram_tokens_in_question(
                    page=page,
                    question_xml=question_xml,
                    image_dir=image_output_dir,
                )
                processed_page_questions.append(processed_xml)

            all_questions_xml.extend(processed_page_questions)
            logger.info(
                f"[{pdf_stem}] Page {page_idx}: Extracted {len(processed_page_questions)} question(s)."
            )

            time.sleep(self.rate_limit_delay)

        doc.close()

        if not all_questions_xml:
            logger.error(f"[{pdf_stem}] Extraction failed across target pages.")
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

    def _build_turn_prompt(
        self,
        current_page_num: int,
        is_instruction_page: bool,
        is_first_target_page: bool,
        out_of_bounds_instruction_text: str = "",
    ) -> str:
        formatted_standards = ", ".join(self.standards) if self.standards else "General"
        tags_block = "\n".join([f"      <tag><text>{t}</text></tag>" for t in self.tags])

        instruction_block = ""
        if self.instruction_text:
            instruction_block = (
                f"=== EXTERNAL INSTRUCTIONS / CHAPTER GUIDELINES ===\n"
                f"{self.instruction_text}\n\n"
            )

        page_instruction_marker = ""
        if is_instruction_page:
            page_instruction_marker = (
                f"NOTE: Page {current_page_num} is designated as the front-page instruction page. "
                f"Parse general marking rules and guidelines before extracting questions.\n\n"
            )
        elif is_first_target_page and out_of_bounds_instruction_text:
            page_instruction_marker = (
                f"=== FRONT-PAGE INSTRUCTIONS (Extracted from Page {self.instruction_page}) ===\n"
                f"{out_of_bounds_instruction_text}\n"
                f"Use the above instructions for marking rules and exam context.\n\n"
            )

        return (
            f"=== PAGE {current_page_num} EXTRACTION ===\n"
            f"{instruction_block}"
            f"{page_instruction_marker}"
            f"Extract all completed questions ending on Page {current_page_num}.\n"
            f"Standards: {formatted_standards}\n"
            f"Global Tags:\n<tags>\n{tags_block}\n</tags>\n\n"
            f'Output ONLY valid <question> XML nodes. If no questions conclude here, return "".'
        )

    def _send_interaction_with_retry(
        self,
        page_img_bytes: bytes,
        prompt: str,
        environment_id: Optional[str] = None,
        previous_interaction_id: Optional[str] = None,
        attempt_limit: int = 5,
    ):
        b64_image = encode_bytes_to_base64(page_img_bytes)

        multimodal_input = [
            {"type": "image", "data": b64_image, "mime_type": "image/png"},
            {"type": "text", "text": prompt},
        ]

        for attempt in range(attempt_limit):
            try:
                env_param = environment_id if environment_id else "remote"

                kwargs = {
                    "agent": self.agent_name,  # Required by Interactions API schema
                    "agent_config": {
                        "type": "antigravity",
                        "model": "gemini-3.5-flash",
                    },
                    "environment": env_param,
                    "system_instruction": self.prompt_text,  # Must be string
                    "input": multimodal_input,
                    "tools": [
                        {"type": "code_execution"},
                        {"type": "google_search"},
                    ],
                }

                if previous_interaction_id:
                    kwargs["previous_interaction_id"] = previous_interaction_id

                return self.client.interactions.create(**kwargs)

            except APIError as e:
                if e.code in (429, 503):
                    delay = self.retry_base_delay * (2**attempt)
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
                logger.error(f"❌ Dropping malformed XML: {e}")
                continue
        return valid_nodes

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
        description="pdf2moodle_agent.py - Flexible Question Extractor for Moodle XML",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("-c", "--config-file", type=Path, help="Path to JSON config file.")
    parser.add_argument("-i", "--input-dir", type=Path, help="Input directory containing PDFs.")
    parser.add_argument("-o", "--output-dir", type=Path, help="Output directory for XML and images.")
    parser.add_argument("-p", "--prompt", type=Path, help="Path to system prompt markdown file.")
    parser.add_argument("--instruction-file", type=Path, help="Separate instruction/chapter markdown file.")
    parser.add_argument("--instruction-page", type=int, help="PDF page containing instructions (default: 1, 0 to disable).")
    parser.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"), help="Process specific page range (e.g. 10 15).")
    parser.add_argument("-s", "--standards", type=str, help="Comma-separated standards (e.g. JEE-Main,NEET).")
    parser.add_argument("-t", "--tags", type=str, help="Comma-separated global tags.")
    parser.add_argument("-m", "--agent", type=str, help="Antigravity agent model name.")
    parser.add_argument("--padding-cm", type=float, help="Diagram crop automatic padding in centimeters.")
    parser.add_argument("--rate-limit-delay", type=float, help="Inter-request delay in seconds between pages.")
    parser.add_argument("--retry-base-delay", type=float, help="Base delay in seconds for API error retries.")
    parser.add_argument("--context-reset-interval", type=int, help="Number of pages before resetting chat history.")
    parser.add_argument("--dpi", type=int, help="Page rendering DPI for vision processing.")
    parser.add_argument("--api-key", type=str, default=os.getenv("GEMINI_API_KEY"), help="API key (defaults to GEMINI_API_KEY env var).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging.")

    # Show help message automatically if script is executed without any arguments
    if len(sys.argv) == 1:
        parser.print_help()
        sys.exit(0)

    args = parser.parse_args()

    # Load defaults from config file if provided
    config_data = {}
    if args.config_file and args.config_file.exists():
        try:
            with open(args.config_file, "r", encoding="utf-8") as f:
                config_data = json.load(f)
        except Exception as e:
            print(f"Error reading config file {args.config_file}: {e}", file=sys.stderr)

    # Resolve hierarchy: CLI Arguments > Config File > Default Values
    resolved = argparse.Namespace()
    resolved.input_dir = args.input_dir or Path(config_data.get("input_dir", "./pdfs"))
    resolved.output_dir = args.output_dir or Path(config_data.get("output_dir", "./output"))
    resolved.prompt = args.prompt or Path(config_data.get("prompt", "./prompts/prompt.md"))

    inst_file_val = args.instruction_file or config_data.get("instruction_file")
    resolved.instruction_file = Path(inst_file_val) if inst_file_val else None

    resolved.instruction_page = (
        args.instruction_page
        if args.instruction_page is not None
        else config_data.get("instruction_page", 1)
    )

    page_range_val = args.page_range or config_data.get("page_range")
    resolved.page_range = tuple(page_range_val) if page_range_val else None

    resolved.standards = args.standards or config_data.get("standards", "JEE-Main")
    resolved.tags = args.tags or config_data.get("tags", "")
    resolved.agent = args.agent or config_data.get("agent", "antigravity-preview-05-2026")
    resolved.padding_cm = (
        args.padding_cm if args.padding_cm is not None else config_data.get("padding_cm", 0.5)
    )
    resolved.rate_limit_delay = (
        args.rate_limit_delay
        if args.rate_limit_delay is not None
        else config_data.get("rate_limit_delay", 3.0)
    )
    resolved.retry_base_delay = (
        args.retry_base_delay
        if args.retry_base_delay is not None
        else config_data.get("retry_base_delay", 4.0)
    )
    resolved.context_reset_interval = (
        args.context_reset_interval
        if args.context_reset_interval is not None
        else config_data.get("context_reset_interval", 4)
    )
    resolved.dpi = args.dpi if args.dpi is not None else config_data.get("dpi", 150)
    resolved.api_key = args.api_key or config_data.get("api_key")
    resolved.verbose = args.verbose or config_data.get("verbose", False)

    return resolved


def main() -> None:
    args = parse_args_and_config()
    setup_logger(args.verbose)

    if not args.api_key:
        logger.critical("API key missing. Set GEMINI_API_KEY environment variable or pass --api-key.")
        sys.exit(1)

    if not args.prompt.exists():
        logger.critical(f"System prompt file not found at: {args.prompt}")
        sys.exit(1)

    client = genai.Client(api_key=args.api_key)

    # Load system prompt directly as text string
    prompt_text = load_file_content(args.prompt)
    logger.info(f"Loaded system prompt ({len(prompt_text)} chars) from {args.prompt}")

    # Read separate instruction file content if provided
    instruction_text = load_file_content(args.instruction_file)
    if instruction_text:
        logger.info(f"Loaded separate instruction document from: {args.instruction_file}")

    extractor = ManagedAgentPaperExtractor(
        client=client,
        prompt_text=prompt_text,
        standards=[s.strip() for s in args.standards.split(",") if s.strip()],
        tags=[t.strip() for t in args.tags.split(",") if t.strip()],
        agent_name=args.agent,
        dpi=args.dpi,
        rate_limit_delay=args.rate_limit_delay,
        retry_base_delay=args.retry_base_delay,
        context_reset_interval=args.context_reset_interval,
        padding_cm=args.padding_cm,
        instruction_text=instruction_text,
        instruction_page=args.instruction_page,
        page_range=args.page_range,
    )

    pdf_files = sorted([f for f in args.input_dir.iterdir() if f.suffix.lower() == ".pdf"])
    for pdf_path in pdf_files:
        extractor.process_pdf(pdf_path, args.output_dir)


if __name__ == "__main__":
    main()