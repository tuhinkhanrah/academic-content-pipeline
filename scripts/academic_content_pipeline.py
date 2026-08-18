#!/usr/bin/env python3
"""
academic_content_pipeline.py - Master Unified Academic Content Pipeline
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
    print(f"📥 Fetching output to {local_path}...")
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

def assemble_prompt_files(rule_files: dict, mode: str, output_format: str = "xml", pdf_engine: str = "html") -> str:
    """Combine specified prompt markdown files into a single context, handling PDF vs XML routing."""
    content_blocks = ["# AGENTS.md - System Rules\n"]

    # Select the right image rule based on mode (Only applies to XML)
    img_rules = BASE64_IMAGE_RULES if mode == "extract" else SYNTHESIS_IMAGE_RULES

    # Route rules based on output format
    if output_format == "pdf":
        pdf_rule_key = "pdf_rules_tex" if pdf_engine == "tex" else "pdf_rules_html"
        valid_keys = ["main_prompt", "instruction_file", pdf_rule_key]
        legacy_key = rule_files.get("pdf_rules")
        if legacy_key:
            valid_keys.append("pdf_rules")
    else:
        valid_keys = ["main_prompt", "instruction_file", "xml_rules", "tags_rules", "templates"]

    for name, filepath in rule_files.items():
        if name not in valid_keys or not filepath:
            continue
        path = Path(filepath)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
                # Dynamically inject image rules ONLY for xml_rules
                if name == "xml_rules" and output_format == "xml":
                    content = content.replace("{IMAGE_HANDLING_INSTRUCTIONS}", img_rules)
                content_blocks.append(f"## File: {path.name}\n\n{content}\n\n---\n")
        else:
            print(f"⚠️ Warning: Prompt file '{filepath}' not found. Skipping.")

    return "\n".join(content_blocks)


def build_pdf_action_instructions(pdf_engine: str, artifact_base: str, upload_url: str) -> tuple[str, str]:
    """Return the PDF generation instructions and the final artifact file name for the selected engine."""
    if pdf_engine == "tex":
        action_instructions = f"""
        3. **Synthesize LaTeX & Compile PDF**:
           - Create a complete standalone LaTeX document named `{artifact_base}.tex` using `article`/`exam`-style structure.
           - CRITICAL: Ensure no text extends beyond the right margin or gets cut off. Every question and statement must be fully contained within the page boundaries.
           - Use native LaTeX for all equations and labels, and keep bilingual output in a clean stacked or side-by-side layout (for example, English first then target language).
           - Ensure the source is valid UTF-8 and includes packages for multilingual text if the target language requires it.
           - For every bilingual question, create two fully separate blocks: first the complete English question block, then a second block for the translated language. Each block must be a standalone question unit with its own option list and its own numbering.
           - For long assertions, statements, or reasons, insert explicit line breaks (`\\\\` or new paragraphs) to prevent text from overflowing past the page edge. Break them at logical points (commas, clause boundaries) rather than allowing one continuous unbroken line.
           - If the chapter/source or applicable subject syllabus supports a diagram question and the selected exam format permits it, include at least one diagram-based MCQ. Do not force one when the source is unsuitable, the question cap leaves no room, or a faithful diagram cannot be rendered.
           - Before selecting SVG-based diagram questions, check whether `rsvg-convert` or Inkscape is available. For eligible diagrams such as circuits, graphs, ray diagrams, geometry, force vectors, and simple chemical structures, create an SVG source with a `viewBox` and padding. If a converter is available, convert the SVG to a local PDF (for example, `rsvg-convert -f pdf -o diagram.pdf diagram.svg`) and include it using `\\includegraphics`; do not put raw SVG in the `.tex` document or depend on an unverified `\\includesvg` workflow.
           - If no SVG-to-PDF converter is available, use TikZ when it can render the diagram accurately. Only omit the diagram-based question when neither SVG conversion nor a faithful TikZ implementation is available. Use PNG only when an accurate vector diagram is impractical, such as detailed biological anatomy.
           - For any Indic language (Hindi, Bengali, Tamil, Telugu, Gujarati, Kannada, Malayalam, Marathi, Punjabi, Odia, Assamese, etc.), use `xelatex` or `lualatex` with `fontspec` and a script-specific Unicode-capable font such as `Noto Serif Devanagari`, `Noto Serif Bengali`, `Noto Serif Tamil`, `Noto Serif Telugu`, etc.
           - Include an overflow-resistant preamble:
             ```latex
             \\documentclass{{article}}
             \\usepackage[margin=1.0in]{{geometry}}
             \\usepackage{{amsmath,amssymb,microtype}}
             \\usepackage{{ragged2e}}
             \\usepackage{{fontspec}}
             \\usepackage{{polyglossia}}
             \\raggedbottom
             \\setmainlanguage{{english}}
             \\setotherlanguage{{hindi}}
             \\newfontfamily\\hindifont[Script=Devanagari]{{Noto Serif Devanagari}}
             \\begin{{document}}
             \\RaggedRight
             \\parindent=0pt
             \\emergencystretch=2em
             ```
           - Compile with `xelatex -interaction=nonstopmode -halt-on-error {artifact_base}.tex` or `lualatex -interaction=nonstopmode -halt-on-error {artifact_base}.tex`.
           - Confirm the compiled PDF exists, is non-empty, and contains no truncated text before upload. For every diagram question, confirm every `\\includegraphics` file exists and visually inspect the rendered page to verify the complete diagram and its labels are visible without clipping.
        4. **Upload PDF Output**:
           - Upload the compiled `{artifact_base}.pdf` to `{upload_url}` via Python `requests.post`.
        """
        post_file_name = f"{artifact_base}.pdf"
    else:
        action_instructions = f"""
        3. **Synthesize HTML & Compile PDF**:
           - Synthesize high-quality practice questions adhering to `.agents/AGENTS.md`.
           - Output a clean, complete HTML file (`{artifact_base}.html`) correctly importing KaTeX and Google Web Fonts.
           - CRITICAL: For every bilingual question, create two fully separate HTML blocks: first the complete English question with its full option set (A, B, C, D), then the translated question with a separate full option set. Do not merge English and translated options into one shared list like `(A) 1 (English) / 1 (Translated)`.
           - For generated diagrams such as circuits, graphs, ray diagrams, geometry, force vectors, and simple chemical structures, use native inline SVG with a `viewBox`, explicit dimensions, and padding so Chrome renders the complete diagram into the PDF. Do not use external image URLs or JavaScript-dependent graphics. Use a local PNG only when an accurate SVG is impractical, such as detailed biological anatomy.
           - IMPORTANT: For every mathematical expression, preserve the literal LaTeX backslashes in the final HTML source. Example: use `$$\\frac{{u^2 \\cos^2 \\theta}}{{g}}$$` and `\\sin\\theta`, not truncated text like `rac{{...}}` or `cos^2` without the leading backslash.
           - Write a Python script using the Headless Chrome CLI to compile the PDF:
             ```python
             import subprocess
             subprocess.run([
                 "google-chrome", "--headless", "--disable-gpu", "--no-sandbox",
                 "--disable-dev-shm-usage", "--run-all-compositor-stages-before-draw",
                 "--virtual-time-budget=5000", "--no-pdf-header-footer",
                 "--print-to-pdf={artifact_base}.pdf", "{artifact_base}.html"
             ], check=True)
             ```
        4. **Upload PDF Output**:
           - Upload the compiled `{artifact_base}.pdf` to `{upload_url}` via Python `requests.post`.
        """
        post_file_name = f"{artifact_base}.pdf"

    return action_instructions, post_file_name

# =======================================================================
# 3. Sandbox Execution Engine
# =======================================================================

def run_remote_sandbox(client, agent_name, prompt, gcp_token, agents_md_content, verbose):
    """Executes a remote interaction with the active Bearer token authorized."""
    print("🚀 Provisioning remote execution sandbox...")
    api_key = os.environ.get("GEMINI_API_KEY")
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
            },
            # 🐛 FIX: 'env' must be nested INSIDE the environment configuration!
            "env": {
                "GEMINI_API_KEY": api_key,
            }
        }
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
    agents_md_content = assemble_prompt_files(rules_dict, mode="extract", output_format="xml")
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
    agents_md_content = assemble_prompt_files(
        rules_dict,
        mode="generate-chapter",
        output_format=args.output_format,
        pdf_engine=args.pdf_engine,
    )
    source_files = [f for f in Path(args.input_dir).rglob("*") if f.suffix.lower() in [".pdf", ".md"]]

    for fpath in source_files:
        print(f"\n{'='*60}\n📚 Mode: Generate Chapter ({args.output_format.upper()}) | File: {fpath.name}")
        upload_to_gcs(fpath, args.bucket_name, verbose=args.verbose)

        output_filename = f"{fpath.stem}_synthetic.{args.output_format}"
        gcs_upload_path = f"output/{output_filename}"
        download_url = f"https://storage.googleapis.com/{args.bucket_name}/processing_queue/{fpath.name.replace(' ', '_')}"
        upload_url = f"https://storage.googleapis.com/upload/storage/v1/b/{args.bucket_name}/o?uploadType=media&name={gcs_upload_path}"

        page_rule = f"Process only pages {args.page_range[0]} to {args.page_range[1]}." if args.page_range else "Process all content."
        mime_type = "application/pdf" if args.output_format == "pdf" else "application/xml"

        if args.output_format == "pdf":
            artifact_base = f"{fpath.stem}_synthetic"
            action_instructions, post_file_name = build_pdf_action_instructions(args.pdf_engine, artifact_base, upload_url)
        else:
            action_instructions = f"""
            3. **Synthesize XML**: Synthesize high-quality practice questions adhering strictly to `.agents/AGENTS.md`. Use inline SVG for diagrams where needed.
            4. **Upload XML Output**:
               - Save XML as `{output_filename}`.
               - Upload to `{upload_url}` via Python `requests.post`.
            """
            post_file_name = output_filename

        # Only pass tag instructions if we are generating XML and tags actually exist
        tags_instruction = f"- **Global Tags**: {args.tags}" if args.output_format == "xml" and args.tags else ""
        execution_prompt = f"""
        You are an autonomous synthetic question generator.
        Your goal is to read the chapter document mounted at `{fpath.name}` and generate a valid {args.output_format.upper()} question bank.

        ### ⚙️ Generation Constraints:
        - **Target Standards**: {args.standards}
        - **Target Languages**: {args.languages} (CRITICAL: Every single question stem, option, answer key, and STEP-BY-STEP DETAILED SOLUTION MUST be strictly stacked bilingual English + Target Language. Do NOT output solutions or explanations in English only!).
        - **Target Difficulty**: {args.difficulty.upper()}
        - **Max Questions**: {args.num_questions}
        - **Scope**: {page_rule}
        - **PDF Generation Engine**: {args.pdf_engine.upper()} (HTML/Chrome or LaTeX compiler)
        {tags_instruction}

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
        {action_instructions}

        **Upload Script Template (Step 4):**
        ```python
        import requests
        headers = {{"Content-Type": "{mime_type}", "Authorization": "Bearer {gcp_token}"}}
        resp = requests.post("{upload_url}", headers=headers, data=open("{post_file_name}", "rb").read())
        if resp.status_code not in [200, 201]: raise Exception(f"Upload failed: {{resp.text}}")
        ```
        """
        run_remote_sandbox(client, args.agent_name, execution_prompt, gcp_token, agents_md_content, args.verbose)
        download_from_gcs(args.bucket_name, gcs_upload_path, Path(args.output_dir) / output_filename)


def cmd_generate_mock(args, client, gcp_token, rules_dict):
    """Generate-Mock mode: Single-request synthesis of full exam papers from blueprints."""
    agents_md_content = assemble_prompt_files(
        rules_dict,
        mode="generate-mock",
        output_format=args.output_format,
        pdf_engine=args.pdf_engine,
    )

    if not args.blueprint.exists():
        print(f"❌ Blueprint file not found: {args.blueprint}")
        return

    with open(args.blueprint, "r", encoding="utf-8") as f:
        blueprint = json.load(f)

    exam_name = blueprint.get("exam_name", "MOCK_EXAM")
    subjects = blueprint.get("subjects", [])

    diff_map = {k.strip().lower(): float(v.strip()) for k, v in (pair.split(":") for pair in args.difficulty_mix.split(","))}
    diff_total = sum(diff_map.values())
    diff_map = {k: v/diff_total for k, v in diff_map.items()}

    print(f"\n{'='*60}\n🎓 Mode: Generate Mock ({args.output_format.upper()}) | Exam: {exam_name}")
    print("  Aggregating all subject syllabi specified in blueprint...")

    syllabus_blocks = []
    for subj in subjects:
        subj_name = subj.get("name", "Subject")
        total_qs = subj.get("total_questions", 0)

        s_path = Path(subj.get("syllabus_file", ""))
        content = ""
        if s_path.exists():
            content = s_path.read_text(encoding="utf-8")
            print(f"  ✓ Loaded syllabus for {subj_name} ({s_path.name})")
        else:
            print(f"  ⚠️ Warning: Syllabus file '{s_path}' not found.")

        block = (
            f"### SUBJECT: {subj_name.upper()}\n"
            f"- Total Questions Required: {total_qs}\n"
            f"Syllabus Scope:\n{content}\n"
        )
        syllabus_blocks.append(block)

    all_syllabi_text = "\n" + "="*40 + "\n\n".join(syllabus_blocks)

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

    output_filename = f"mock_{exam_name.lower()}_full_bank.{args.output_format}"
    gcs_upload_path = f"output/{output_filename}"
    upload_url = f"https://storage.googleapis.com/upload/storage/v1/b/{args.bucket_name}/o?uploadType=media&name={gcs_upload_path}"
    mime_type = "application/pdf" if args.output_format == "pdf" else "application/xml"

    if args.output_format == "pdf":
        artifact_base = "exam_paper"
        action_instructions, post_file_name = build_pdf_action_instructions(args.pdf_engine, artifact_base, upload_url)
    else:
        action_instructions = f"""
        2. **Synthesize Complete Question Bank (XML)**:
           - Generate all required questions for EVERY subject according to the blueprint.
           - Render diagrams using inline vector `<svg>` elements inside CDATA.
           - Wrap all questions inside a single `<quiz>` root document saved to `{output_filename}`.
        3. **Upload Consolidated XML to GCS**:
           - Upload `{output_filename}` directly to GCS.
        """
        post_file_name = output_filename

    # Only pass tag instructions if we are generating XML and tags actually exist
    tags_instruction = f"- **Global Tags**: {args.tags}" if args.output_format == "xml" and args.tags else ""
    execution_prompt = f"""
    You are an autonomous master test construction and typesetting agent.
    Your objective is to synthesize a complete, calibrated, full-length exam for **{exam_name}** in a SINGLE execution.

    ### ⚙️ Global Exam Blueprint & Constraints:
    - **Exam Standard**: {args.standards}
    - **Target Languages**: {args.languages} (CRITICAL: Every single question stem, option, answer key, and STEP-BY-STEP DETAILED SOLUTION MUST be strictly stacked bilingual English + Target Language. Do NOT output solutions or explanations in English only!).
    - **Difficulty Breakdown Ratio**: {args.difficulty_mix} (e.g. easy:0.2, medium:0.5, hard:0.3 ratio across subjects).
    - **PDF Generation Engine**: {args.pdf_engine.upper()} (HTML/Chrome or LaTeX compiler)
    {tags_instruction}
    - **Output Format**: {args.output_format.upper()}

    ### 📚 Combined Syllabus Scope:
    {all_syllabi_text}

    ### 📋 Workflow Steps:
    1. **Setup Environment**:
       ```python
       import requests, os
       os.makedirs("/workspace/pdfs", exist_ok=True)
       os.makedirs("/workspace/output", exist_ok=True)
       {sample_download_script}
       ```
    {action_instructions}

    **Upload Script Template (Step 3):**
    ```python
    import requests
    url = "{upload_url}"
    headers = {{
        "Content-Type": "{mime_type}",
        "Authorization": "Bearer {gcp_token}"
    }}
    with open("{post_file_name}", "rb") as f:
        data = f.read()

    response = requests.post(url, headers=headers, data=data)
    print(f"GCS Upload Status Code: {{response.status_code}}")
    print(f"GCS Response: {{response.text}}")
    if response.status_code not in [200, 201]:
        raise RuntimeError(f"GCS Upload failed with status {{response.status_code}}: {{response.text}}")
    ```
    """

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
    shared_parser.add_argument("--output-format", choices=["xml", "pdf"], default="xml", help="Choose output format (xml or pdf).")
    shared_parser.add_argument("--pdf-engine", choices=["html", "tex"], default="html", help="Choose PDF renderer: html (Chrome) or tex (LaTeX).")

    # Core System Prompt Rule Files
    shared_parser.add_argument("--prompt", required=True, type=Path, help="Path to the main exam prompt (e.g. jee_main.md)")
    shared_parser.add_argument("--instruction-file", default=None, type=Path, help="Path to exam instruction file")

    # XML Specific Rules
    shared_parser.add_argument("--xml-rules", default="prompts/core/moodle_xml_rules.md", type=Path)
    shared_parser.add_argument("--tags-rules", default="prompts/core/naming_and_tags_rules.md", type=Path)
    shared_parser.add_argument("--templates", default="prompts/core/moodle_xml_templates.md", type=Path)

    # PDF Specific Rules
    shared_parser.add_argument("--pdf-rules", default=None, type=Path, help="Legacy single PDF rules file; optional compatibility fallback")
    shared_parser.add_argument("--pdf-rules-html", default="prompts/core/pdf_html_rules.md", type=Path)
    shared_parser.add_argument("--pdf-rules-tex", default="prompts/core/pdf_tex_rules.md", type=Path)

    # 2. Create the Main parser and attach the shared parser to subcommands
    parser = argparse.ArgumentParser(description="academic_content_pipeline.py - Master Unified Academic Content Pipeline")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    # Subcommand Mode 1: extract (Only supports XML)
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

    # Hard-enforce extract mode to be XML only to prevent logic breakage
    if args.mode == "extract" and args.output_format == "pdf":
        print("❌ Error: 'extract' mode only supports --output-format xml.")
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
        "pdf_rules": args.pdf_rules,
        "pdf_rules_html": args.pdf_rules_html,
        "pdf_rules_tex": args.pdf_rules_tex,
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