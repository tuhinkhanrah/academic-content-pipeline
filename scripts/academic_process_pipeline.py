#!/usr/bin/env python3
"""
academic_process_pipeline.py - Master Unified Academic Content Pipeline
Modes:
  1. extract          (Extract questions from PDFs via Mistral OCR + Gemini Chat)
  2. generate-chapter (Synthesize questions from chapter PDFs/MDs via Gemini Chat)
  3. generate-mock    (Synthesize full mock exams from JSON blueprints via Gemini Chat)
"""

import os
import sys
import re
import io
import gc
import json
import base64
import hashlib
import argparse
import subprocess
import httpx
from pathlib import Path
from PIL import Image, ImageEnhance
import pymupdf as fitz
from mistralai.client import Mistral
from google import genai
from google.genai import types

# ---------------------------------------------------------------------------
# 0. DISABLE PILLOW DECOMPRESSION BOMB LIMIT
# ---------------------------------------------------------------------------
Image.MAX_IMAGE_PIXELS = None

# ---------------------------------------------------------------------------
# 1. CLIENT SETUP & PROMPT ASSEMBLER
# ---------------------------------------------------------------------------
MISTRAL_KEY = os.environ.get("MISTRAL_API_KEY")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")

if not MISTRAL_KEY or not GEMINI_KEY:
    print("❌ Error: MISTRAL_API_KEY and GEMINI_API_KEY environment variables must be set.")
    sys.exit(1)

http_client = httpx.Client(verify=False, timeout=300.0)
mistral_client = Mistral(api_key=MISTRAL_KEY, client=http_client)
gemini_client = genai.Client(api_key=GEMINI_KEY)

def assemble_prompt_files(rule_files: dict, mode: str, output_format: str = "xml", pdf_engine: str = "html", verify_online: bool = False) -> str:
    """Combines specified prompt markdown files into a single context string."""
    content_blocks = ["# SYSTEM INSTRUCTIONS & PIPELINE RULES\n"]

    if output_format == "pdf":
        pdf_rule_key = "pdf_rules_tex" if pdf_engine == "tex" else "pdf_rules_html"
        valid_keys = ["main_prompt", "instruction_file", pdf_rule_key, "pdf_rules"]
    else:
        valid_keys = ["main_prompt", "instruction_file", "xml_rules", "tags_rules", "templates"]

    for name, filepath in rule_files.items():
        if name not in valid_keys or not filepath:
            continue
        path = Path(filepath)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
                if name == "xml_rules" and mode == "extract" and not verify_online:
                    content = re.sub(
                        r"\n# Mandatory Online Answer Verification\n.*?(?=\n# Feedback Rules & Reasoning Structure\n)",
                        "\n", content, flags=re.DOTALL,
                    )
                content_blocks.append(f"## File: {path.name}\n\n{content}\n\n---\n")
        else:
            print(f"⚠️ Warning: Prompt file '{filepath}' not found. Skipping.")

    # STRICT OVERRIDE FOR EXTRACTION MODE TO PREVENT SVG GENERATION
    if mode == "extract":
        content_blocks.append("""
## CRITICAL EXTRACTION OVERRIDE RULE FOR DIAGRAMS & IMAGES:
1. DO NOT GENERATE INLINE <svg> CODE IN EXTRACTION MODE!
2. All diagrams, circuits, graphs, and figures are provided as attached images with reference IDs (e.g., img-0.jpeg, img-1.jpeg).
3. Whenever a question or option refers to a visual diagram, circuit, graph, or figure, embed it using:
   <img src="@@PLUGINFILE@@/EXACT_IMAGE_ID.jpeg" />
4. DO NOT write <file> or Base64 payload tags! The local Python post-processor will read the local image files and inject the <file> Base64 nodes automatically.
---
""")

    return "\n".join(content_blocks)

# ---------------------------------------------------------------------------
# 2. HASHING, PDF SLICING & IMAGE ENHANCEMENT UTILITIES
# ---------------------------------------------------------------------------
def get_file_hash(filepath: Path) -> str:
    """Calculates SHA-256 hash to prevent redundant PDF uploads to Mistral."""
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(4096), b""):
            sha256.update(chunk)
    return sha256.hexdigest()

