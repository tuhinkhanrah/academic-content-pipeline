import os
import sys
import argparse
import subprocess
from pathlib import Path
from google import genai

def get_gcloud_access_token() -> str:
    """Fetch active GCP OAuth access token from local gcloud CLI."""
    try:
        return subprocess.check_output(
            ["gcloud", "auth", "print-access-token"], text=True
        ).strip()
    except subprocess.CalledProcessError:
        print("❌ Error: Failed to fetch access token. Make sure you ran 'gcloud auth login'.")
        sys.exit(1)

def upload_single_pdf_to_gcs(pdf_path: Path, bucket_name: str, verbose: bool = False) -> str:
    """Uploads a single PDF to GCS, skipping if it already exists."""
    # Create a safe, unique path in GCS to avoid overwrites
    safe_filename = pdf_path.name.replace(" ", "_")
    gcs_target_uri = f"gs://{bucket_name}/processing_queue/{safe_filename}"

    # 1. Check if the file is already in the bucket
    try:
        subprocess.run(
            ["gcloud", "storage", "ls", gcs_target_uri], 
            check=True, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL
        )
        print(f"⚡ {pdf_path.name} already exists in GCS. Skipping upload.")
        return gcs_target_uri
    except subprocess.CalledProcessError:
        # If 'ls' fails, the file is not there. Proceed with upload.
        pass

    # 2. Upload the file
    print(f"🔄 Uploading {pdf_path.name} to GCS...")
    cmd = ["gcloud", "storage", "cp", str(pdf_path), gcs_target_uri]
    
    try:
        if verbose:
            subprocess.run(cmd, check=True)
        else:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return gcs_target_uri
    except subprocess.CalledProcessError as e:
        print(f"❌ Failed to upload {pdf_path.name} to GCS: {e}")
        return None

def assemble_prompt_files(rule_files: list) -> str:
    """Combine specified prompt markdown files into a single AGENTS.md instruction context."""
    content_blocks = ["# AGENTS.md - Moodle Extraction System Rules\n"]
    loaded_count = 0

    for filepath in rule_files:
        if not filepath:
            continue
        path = Path(filepath)
        if path.exists():
            print(f"   └── Injecting prompt: {path.name}")
            with open(path, "r", encoding="utf-8") as f:
                content_blocks.append(f"## File: {path.name}\n\n" + f.read() + "\n\n---\n")
            loaded_count += 1
        else:
            print(f"⚠️ Warning: Prompt file '{filepath}' not found. Skipping.")

    if loaded_count == 0:
        content_blocks.append("You are a domain-agnostic exam paper parser converting PDFs into Moodle XML.")

    return "\n".join(content_blocks)

