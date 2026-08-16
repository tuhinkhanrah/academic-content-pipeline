#!/usr/bin/env python3
"""
paper2moodle_gcloud.py - Master Unified Moodle XML Pipeline
Modes:
  1. extract          (Extract questions from PDFs via Sandbox Vision)
  2. generate-chapter (Synthesize questions from chapter PDFs/MDs)
  3. generate-mock    (Synthesize full mock exams from JSON blueprints)
"""

import os
import sys
import argparse
import subprocess
import json
from pathlib import Path
from google import genai

# =======================================================================
# 1. GCS Auth & Transfer Utilities
# =======================================================================

def get_gcloud_access_token() -> str:
    """Fetch active GCP OAuth access token from local gcloud CLI."""
    try:
        return subprocess.check_output(
            ["gcloud", "auth", "print-access-token"], text=True
        ).strip()
    except subprocess.CalledProcessError:
        print("❌ Error: Failed to fetch access token. Make sure you ran 'gcloud auth login'.")
        sys.exit(1)

def upload_to_gcs(file_path: Path, bucket_name: str, prefix: str = "processing_queue", verbose: bool = False) -> str:
    """Uploads a file to GCS, skipping if it already exists."""
    safe_filename = file_path.name.replace(" ", "_")
    gcs_target_uri = f"gs://{bucket_name}/{prefix}/{safe_filename}"
    
    try:
        subprocess.run(["gcloud", "storage", "ls", gcs_target_uri], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(f"⚡ {file_path.name} already exists in GCS. Skipping upload.")
        return gcs_target_uri
    except subprocess.CalledProcessError:
        pass # File not found, proceed to upload

    print(f"🔄 Uploading {file_path.name} to GCS...")
    cmd = ["gcloud", "storage", "cp", str(file_path), gcs_target_uri]
    subprocess.run(cmd, check=True, stdout=None if verbose else subprocess.DEVNULL, stderr=subprocess.PIPE)
    return gcs_target_uri

def download_from_gcs(bucket_name: str, gcs_path: str, local_path: Path):
    """Downloads a file from GCS to local disk."""
    gcs_uri = f"gs://{bucket_name}/{gcs_path}"
    print(f"📥 Fetching output XML to {local_path}...")
    subprocess.run(["gcloud", "storage", "cp", gcs_uri, str(local_path)], check=True, stdout=subprocess.DEVNULL)

# =======================================================================
# 2. Dynamic Rules & Prompts
# =======================================================================

BASE64_IMAGE_RULES = """
- **Precision Cropping:** Use your vision capabilities to determine the exact bounding boxes `[x0, y0, x1, y1]` for diagrams, graphs, circuits, and purely graphical answer options.
- **Text Exclusion:** Ensure zero text bleed. Bounding boxes must exclude question text, labels, and option numbers.
- **Moodle Native Embedding:** Images must be embedded using Base64 encoding. 
  - In the HTML/CDATA block: `<img src="@@PLUGINFILE@@/image_name.png" alt="description" />`
  - Immediately following the text block: `<file name="image_name.png" path="/" encoding="base64">BASE64_STRING</file>`
"""

SYNTHESIS_IMAGE_RULES = """
- **Programmatic Diagram Synthesis (DEFAULT):** Use Inline `<svg>` tags directly inside `<questiontext>` CDATA for circuits, graphs, ray optics, geometry, and chemical structures. Prevent cropping via padding.
- **Base64 Embedded PNG (FALLBACK):** ONLY if a biology question explicitly requires a highly intricate anatomical illustration where vector SVG is impossible.
"""

def assemble_prompt_files(rule_files: dict, mode: str) -> str:
    """Combine specified prompt markdown files into a single context, injecting dynamic image rules."""
    content_blocks = ["# AGENTS.md - Moodle System Rules\n"]
    
    # Select the right image rule based on mode
    img_rules = BASE64_IMAGE_RULES if mode == "extract" else SYNTHESIS_IMAGE_RULES

    for name, filepath in rule_files.items():
        if not filepath:
            continue
        path = Path(filepath)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
                if name == "xml_rules":
                    content = content.replace("{IMAGE_HANDLING_INSTRUCTIONS}", img_rules)
                content_blocks.append(f"## File: {path.name}\n\n{content}\n\n---\n")
        else:
            print(f"⚠️ Warning: Prompt file '{filepath}' not found. Skipping.")
            
    return "\n".join(content_blocks)

# =======================================================================
# 3. Sandbox Execution Engine
# =======================================================================

def run_remote_sandbox(client, agent_name, prompt, gcp_token, agents_md_content, verbose):
    """Executes a remote interaction with the active Bearer token authorized."""
    print("🚀 Provisioning remote execution sandbox...")
    interaction = client.interactions.create(
        agent=agent_name,
        input=prompt,
        environment={
            "type": "remote",
            "sources": [
                {
                    "type": "inline", 
                    "target": ".agents/AGENTS.md", 
                    "content": agents_md_content
                }
            ],
            "network": {
                "allowlist": [
                    {"domain": "storage.googleapis.com", "transform": {"Authorization": f"Bearer {gcp_token}"}},
                    {"domain": "*"}
                ]
            }
        },
    )
    if verbose:
        print("\n🔍 Agent Finished Execution. Logs:\n" + "="*60)
        print(interaction.output_text)
        print("="*60 + "\n")
    return interaction

# =======================================================================
# 4. Mode Implementations
# =======================================================================

def cmd_extract(args, client, gcp_token, rules_dict):
    """Extract mode: Exact proven execution_prompt preserved without modification."""
    agents_md_content = assemble_prompt_files(rules_dict, mode="extract")
    input_dir_path = Path(args.input_dir).resolve()
    pdf_files = list(input_dir_path.rglob("*.pdf"))
    
    if not pdf_files:
        print(f"❌ No PDF files found in {input_dir_path}")
        return

    print(f"  Found {len(pdf_files)} PDF(s). Starting sequential processing loop...\n")

    for pdf_path in pdf_files:
        print(f"\n{'='*60}")
        print(f"📄 Mode: Extract | Processing: {pdf_path.name}")
        print(f"📁 Directory: {pdf_path.parent}")
        
        gcs_uri = upload_to_gcs(pdf_path, args.bucket_name, verbose=args.verbose)
        if not gcs_uri:
            continue
            
        safe_filename = pdf_path.name.replace(" ", "_")
        output_filename = f"{pdf_path.stem}_moodle.xml"
        gcs_upload_path = f"output/{output_filename}"
        bucket_upload_url = f"https://storage.googleapis.com/upload/storage/v1/b/{args.bucket_name}/o?uploadType=media&name={gcs_upload_path}"

        page_rule = f"Process only pages {args.page_range[0]} to {args.page_range[1]}." if args.page_range else "Process all pages."
        instruction_rule = "Skip the instruction page completely." if args.no_instruction_page else "Include questions from the instruction page if any exist."
        
        execution_prompt = f"""
        You are an autonomous exam paper processing agent capable of handling questions from any academic subject.
        Your goal is to extract all exam questions from the PDF file mounted at `/workspace/pdfs/input.pdf` 
        and generate a single, strictly valid Moodle XML question bank saved to `/workspace/output/{output_filename}`.

        ### ⚙️ Extraction Parameters for this Run:
        - **Target Languages**: {args.languages} (CRITICAL: You MUST output all question text, options, and feedback in ALL of these languages. If a language is missing in the source PDF, you MUST translate it.)
        - **Page Range**: {page_rule}
        - **Instructions Rule**: {instruction_rule}

        ### 📋 Workflow Steps:
        1. **Fetch & Render PDF**: 
           - Write and run a Python script to download the PDF using the `requests` library:
             ```python
             import requests, os
             os.makedirs("/workspace/pdfs", exist_ok=True)
             os.makedirs("/workspace/images", exist_ok=True)
             
             url = "[https://storage.googleapis.com/](https://storage.googleapis.com/){args.bucket_name}/processing_queue/{safe_filename}"
             headers = {{"Authorization": "Bearer {gcp_token}"}}
             
             print("Downloading PDF from GCS...")
             response = requests.get(url, headers=headers)
             if response.status_code == 200:
                 with open("/workspace/pdfs/input.pdf", "wb") as f:
                     f.write(response.content)
                 print("PDF downloaded successfully.")
             else:
                 raise Exception(f"Failed to download PDF: {{response.status_code}} {{response.text}}")
             ```
           - After downloading, write a script using `pymupdf` (PyMuPDF) to convert the specified pages of `/workspace/pdfs/input.pdf` into high-resolution PNG images in `/workspace/images/`.

        2. **Visual Inspection & Verification**:
           - Visually inspect the rendered PNG images.
           - Accurately read all textual content, mathematical formulas, and diagrams.
           - Run Python verification scripts to double-check mathematical calculations or logic if needed.

        3. **Vision-Guided Precision Cropping (No Text Bleed)**:
            - **USE YOUR VISION**: Do NOT rely on `pymupdf` `get_images()` or `get_drawings()` to find diagrams, as the PDF structure often bundles text and images together.
            - **Determine Coordinates**: Visually inspect the rendered PNGs. Determine the exact bounding box coordinates `[x0, y0, x1, y1]` for ANY visual asset. This includes, but is not limited to: graphs, circuits, chemical structures, biological diagrams, maps, data charts, or photographs.
            - **STRICT TEXT EXCLUSION**: Your bounding box MUST ONLY encapsulate the graphical elements. You must explicitly exclude any surrounding question text, labels like "Select one:", prose, or standard mathematical/chemical formulas (which should be LaTeX).
            - **Python Cropping**: Write a Python script using `PIL` (Pillow) to open the rendered high-resolution PNGs and crop them using your visually determined coordinates.
            - **Graphical Multiple-Choice Options**: If the options are purely graphical (e.g., individual charts, molecular rings, or diagrams), visually determine the coordinates for EACH option separately and crop them into individual images. DO NOT use placeholders like "Option 1 graph".
            - **Base64 & XML Embedding**: Convert the perfectly cropped images to Base64 strings. You MUST use Moodle's native file embedding:
              - **For Question Text**: `<img src="@@PLUGINFILE@@/q_img.png" />` followed by `<file name="q_img.png" path="/" encoding="base64">YOUR_BASE64_STRING_HERE</file>`.
              - **For Answer Options**: Inside EACH `<answer>`, place `<img src="@@PLUGINFILE@@/opt_img.png" />` followed by `<file name="opt_img.png" path="/" encoding="base64">YOUR_BASE64_STRING_HERE</file>`.

        4. **Moodle XML Formatting**: Adhere strictly to all formatting rules, tag schemas, and bilingual/monolingual instructions provided in `.agents/AGENTS.md`.

        5. **Output & Persistent Delivery**:
            - Write the final Moodle XML file locally to `{output_filename}`.
            - Write and execute a Python script to upload `{output_filename}` directly to Google Cloud Storage.
            - Use the `requests` library:
              ```python
              import requests
              url = "{bucket_upload_url}"
              with open("{output_filename}", "rb") as f:
                  data = f.read()
              # Execute POST request to upload
              headers = {{
                  "Content-Type": "application/xml",
                  "Authorization": "Bearer {gcp_token}"
              }}
              response = requests.post(url, headers=headers, data=data)
              print(f"GCS Upload Status Code: {{response.status_code}}")
              print(f"GCS Response: {{response.text}}")
              # CRITICAL: Raise error if upload failed so the process halts explicitly
              if response.status_code not in [200, 201]:
                  raise RuntimeError(f"GCS Upload failed with status {{response.status_code}}: {{response.text}}")
              ```
            - Ensure the script prints the exact HTTP status code and response body to the terminal logs.
        """
        
        run_remote_sandbox(client, args.agent_name, execution_prompt, gcp_token, agents_md_content, args.verbose)
        download_from_gcs(args.bucket_name, gcs_upload_path, pdf_path.parent / output_filename)


def cmd_generate_chapter(args, client, gcp_token, rules_dict):
    """Generate-Chapter mode: Synthesizes question banks from book chapters."""
    agents_md_content = assemble_prompt_files(rules_dict, mode="generate-chapter")
    source_files = [f for f in Path(args.input_dir).rglob("*") if f.suffix.lower() in [".pdf", ".md"]]
    
    for fpath in source_files:
        print(f"\n{'='*60}\n📚 Mode: Generate Chapter | File: {fpath.name}")
        upload_to_gcs(fpath, args.bucket_name, verbose=args.verbose)
        
        output_filename = f"{fpath.stem}_synthetic.xml"
        gcs_upload_path = f"output/{output_filename}"
        download_url = f"https://storage.googleapis.com/{args.bucket_name}/processing_queue/{fpath.name.replace(' ', '_')}"
        upload_url = f"https://storage.googleapis.com/upload/storage/v1/b/{args.bucket_name}/o?uploadType=media&name={gcs_upload_path}"

        page_rule = f"Process only pages {args.page_range[0]} to {args.page_range[1]}." if args.page_range else "Process all content."

        execution_prompt = f"""
        You are an autonomous synthetic question generator.
        Your goal is to read the chapter document mounted at `{fpath.name}` and generate a valid Moodle XML question bank saved to `{output_filename}`.
        
        ### ⚙️ Generation Constraints:
        - **Target Standards**: {args.standards}
        - **Target Languages**: {args.languages} (CRITICAL: Output options/feedback stacked bilingually if multiple requested).
        - **Target Difficulty**: {args.difficulty.upper()}
        - **Max Questions per Page/Section**: {args.num_questions}
        - **Scope**: {page_rule}
        
        ### 📋 Workflow Steps:
        1. **Download Document**:
           ```python
           import requests, os
           os.makedirs("/workspace/docs", exist_ok=True)
           url = "{download_url}"
           headers = {{"Authorization": "Bearer {gcp_token}"}}
           resp = requests.get(url, headers=headers)
           if resp.status_code == 200:
               with open("/workspace/docs/input_file", "wb") as f:
                   f.write(resp.content)
           else:
               raise Exception(f"Download failed: {{resp.status_code}}")
           ```
        2. Read and analyze the chapter content.
        3. Synthesize high-quality practice questions adhering strictly to `.agents/AGENTS.md`. Use inline SVG for diagrams where needed.
        4. **Upload XML Output**:
           ```python
           import requests
           headers = {{"Content-Type": "application/xml", "Authorization": "Bearer {gcp_token}"}}
           resp = requests.post("{upload_url}", headers=headers, data=open("{output_filename}", "rb").read())
           if resp.status_code not in [200, 201]: raise Exception(f"Upload failed: {{resp.text}}")
           ```
        """
        run_remote_sandbox(client, args.agent_name, execution_prompt, gcp_token, agents_md_content, args.verbose)
        download_from_gcs(args.bucket_name, gcs_upload_path, fpath.parent / output_filename)


def cmd_generate_mock(args, client, gcp_token, rules_dict):
    """Generate-Mock mode: Single-request synthesis of full exam papers from blueprints."""
    agents_md_content = assemble_prompt_files(rules_dict, mode="generate-mock")
    
    # 1. Load Blueprint JSON
    if not args.blueprint.exists():
        print(f"❌ Blueprint file not found: {args.blueprint}")
        return

    with open(args.blueprint, "r", encoding="utf-8") as f:
        blueprint = json.load(f)
    
    exam_name = blueprint.get("exam_name", "MOCK_EXAM")
    subjects = blueprint.get("subjects", [])

    # 2. Aggregate all subject syllabi into a single text block
    print(f"\n{'='*60}\n🎓 Mode: Generate Mock | Single-Request Paper Generation: {exam_name}")
    print("  Aggregating all subject syllabi specified in blueprint...")

    syllabus_blocks = []
    for subj in subjects:
        subj_name = subj.get("name", "Subject")
        total_qs = subj.get("total_questions", 0)
        grade = subj.get("default_grade", 4.0)
        penalty = subj.get("penalty", 0.25)
        neg_frac = subj.get("negative_fraction", -25)
        
        s_path = Path(subj.get("syllabus_file", ""))
        content = ""
        if s_path.exists():
            content = f_path.read_text(encoding="utf-8") if (f_path := s_path).exists() else ""
            print(f"  ✓ Loaded syllabus for {subj_name} ({s_path.name})")
        else:
            print(f"  ⚠️ Warning: Syllabus file '{s_path}' not found.")

        block = (
            f"### SUBJECT: {subj_name.upper()}\n"
            f"- Total Questions Required: {total_qs}\n"
            f"- Default Grade (<defaultgrade>): {grade}\n"
            f"- Penalty (<penalty>): {penalty}\n"
            f"- Incorrect Choice Fraction: {neg_frac}%\n\n"
            f"Syllabus Scope:\n{content}\n"
        )
        syllabus_blocks.append(block)

    all_syllabi_text = "\n" + "="*40 + "\n\n".join(syllabus_blocks)

    # 3. Handle optional sample reference PDF
    sample_download_script = ""
    if args.sample_pdf and args.sample_pdf.exists():
        upload_to_gcs(args.sample_pdf, args.bucket_name, verbose=args.verbose)
        sample_url = f"https://storage.googleapis.com/{args.bucket_name}/processing_queue/{args.sample_pdf.name.replace(' ', '_')}"
        sample_download_script = f"""
        # Download reference sample PDF
        r = requests.get("{sample_url}", headers={{"Authorization": "Bearer {gcp_token}"}})
        if r.status_code == 200:
            with open("/workspace/pdfs/sample_paper.pdf", "wb") as f:
                f.write(r.content)
        """

    output_filename = f"mock_{exam_name.lower()}_full_bank.xml"
    gcs_upload_path = f"output/{output_filename}"
    upload_url = f"https://storage.googleapis.com/upload/storage/v1/b/{args.bucket_name}/o?uploadType=media&name={gcs_upload_path}"

    # 4. Construct Single Unified Execution Prompt
    execution_prompt = f"""
    You are an autonomous master test construction agent.
    Your objective is to synthesize a complete, calibrated, full-length Moodle XML question bank for **{exam_name}** in a SINGLE execution.

    ### ⚙️ Global Exam Blueprint & Constraints:
    - **Exam Standard**: {args.standards}
    - **Target Languages**: {args.languages} (Output stacked bilingual content if multiple languages are specified).
    - **Difficulty Breakdown Ratio**: {args.difficulty_mix} (e.g. easy:0.2, medium:0.5, hard:0.3 ratio across subjects).
    - **Global Tags**: {args.tags}

    ### 📚 Combined Syllabus Scope & Blueprint Specifications:
    {all_syllabi_text}

    ### 📋 Workflow Steps:
    1. **Setup Environment**:
       - Execute Python code to initialize directories and load sample files:
         ```python
         import requests, os
         os.makedirs("/workspace/pdfs", exist_ok=True)
         os.makedirs("/workspace/output", exist_ok=True)
         {sample_download_script}
         ```

    2. **Synthesize Complete Question Bank**:
       - Generate all required questions for EVERY subject according to the blueprint totals and difficulty ratio mix.
       - Render diagrams programmatically using clean inline vector `<svg>` elements inside CDATA (or Base64 fallbacks for complex anatomy).
       - Ensure all naming conventions, XML tag structures, LaTeX delimiters, and step-by-step explanations strictly adhere to `.agents/AGENTS.md`.
       - Wrap all question nodes inside a single `<quiz>` root XML document saved locally to `{output_filename}`.

    3. **Upload Consolidated XML to GCS**:
       - Execute Python code to POST the full XML file directly to Google Cloud Storage:
         ```python
         import requests
         url = "{upload_url}"
         headers = {{
             "Content-Type": "application/xml",
             "Authorization": "Bearer {gcp_token}"
         }}
         with open("{output_filename}", "rb") as f:
             data = f.read()
         
         response = requests.post(url, headers=headers, data=data)
         print(f"GCS Upload Status Code: {{response.status_code}}")
         print(f"GCS Response: {{response.text}}")
         if response.status_code not in [200, 201]:
             raise RuntimeError(f"GCS Upload failed with status {{response.status_code}}: {{response.text}}")
         ```
    """

    # 5. Execute single sandbox interaction & retrieve final XML
    run_remote_sandbox(client, args.agent_name, execution_prompt, gcp_token, agents_md_content, args.verbose)
    download_from_gcs(args.bucket_name, gcs_upload_path, Path(args.output_dir) / output_filename)


# =======================================================================
# 5. CLI Definition & Main Entry Point
# =======================================================================

def main():
    # 1. Create a "Shared" parser for all the global flags
    shared_parser = argparse.ArgumentParser(add_help=False)
    
    # Global Command-Line Flags
    shared_parser.add_argument("--languages", default="english", help="Target languages (e.g. english,bengali)")
    shared_parser.add_argument("--standards", default="general", help="Target standards (e.g. neet_ug, jee_main)")
    shared_parser.add_argument("--tags", default="", help="Global tags (e.g. year:2026)")
    shared_parser.add_argument("--output-dir", default="./output", type=Path)
    shared_parser.add_argument("--bucket-name", required=True, help="GCS Bucket name for staging and output")
    shared_parser.add_argument("--agent-name", default="antigravity-preview-05-2026")
    shared_parser.add_argument("--verbose", action="store_true", help="Print detailed remote execution logs")

    # Core System Prompt Rule Files
    shared_parser.add_argument("--prompt", required=True, type=Path, help="Path to the main exam prompt (e.g. jee_main.md)")
    shared_parser.add_argument("--instruction-file", default=None, type=Path, help="Path to exam instruction file")
    shared_parser.add_argument("--xml-rules", default="prompts/core/moodle_xml_rules.md", type=Path)
    shared_parser.add_argument("--tags-rules", default="prompts/core/naming_and_tags_rules.md", type=Path)
    shared_parser.add_argument("--templates", default="prompts/core/moodle_xml_templates.md", type=Path)

    # 2. Create the Main parser and attach the shared parser to subcommands
    parser = argparse.ArgumentParser(description="paper2moodle_gcloud.py - Master Unified Moodle XML Pipeline")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    # Subcommand Mode 1: extract
    ext_parser = subparsers.add_parser("extract", parents=[shared_parser], help="Extract questions from exam paper PDFs")
    ext_parser.add_argument("--input-dir", required=True, type=Path, help="Local directory containing input PDFs")
    ext_parser.add_argument("--page-range", type=int, nargs=2, metavar=('START', 'END'), help="Page range to extract (e.g., 1 10)")
    ext_parser.add_argument("--no-instruction-page", action="store_true", help="Skip front instruction page")

    # Subcommand Mode 2: generate-chapter
    chap_parser = subparsers.add_parser("generate-chapter", parents=[shared_parser], help="Synthesize practice questions from chapter documents")
    chap_parser.add_argument("--input-dir", required=True, type=Path)
    chap_parser.add_argument("--page-range", type=int, nargs=2, metavar=('START', 'END'))
    chap_parser.add_argument("--num-questions", type=int, default=5)
    chap_parser.add_argument("--difficulty", choices=["easy", "medium", "hard"], default="medium")

    # Subcommand Mode 3: generate-mock
    mock_parser = subparsers.add_parser("generate-mock", parents=[shared_parser], help="Synthesize full mock exam papers from JSON blueprints")
    mock_parser.add_argument("--blueprint", required=True, type=Path)
    mock_parser.add_argument("--sample-pdf", type=Path)
    mock_parser.add_argument("--difficulty-mix", default="easy:0.2,medium:0.5,hard:0.3")

    args = parser.parse_args()

    # Validate Environment
    if not os.environ.get("GEMINI_API_KEY"):
        print("❌ Error: GEMINI_API_KEY environment variable is not set.")
        sys.exit(1)

    gcp_token = get_gcloud_access_token()
    client = genai.Client()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    rules_dict = {
        "main_prompt": args.prompt,
        "instruction_file": args.instruction_file,
        "xml_rules": args.xml_rules,
        "tags_rules": args.tags_rules,
        "templates": args.templates,
    }

    # Dispatch to appropriate mode handler
    if args.mode == "extract":
        cmd_extract(args, client, gcp_token, rules_dict)
    elif args.mode == "generate-chapter":
        cmd_generate_chapter(args, client, gcp_token, rules_dict)
    elif args.mode == "generate-mock":
        cmd_generate_mock(args, client, gcp_token, rules_dict)

if __name__ == "__main__":
    main()