def slice_pdf_pages(pdf_path: Path, page_range: list, temp_dir: Path = Path("extracted_data/temp_sliced")) -> tuple[Path, bool]:
    """Extracts the entire specified page range from a PDF into a temporary file."""
    if not page_range:
        return pdf_path, False

    doc = fitz.open(pdf_path)
    start_page = max(0, page_range[0] - 1)
    end_page = min(len(doc) - 1, page_range[1] - 1)

    print(f"✂️ Slicing PDF pages {page_range[0]} to {page_range[1]} at once...")
    sliced_doc = fitz.open()
    sliced_doc.insert_pdf(doc, from_page=start_page, to_page=end_page)

    temp_dir.mkdir(parents=True, exist_ok=True)
    sliced_path = temp_dir / f"sliced_{start_page+1}_to_{end_page+1}_{pdf_path.name}"

    sliced_doc.save(str(sliced_path))
    sliced_doc.close()
    doc.close()
    return sliced_path, True

def enhance_extracted_images(output_dir, scale_factor=1.2, max_dim=2500):
    print(f"Enhancing extracted images ({scale_factor}x zoom, Max cap: {max_dim}px)...")
    for filename in os.listdir(output_dir):
        if filename.lower().endswith((".jpg", ".jpeg")):
            filepath = os.path.join(output_dir, filename)
            with Image.open(filepath) as img:
                w, h = img.size
                target_w, target_h = int(w * scale_factor), int(h * scale_factor)

                if max(target_w, target_h) > max_dim:
                    cap_scale = max_dim / float(max(w, h))
                    new_size = (int(w * cap_scale), int(h * cap_scale))
                else:
                    new_size = (target_w, target_h)

                img_scaled = img.resize(new_size, Image.Resampling.LANCZOS)
                img_final = ImageEnhance.Contrast(img_scaled).enhance(1.4)
                img_final.save(filepath, quality=92)

            del img_scaled, img_final
            gc.collect()

def run_mistral_ocr(pdf_path: Path, img_output_dir):
    """Extracts Markdown text and isolated diagram images from PDF via Mistral OCR with deduplication."""
    if not os.path.exists(img_output_dir):
        os.makedirs(img_output_dir)

    file_hash = get_file_hash(pdf_path)
    remote_filename = f"{file_hash}_{pdf_path.name}"

    my_files = mistral_client.files.list()
    uploaded_file_id = next((f.id for f in my_files.data if f.filename == remote_filename), None)

    if not uploaded_file_id:
        print(f"Uploading '{pdf_path.name}' to Mistral OCR...")
        with open(pdf_path, "rb") as f:
            uploaded_file = mistral_client.files.upload(
                file={"file_name": remote_filename, "content": f.read()}, purpose="ocr"
            )
        uploaded_file_id = uploaded_file.id
    else:
        print("-> File hash match found on Mistral server. Skipping upload!")

    signed_url = mistral_client.files.get_signed_url(file_id=uploaded_file_id, expiry=1)

    print("Executing Mistral OCR processing...")
    ocr_response = mistral_client.ocr.process(
        model="mistral-ocr-latest",
        document={"type": "document_url", "document_url": signed_url.url},
        include_image_base64=True
    )

    full_markdown = ""
    extracted_images = {}

    for page in ocr_response.pages:
        full_markdown += page.markdown + "\n\n"
        for img in page.images:
            raw_b64 = img.image_base64.split("base64,")[-1] if "base64," in img.image_base64 else img.image_base64
            img_data = base64.b64decode(raw_b64)

            file_name = f"{img.id}.jpeg"
            file_path = os.path.join(img_output_dir, file_name)
            with open(file_path, "wb") as f:
                f.write(img_data)

            extracted_images[file_name] = file_path

    if extracted_images:
        enhance_extracted_images(img_output_dir)

    return full_markdown, extracted_images

# ---------------------------------------------------------------------------
# 3. XML POST-PROCESSING & BASE64 INJECTION
# ---------------------------------------------------------------------------
def fix_and_inject_moodle_xml(raw_xml, image_map):
    print("Post-processing and validating Moodle XML structure...")

    b64_map = {}
    for img_filename, filepath in image_map.items():
        if os.path.exists(filepath):
            with open(filepath, "rb") as f:
                b64_map[img_filename] = base64.b64encode(f.read()).decode("utf-8")

    def ensure_text_wrapper(match):
        tag_name, attributes, content = match.group(1), match.group(2), match.group(3)
        if "<text>" not in content:
            content = f"\n      <text>{content.strip()}</text>\n    "
        return f"<{tag_name}{attributes}>{content}</{tag_name}>"

    xml_fixed = re.sub(
        r'<(questiontext|generalfeedback)([^>]*)>(.*?)</\1>',
        ensure_text_wrapper, raw_xml, flags=re.DOTALL
    )

    def inject_file_tag(match):
        full_block = match.group(0)
        found_imgs = re.findall(r'@@PLUGINFILE@@/([a-zA-Z0-9_\-\.]+)', full_block)

        for img_name in set(found_imgs):
            clean_id = img_name.replace(".jpeg", "").replace(".jpg", "")
            target_key = next((k for k in b64_map.keys() if clean_id in k), None)

            if target_key and f'<file name="{img_name}"' not in full_block:
                b64_data = b64_map[target_key]
                file_tag = f'\n      <file name="{img_name}" encoding="base64">{b64_data}</file>'
                closing_index = full_block.rfind("</")
                if closing_index != -1:
                    full_block = full_block[:closing_index] + file_tag + "\n    " + full_block[closing_index:]

        return full_block

    pattern = r'<(questiontext|answer|generalfeedback)[^>]*>.*?</\1>'
    processed_xml = re.sub(pattern, inject_file_tag, xml_fixed, flags=re.DOTALL)

    processed_xml = re.sub(
        r'<name>\s*<!\[CDATA\[(.*?)\]\]>\s*</name>',
        r'<name>\n        <text>\1</text>\n    </name>', processed_xml
    )

    return processed_xml