def run_gcs_extraction_pipeline(args):
    # 1. Fetch GCP Credentials
    gcp_token = get_gcloud_access_token()

    # 2. Assemble specified prompt rule files into AGENTS.md
    print("📚 Assembling prompt files for sandbox context...")
    prompt_files = [args.prompt, args.instruction_file, args.xml_rules, args.tags_rules, args.templates]
    agents_md_content = assemble_prompt_files(prompt_files)

    # 3. Find all PDFs recursively in the local directory
    input_dir_path = Path(args.papers_dir).resolve()
    pdf_files = list(input_dir_path.rglob("*.pdf"))

    if not pdf_files:
        print(f"❌ No PDF files found in {input_dir_path}")
        return

    print(f"🔍 Found {len(pdf_files)} PDF(s). Starting sequential processing loop...\n")
    client = genai.Client()

    for pdf_path in pdf_files:
        print(f"\n{'='*60}")
        print(f"📄 Processing: {pdf_path.name}")
        print(f"📁 Directory: {pdf_path.parent}")
        
        # 3a. Upload single PDF
        gcs_uri = upload_single_pdf_to_gcs(pdf_path, args.bucket_name, args.verbose)
        if not gcs_uri:
            continue # Skip to next if upload fails

        # 3b. Setup Dynamic Filenames
        output_filename = f"{pdf_path.stem}_moodle.xml"
        gcs_upload_path = f"output/{output_filename}"
        bucket_upload_url = f"https://storage.googleapis.com/upload/storage/v1/b/{args.bucket_name}/o?uploadType=media&name={gcs_upload_path}"

        # 3c. Construct Prompt for this specific file
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

        ### 🛠️ Workflow Steps:
        1. **Render Pages**: Write and run a Python script using `pymupdf` (PyMuPDF) to convert the specified pages of the PDF `/workspace/pdfs/input.pdf` into high-resolution PNG images in `/workspace/images/`.
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
             headers = {{"Content-Type": "application/xml"}}
             response = requests.post(url, headers=headers, data=data)

             print(f"GCS Upload Status Code: {{response.status_code}}")
             print(f"GCS Response: {{response.text}}")

             # CRITICAL: Raise error if upload failed so the process halts explicitly
             if response.status_code not in [200, 201]:
                 raise RuntimeError(f"GCS Upload failed with status {{response.status_code}}: {{response.text}}")
             ```
           - Ensure the script prints the exact HTTP status code and response body to the terminal logs.
        """

        # 3d. Execute Remote Interaction
        try:
            print(f"🚀 Provisioning remote sandbox for {pdf_path.name}...")
            interaction = client.interactions.create(
                agent="antigravity-preview-05-2026",
                input=execution_prompt,
                environment={
                    "type": "remote",
                    "sources": [
                        {
                            "type": "gcs", 
                            "source": gcs_uri, 
                            "target": "/workspace/pdfs/input.pdf"
                        },
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
            
            if args.verbose:
                print("\n✅ Agent finished execution. Execution Logs:")
                print("=" * 60)
                print(interaction.output_text)
                print("=" * 60)
                
        except Exception as e:
            print(f"❌ Sandbox execution failed for {pdf_path.name}: {e}")
            continue

        # 3e. Retrieve Final XML from GCS to the exact local PDF folder
        print(f"📥 Fetching final XML to {pdf_path.parent}...")
        gcs_output_uri = f"gs://{args.bucket_name}/{gcs_upload_path}"
        local_output_path = pdf_path.parent / output_filename
        
        try:
            subprocess.run(["gcloud", "storage", "cp", gcs_output_uri, str(local_output_path)], check=True)
            print(f"🎉 Success! Saved to: {local_output_path}")
        except subprocess.CalledProcessError:
            print(f"❌ Failed to download {output_filename} from GCS. Check the agent's Python execution logs to ensure the upload succeeded.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Paper2Moodle Universal Cloud Agent Pipeline")
    
    # Core Data Inputs
    parser.add_argument("--papers-dir", required=True, help="Local directory containing question paper PDFs")
    parser.add_argument("--bucket-name", default="moodle-papers-bucket-12345", help="GCS bucket name for staging")
    parser.add_argument("--languages", type=str, required=True, help="Target languages (e.g. 'english,bengali')")
    
    # Prompt Rule Files
    parser.add_argument("-p", "--prompt", type=Path, help="Path to system prompt markdown file.")
    parser.add_argument("--xml-rules", help="Path to moodle_xml_rules.md")
    parser.add_argument("--tags-rules", help="Path to naming_and_tags_rules.md")
    parser.add_argument("--templates", help="Path to moodle_xml_templates.md")
    parser.add_argument("--instruction-file", help="Path to exam instruction file (e.g. neet.md, jee.md, general.md)")
    
    # Filtering / Page Options
    parser.add_argument("--no-instruction-page", action="store_true", help="Skip instruction pages")
    parser.add_argument("--page-range", type=int, nargs=2, metavar=('START', 'END'), help="Page range to extract")
    
    # Runtime Flags
    parser.add_argument("--verbose", action="store_true", help="Print debug logs and assembled prompt")

    args = parser.parse_args()

    if not os.environ.get("GEMINI_API_KEY"):
        print("❌ Error: GEMINI_API_KEY environment variable is not set.")
        sys.exit(1)

    run_gcs_extraction_pipeline(args)