# ---------------------------------------------------------------------------
# 4. LOCAL PDF COMPILERS (HTML / TeX)
# ---------------------------------------------------------------------------
def compile_html_to_pdf(html_content: str, output_pdf_path: Path):
    html_file = output_pdf_path.with_suffix(".html")
    with open(html_file, "w", encoding="utf-8") as f:
        f.write(html_content)

    print(f"Compiling HTML to PDF via Headless Chrome -> {output_pdf_path}...")
    cmd = [
        "google-chrome", "--headless", "--disable-gpu", "--no-sandbox",
        "--disable-dev-shm-usage", "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=5000", "--no-pdf-header-footer",
        f"--print-to-pdf={output_pdf_path}", str(html_file)
    ]
    subprocess.run(cmd, check=True)

def compile_tex_to_pdf(tex_content: str, output_pdf_path: Path):
    from pipeline_utils import sanitize_indic_font_blocks

    tex_file = output_pdf_path.with_suffix(".tex")
    sanitized = sanitize_indic_font_blocks(tex_content)
    with open(tex_file, "w", encoding="utf-8") as f:
        f.write(sanitized)

    print(f"Compiling TeX to PDF via xelatex -> {output_pdf_path}...")
    cmd = ["xelatex", "-interaction=nonstopmode", "-halt-on-error", f"-output-directory={output_pdf_path.parent}", str(tex_file)]
    subprocess.run(cmd, check=True)

# ---------------------------------------------------------------------------
# 5. GEMINI CHAT GENERATION ENGINE (`Chat.send_message`)
# ---------------------------------------------------------------------------
def generate_content_with_chat(system_instruction, contents):
    """Executes stateful generation using Chat.send_message."""
    print("Initiating Gemini Chat Session (gemini-3.5-flash)...")

    chat = gemini_client.chats.create(
        model="gemini-3.5-flash",
        config=types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=0.1,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)
        )
    )

    response = chat.send_message(message=contents)
    raw_text = response.text

    match = re.search(r'```(?:xml|html|tex|latex)?\n(.*?)\n```', raw_text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return raw_text.strip()

# ---------------------------------------------------------------------------
# 6. MODE HANDLERS
# ---------------------------------------------------------------------------
def cmd_extract(args, rules_dict):
    system_instruction = assemble_prompt_files(
        rules_dict, mode="extract", output_format="xml", verify_online=args.verify_online
    )
    input_dir_path = Path(args.input_dir).resolve()
    pdf_files = [f for f in input_dir_path.rglob("*.pdf") if not f.name.startswith("sliced_") and not f.name.startswith("temp_page_")]

    if not pdf_files:
        print(f"❌ No PDF files found in {input_dir_path}")
        return

    for pdf_path in pdf_files:
        print(f"\n{'='*60}\n📄 Mode: Extract | Processing: {pdf_path.name}")
        staging_dir = "extracted_data"

        # Slices the ENTIRE requested range (e.g. pages 4 to 10) at once
        processed_pdf, is_temp = slice_pdf_pages(pdf_path, args.page_range)

        try:
            markdown_text, image_map = run_mistral_ocr(processed_pdf, staging_dir)
            print(f"Mistral OCR extracted {len(image_map)} image(s) across the page range.")

            contents = [
                f"Here is the complete OCR Markdown of the exam for the requested pages:\n\n{markdown_text}\n\n"
                f"Attached Diagram Reference IDs: {list(image_map.keys())}\n\n"
                f"Target Languages: {args.languages}\nTarget Standards: {args.standards}\nGlobal Tags: {args.tags}"
            ]

            for img_filename, filepath in image_map.items():
                contents.append(f"Diagram reference ID: {img_filename}")
                contents.append(Image.open(filepath))

            raw_xml = generate_content_with_chat(system_instruction, contents)
            final_xml = fix_and_inject_moodle_xml(raw_xml, image_map)

            output_filepath = args.output_dir / f"{pdf_path.stem}_moodle.xml"
            with open(output_filepath, "w", encoding="utf-8") as f:
                f.write(final_xml)

            print(f"✨ Saved Moodle XML to: {output_filepath}")
        finally:
            if is_temp and processed_pdf.exists():
                os.remove(processed_pdf)

def cmd_generate_chapter(args, rules_dict):
    system_instruction = assemble_prompt_files(
        rules_dict, mode="generate-chapter", output_format=args.output_format, pdf_engine=args.pdf_engine
    )
    source_files = [f for f in Path(args.input_dir).rglob("*") if f.suffix.lower() in [".pdf", ".md"] and not f.name.startswith("sliced_") and not f.name.startswith("temp_page_")]

    for fpath in source_files:
        print(f"\n{'='*60}\n📚 Mode: Generate Chapter ({args.output_format.upper()}) | File: {fpath.name}")
        staging_dir = "extracted_data"
        is_temp = False

        if fpath.suffix.lower() == ".pdf":
            processed_pdf, is_temp = slice_pdf_pages(fpath, args.page_range)
            markdown_text, image_map = run_mistral_ocr(processed_pdf, staging_dir)
        else:
            with open(fpath, "r", encoding="utf-8") as f:
                markdown_text = f.read()
            image_map = {}

        try:
            contents = [
                f"Chapter Content Markdown:\n\n{markdown_text}\n\n"
                f"Constraints: Synthesize up to {args.num_questions} questions.\n"
                f"Difficulty: {args.difficulty}\nTarget Languages: {args.languages}\n"
                f"Target Standards: {args.standards}\nGlobal Tags: {args.tags}"
            ]

            for img_filename, filepath in image_map.items():
                contents.append(f"Diagram reference ID: {img_filename}")
                contents.append(Image.open(filepath))

            output_text = generate_content_with_chat(system_instruction, contents)

            if args.output_format == "pdf":
                pdf_path = args.output_dir / f"{fpath.stem}_synthetic.pdf"
                if args.pdf_engine == "tex":
                    compile_tex_to_pdf(output_text, pdf_path)
                else:
                    compile_html_to_pdf(output_text, pdf_path)
                print(f"✨ Saved Synthetic PDF to: {pdf_path}")
            else:
                final_xml = fix_and_inject_moodle_xml(output_text, image_map)
                xml_path = args.output_dir / f"{fpath.stem}_synthetic.xml"
                with open(xml_path, "w", encoding="utf-8") as f:
                    f.write(final_xml)
                print(f"✨ Saved Synthetic XML to: {xml_path}")
        finally:
            if is_temp and processed_pdf.exists():
                os.remove(processed_pdf)

def cmd_generate_mock(args, rules_dict):
    system_instruction = assemble_prompt_files(
        rules_dict, mode="generate-mock", output_format=args.output_format, pdf_engine=args.pdf_engine
    )

    if not args.blueprint.exists():
        print(f"❌ Blueprint file not found: {args.blueprint}")
        return

    with open(args.blueprint, "r", encoding="utf-8") as f:
        blueprint = json.load(f)

    exam_name = blueprint.get("exam_name", "MOCK_EXAM")
    subjects = blueprint.get("subjects", [])

    syllabus_blocks = []
    for subj in subjects:
        subj_name = subj.get("name", "Subject")
        total_qs = subj.get("total_questions", 0)
        s_path = Path(subj.get("syllabus_file", ""))
        content = s_path.read_text(encoding="utf-8") if s_path.exists() else ""
        syllabus_blocks.append(f"### SUBJECT: {subj_name.upper()}\nQuestions: {total_qs}\nSyllabus:\n{content}\n")

    contents = [
        f"Synthesize a complete mock paper for {exam_name}.\n\n"
        f"Difficulty Mix: {args.difficulty_mix}\nTarget Languages: {args.languages}\n"
        f"Target Standards: {args.standards}\nGlobal Tags: {args.tags}\n\n" + "\n".join(syllabus_blocks)
    ]

    output_text = generate_content_with_chat(system_instruction, contents)

    if args.output_format == "pdf":
        pdf_path = args.output_dir / f"mock_{exam_name.lower()}_paper.pdf"
        if args.pdf_engine == "tex":
            compile_tex_to_pdf(output_text, pdf_path)
        else:
            compile_html_to_pdf(output_text, pdf_path)
        print(f"✨ Saved Full Mock PDF to: {pdf_path}")
    else:
        final_xml = fix_and_inject_moodle_xml(output_text, image_map={})
        xml_path = args.output_dir / f"mock_{exam_name.lower()}_full_bank.xml"
        with open(xml_path, "w", encoding="utf-8") as f:
            f.write(final_xml)
        print(f"✨ Saved Full Mock XML to: {xml_path}")

# ---------------------------------------------------------------------------
# 7. CLI ENTRY POINT
# ---------------------------------------------------------------------------
def main():
    shared_parser = argparse.ArgumentParser(add_help=False)

    # Global Command-Line Flags
    shared_parser.add_argument("--languages", default="english", help="Target languages (e.g. english,bengali)")
    shared_parser.add_argument("--standards", default="general", help="Target standards (e.g. neet_ug, jee_main)")
    shared_parser.add_argument("--tags", default="", help="Global tags (e.g. year:2026)")
    shared_parser.add_argument("--output-dir", default="./output", type=Path)
    shared_parser.add_argument("--bucket-name", default="unused", help="Maintained for CLI argument compatibility")
    shared_parser.add_argument("--output-format", choices=["xml", "pdf"], default="xml", help="Choose output format (xml or pdf).")
    shared_parser.add_argument("--pdf-engine", choices=["html", "tex"], default="html", help="Choose PDF renderer: html (Chrome) or tex (LaTeX).")

    # Core System Prompt Rule Files
    shared_parser.add_argument("--prompt", required=True, type=Path, help="Path to main exam prompt")
    shared_parser.add_argument("--instruction-file", default=None, type=Path)
    shared_parser.add_argument("--xml-rules", default="prompts/core/moodle_xml_rules.md", type=Path)
    shared_parser.add_argument("--tags-rules", default="prompts/core/naming_and_tags_rules.md", type=Path)
    shared_parser.add_argument("--templates", default="prompts/core/moodle_xml_templates.md", type=Path)
    shared_parser.add_argument("--pdf-rules", default=None, type=Path)
    shared_parser.add_argument("--pdf-rules-html", default="prompts/core/pdf_html_rules.md", type=Path)
    shared_parser.add_argument("--pdf-rules-tex", default="prompts/core/pdf_tex_rules.md", type=Path)

    parser = argparse.ArgumentParser(description="academic_process_pipeline.py - Unified Pipeline")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    # Mode 1: extract
    ext_parser = subparsers.add_parser("extract", parents=[shared_parser])
    ext_parser.add_argument("--input-dir", required=True, type=Path)
    ext_parser.add_argument("--page-range", type=int, nargs=2, metavar=('START', 'END'), help="Page range (e.g. 4 10)")
    ext_parser.add_argument("--no-instruction-page", action="store_true")
    ext_parser.add_argument("--verify-online", action="store_true")

    # Mode 2: generate-chapter
    chap_parser = subparsers.add_parser("generate-chapter", parents=[shared_parser])
    chap_parser.add_argument("--input-dir", required=True, type=Path)
    chap_parser.add_argument("--page-range", type=int, nargs=2, metavar=('START', 'END'), help="Page range (e.g. 1 3)")
    chap_parser.add_argument("--num-questions", type=int, default=5)
    chap_parser.add_argument("--difficulty", choices=["easy", "medium", "hard"], default="medium")

    # Mode 3: generate-mock
    mock_parser = subparsers.add_parser("generate-mock", parents=[shared_parser])
    mock_parser.add_argument("--blueprint", required=True, type=Path)
    mock_parser.add_argument("--sample-pdf", type=Path)
    mock_parser.add_argument("--difficulty-mix", default="easy:0.2,medium:0.5,hard:0.3")

    args = parser.parse_args()

    if args.mode == "extract" and args.output_format == "pdf":
        print("❌ Error: 'extract' mode only supports --output-format xml.")
        sys.exit(1)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    rules_dict = {
        "main_prompt": args.prompt,
        "instruction_file": args.instruction_file,
        "xml_rules": args.xml_rules,
        "tags_rules": args.tags_rules,
        "templates": args.templates,
        "pdf_rules": args.pdf_rules,
        "pdf_rules_html": args.pdf_rules_html,
        "pdf_rules_tex": args.pdf_rules_tex,
    }

    if args.mode == "extract":
        cmd_extract(args, rules_dict)
    elif args.mode == "generate-chapter":
        cmd_generate_chapter(args, rules_dict)
    elif args.mode == "generate-mock":
        cmd_generate_mock(args, rules_dict)

if __name__ == "__main__":
    main()