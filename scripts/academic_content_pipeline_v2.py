#!/usr/bin/env python3
"""
academic_content_pipeline.py - Master Unified Academic Content Pipeline
Modes:
  1. extract          (Two-Pass Extraction: Structure -> Crop -> Solve -> XML)
  2. generate-chapter (Synthesize questions from chapter PDFs/MDs)
  3. generate-mock    (Synthesize full mock exams from JSON blueprints)
"""

import os
import sys
import time
import argparse
import subprocess
import json
import re
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

def assemble_prompt_files(
    rule_files: dict,
    mode: str,
    output_format: str = "xml",
    pdf_engine: str = "html",
    verify_online: bool = False,
) -> str:
    content_blocks = ["# AGENTS.md - System Rules\n"]

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
                if name == "xml_rules" and mode == "extract" and not verify_online:
                    content = re.sub(
                        r"\n# Mandatory Online Answer Verification\n.*?(?=\n# Feedback Rules & Reasoning Structure\n)",
                        "\n",
                        content,
                        flags=re.DOTALL,
                    )
                content_blocks.append(f"## File: {path.name}\n\n{content}\n\n---\n")
        else:
            print(f"⚠️ Warning: Prompt file '{filepath}' not found. Skipping.")

    return "\n".join(content_blocks)

def build_pdf_action_instructions(pdf_engine: str, artifact_base: str, upload_url: str) -> tuple[str, str]:
    if pdf_engine == "tex":
        action_instructions = f"""
        3. **Synthesize LaTeX & Compile PDF**:
           - Create a complete standalone LaTeX document named `{artifact_base}.tex` using `article`/`exam`-style structure.
           - CRITICAL: Ensure no text extends beyond the right margin or gets cut off.
           - Use native LaTeX for all equations and labels, and keep bilingual output in a clean layout.
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
           - Compile with `xelatex -interaction=nonstopmode -halt-on-error {artifact_base}.tex`.
        4. **Upload PDF Output**:
           - Upload the compiled `{artifact_base}.pdf` to `{upload_url}` via Python `requests.post`.
        """
        post_file_name = f"{artifact_base}.pdf"
    else:
        action_instructions = f"""
        3. **Synthesize HTML & Compile PDF**:
           - Output a clean, complete HTML file (`{artifact_base}.html`) correctly importing KaTeX and Google Web Fonts.
           - For generated diagrams, use native inline SVG.
           - Write a Python script using Headless Chrome CLI to compile the PDF:
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
    print("🚀 Provisioning remote execution sandbox...")
    api_key = os.environ.get("GEMINI_API_KEY")

    for attempt in range(3):
        try:
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

        except Exception as e:
            print(f"⚠️ Sandbox API Error (Attempt {attempt+1}/3): {e}")
            if attempt < 2:
                print("Retrying in 15 seconds...")
                time.sleep(15)
            else:
                print("❌ Max retries reached. Google GenAI API is likely down.")
                sys.exit(1)

# =======================================================================
# 4. Mode Implementations
# =======================================================================

def cmd_extract(args, client, gcp_token, rules_dict):
    agents_md_content = assemble_prompt_files(
        rules_dict,
        mode="extract",
        output_format="xml",
        verify_online=args.verify_online,
    )
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
        You are an autonomous exam paper processing agent.
        Your goal is to extract all exam questions from the PDF file mounted at `/workspace/pdfs/input.pdf`
        and generate a single, strictly valid Moodle XML question bank saved to `/workspace/output/{output_filename}`.

        ### ⚙️ Extraction Parameters:
        - **Target Languages**: {args.languages}
        - **Page Range**: {page_rule}
        - **Instructions Rule**: {instruction_rule}

        ### 📋 Execution Plan:
        Write and execute a Python script (`extract.py`) in your workspace to perform a strictly decoupled TWO-PASS extraction pipeline.

        Execute the script using `python3 extract.py`. Here is the exact Python script implementation you must create and run:

        ```python
        import os, sys, base64, io, json, time, requests, pymupdf
        from PIL import Image
        from google import genai
        from google.genai import types
        from pydantic import BaseModel, Field

        # Load system rules for Gemini to follow strictly
        system_rules = ""
        if os.path.exists(".agents/AGENTS.md"):
            with open(".agents/AGENTS.md", "r", encoding="utf-8") as f:
                system_rules = f.read()

        # =========================================================
        # 1. SCHEMAS: PASS 1 (THE EYES - EXACT PDF2HTML CLONE)
        # =========================================================
        class DiagramBox(BaseModel):
            box_2d: list[int] = Field(description="Bounding box [ymin, xmin, ymax, xmax] normalized to 0-1000")

        class MCQOptionPass1(BaseModel):
            text: str = Field(description="The text for this option. Leave empty if it is purely graphical.")
            diagram: DiagramBox | None = Field(description="Bounding box if this specific option is a graph, circuit, or image", default=None)

        class ExtractedQuestionPass1(BaseModel):
            question_number: str = Field(description="Question number (e.g., '22')")
            question_html: str = Field(description="The question stem text formatted as HTML")
            question_diagram: DiagramBox | None = Field(description="Bounding box of the diagram in the question stem, if present", default=None)
            options: list[MCQOptionPass1] = Field(description="Exactly 4 multiple choice options", default_factory=list)

        class PageExtractionPass1(BaseModel):
            is_question_page: bool = Field(description="True if this page contains questions.")
            questions: list[ExtractedQuestionPass1] = Field(description="List of MCQs found", default_factory=list)

        # =========================================================
        # 2. SCHEMAS: PASS 2 (THE BRAIN - SOLVER & TAGGER)
        # =========================================================
        class TagItem(BaseModel):
            key: str = Field(description="Tag key in lowercase e.g. standard, subject, chapter")
            value: str = Field(description="Tag value in lowercase snake_case")

        class QuestionSolutionPass2(BaseModel):
            question_name: str = Field(description="Dynamic Question Name e.g. JEEMAIN_PHYSICS_2026_Q01 - Title snippet")
            question_type: str = Field(description="'multichoice' or 'numerical'")
            is_single_choice: bool = Field(default=True)
            correct_option_indices: list[int] = Field(default_factory=list)
            numerical_answer: str | None = Field(default=None)
            tolerance: float = Field(default=0.0)
            default_grade: float = Field(default=4.0)
            penalty: float = Field(default=0.25)
            answernumbering: str = Field(default="abc")
            shuffleanswers: bool = Field(default=True)
            step_by_step_solution: str = Field(description="Detailed 5-step solution formatted in HTML")
            tags: list[TagItem] = Field(default_factory=list)

        # =========================================================
        # 3. HELPER FUNCTIONS
        # =========================================================
        def get_cropped_pil(img, box_2d, padding_pct=0.04):
            if not box_2d or len(box_2d) != 4: return None
            ymin, xmin, ymax, xmax = box_2d
            
            if ymin > ymax: ymin, ymax = ymax, ymin
            if xmin > xmax: xmin, xmax = xmax, xmin

            width, height = img.size
            pad_x = (xmax - xmin) * padding_pct
            pad_y = (ymax - ymin) * padding_pct

            xmin_pad = max(0, xmin - pad_x)
            ymin_pad = max(0, ymin - pad_y)
            xmax_pad = min(1000, xmax + pad_x)
            ymax_pad = min(1000, ymax + pad_y)

            left = int((xmin_pad / 1000.0) * width)
            top = int((ymin_pad / 1000.0) * height)
            right = int((xmax_pad / 1000.0) * width)
            bottom = int((ymax_pad / 1000.0) * height)
            
            if right <= left or bottom <= top: return None
            return img.crop((left, top, right, bottom))

        def pil_to_base64(cropped_img):
            if not cropped_img: return ""
            buffered = io.BytesIO()
            cropped_img.save(buffered, format="PNG")
            return base64.b64encode(buffered.getvalue()).decode("utf-8")

        # =========================================================
        # 4. MAIN PIPELINE
        # =========================================================
        os.makedirs("/workspace/pdfs", exist_ok=True)
        os.makedirs("/workspace/output", exist_ok=True)

        pdf_url = "[https://storage.googleapis.com/](https://storage.googleapis.com/){args.bucket_name}/processing_queue/{safe_filename}"
        headers = {{"Authorization": "Bearer {gcp_token}"}}
        r = requests.get(pdf_url, headers=headers)
        if r.status_code == 200:
            with open("/workspace/pdfs/input.pdf", "wb") as f:
                f.write(r.content)
            print("PDF Downloaded.")
        else:
            raise Exception("Failed to download PDF")

        doc = pymupdf.open("/workspace/pdfs/input.pdf")
        total_pages = len(doc)
        client = genai.Client()

        xml_output = '<?xml version="1.0" encoding="UTF-8"?>\\n<quiz>\\n'
        question_global_counter = 1

        for page_idx in range(total_pages):
            page_num = page_idx + 1
            print(f"\\n--- Processing Page {{page_num}}/{{total_pages}} ---")
            page = doc[page_idx]
            pix = page.get_pixmap(dpi=200)
            img = Image.open(io.BytesIO(pix.tobytes("png")))

            # ---------------------------------------------------------
            # PASS 1: THE EYES (Extract Structure & Bounding Boxes)
            # ---------------------------------------------------------
            prompt_pass_1 = (
                "Extract all MCQs. "
                "Ensure MathJax is wrapped in \\\\(...\\\\) or \\\\[...\\\\]. "
                "CRITICAL VISUAL RULE: If the 4 options are graphs, circuits, or diagrams, you MUST return the bounding box "
                "for EACH option individually inside the `options[].diagram` field. Do NOT group option graphs into the main "
                "question diagram. Exclude question text from bounding boxes."
            )
            
            page_data = None
            for attempt in range(5):
                try:
                    chat1 = client.chats.create(model="gemini-3.7-flash")
                    resp1 = chat1.send_message(
                        message=[img, prompt_pass_1],
                        config=types.GenerateContentConfig(
                            response_mime_type="application/json", 
                            response_schema=PageExtractionPass1, 
                            temperature=0.1
                        )
                    )
                    page_data = json.loads(resp1.text)
                    break
                except Exception as e:
                    print(f"Pass 1 Attempt {{attempt+1}} failed: {{e}}")
                    time.sleep(10)

            if not page_data or not page_data.get("is_question_page") or not page_data.get("questions"):
                print(f"Skipping page {{page_num}} (No concluding questions).")
                continue

            for mcq_dict in page_data.get("questions", []):
                q_pass1 = ExtractedQuestionPass1(**mcq_dict)
                q_id = f"q_{{question_global_counter}}"
                question_global_counter += 1

                # Step 1A: Crop Main Diagram safely
                q_img_pil = None
                q_files_xml = ""
                q_img_tag = ""
                if q_pass1.question_diagram and q_pass1.question_diagram.box_2d:
                    q_img_pil = get_cropped_pil(img, q_pass1.question_diagram.box_2d)
                    b64 = pil_to_base64(q_img_pil)
                    if b64:
                        filename = f"{{q_id}}_main.png"
                        q_img_tag = f'<br/><img src="@@PLUGINFILE@@/{{filename}}" alt="Question Diagram"/><br/>'
                        q_files_xml = f'<file name="{{filename}}" path="/" encoding="base64">{{b64}}</file>\\n'

                q_text_full = f"{{q_pass1.question_html}} {{q_img_tag}}"

                # Step 1B: Crop Option Diagrams (Hold for Pass 2 context & XML) safely
                options_data = []
                for opt_idx, opt in enumerate(q_pass1.options or []):
                    opt_pil = None
                    opt_files_xml = ""
                    opt_img_tag = ""
                    if opt.diagram and opt.diagram.box_2d:
                        opt_pil = get_cropped_pil(img, opt.diagram.box_2d)
                        b64_opt = pil_to_base64(opt_pil)
                        if b64_opt:
                            opt_filename = f"{{q_id}}_opt_{{opt_idx}}.png"
                            opt_img_tag = f'<br/><img src="@@PLUGINFILE@@/{{opt_filename}}" alt="Option Diagram"/><br/>'
                            opt_files_xml = f'\\n      <file name="{{opt_filename}}" path="/" encoding="base64">{{b64_opt}}</file>'
                    
                    options_data.append({{
                        "text": opt.text if opt.text else "",
                        "tag": opt_img_tag,
                        "xml": opt_files_xml,
                        "pil": opt_pil
                    }})

                # ---------------------------------------------------------
                # PASS 2: THE BRAIN (Solve & Tag)
                # ---------------------------------------------------------
                print(f"  Generating solution for Question {{question_global_counter-1}}...")
                
                pass_2_prompt_text = (
                    "You are the Expert Solver. Review the following question text and attached cropped diagrams (if any). "
                    "Determine the correct answer, build the 5-step solution, and assign appropriate metadata tags.\\n\\n"
                    f"Question Text:\\n{{q_pass1.question_html}}\\n\\n"
                )
                if options_data:
                    pass_2_prompt_text += "Options:\\n"
                    for i, opt in enumerate(options_data):
                        pass_2_prompt_text += f"Index {{i}}: {{opt['text']}}\\n"

                pass_2_payload = [pass_2_prompt_text]
                if q_img_pil: pass_2_payload.append(q_img_pil)
                for opt in options_data:
                    if opt["pil"]: pass_2_payload.append(opt["pil"])
                
                solution_data = None
                for attempt in range(5):
                    try:
                        chat2 = client.chats.create(model="gemini-3.7-flash")
                        resp2 = chat2.send_message(
                            message=pass_2_payload,
                            config=types.GenerateContentConfig(
                                system_instruction=system_rules,
                                response_mime_type="application/json", 
                                response_schema=QuestionSolutionPass2, 
                                temperature=0.2
                            )
                        )
                        solution_data = json.loads(resp2.text)
                        break
                    except Exception as e:
                        print(f"Pass 2 Attempt {{attempt+1}} failed: {{e}}")
                        time.sleep(10)

                if not solution_data:
                    print("  Failed to generate solution. Using fallback.")
                    solution_data = {{
                        "question_name": f"Question {{question_global_counter-1}}",
                        "question_type": "multichoice",
                        "is_single_choice": True,
                        "correct_option_indices": [0],
                        "numerical_answer": None,
                        "tolerance": 0.0,
                        "default_grade": 4.0,
                        "penalty": 0.25,
                        "answernumbering": "abc",
                        "shuffleanswers": True,
                        "step_by_step_solution": "<p>Solution generation failed.</p>",
                        "tags": []
                    }}
                
                q_pass2 = QuestionSolutionPass2(**solution_data)

                # ---------------------------------------------------------
                # STEP 3: Moodle XML Assembly
                # ---------------------------------------------------------
                tags_xml = "<tags>\\n"
                for t in q_pass2.tags:
                    tags_xml += f'    <tag><text>{{t.key}}:{{t.value}}</text></tag>\\n'
                tags_xml += "</tags>"

                if q_pass2.question_type == "numerical":
                    num_val = q_pass2.numerical_answer if q_pass2.numerical_answer else "0"
                    xml_block = f'''  <question type="numerical">
    <name><text>{{q_pass2.question_name}}</text></name>
    <questiontext format="html">
      <text><![CDATA[{{q_text_full}}]]></text>
      {{q_files_xml}}</questiontext>
    <generalfeedback format="html">
      <text><![CDATA[{{q_pass2.step_by_step_solution}}]]></text>
    </generalfeedback>
    <defaultgrade>{{q_pass2.default_grade}}</defaultgrade>
    <penalty>{{q_pass2.penalty}}</penalty>
    <answer fraction="100" format="moodle_auto_format">
      <text>{{num_val}}</text>
      <tolerance>{{q_pass2.tolerance}}</tolerance>
    </answer>
    <unitgradingtype>0</unitgradingtype>
    <unitpenalty>0.1000000</unitpenalty>
    <showunits>3</showunits>
    {{tags_xml}}
  </question>\\n'''
                else:
                    single_str = "true" if q_pass2.is_single_choice else "false"
                    shuffle_str = "true" if q_pass2.shuffleanswers else "false"

                    num_correct = len(q_pass2.correct_option_indices)
                    pos_fraction = (100.0 / num_correct) if num_correct > 0 else 100.0
                    neg_fraction = -abs(q_pass2.penalty * 100) if q_pass2.is_single_choice else -50.0

                    answers_xml = ""
                    for opt_idx, opt in enumerate(options_data):
                        is_correct = opt_idx in q_pass2.correct_option_indices
                        fraction = pos_fraction if is_correct else neg_fraction
                        opt_text_full = f"{{opt['text']}} {{opt['tag']}}"

                        answers_xml += f'''    <answer fraction="{{fraction:.7f}}" format="html">
      <text><![CDATA[{{opt_text_full}}]]></text>{{opt['xml']}}
    </answer>\\n'''

                    xml_block = f'''  <question type="multichoice">
    <name><text>{{q_pass2.question_name}}</text></name>
    <questiontext format="html">
      <text><![CDATA[{{q_text_full}}]]></text>
      {{q_files_xml}}</questiontext>
    <generalfeedback format="html">
      <text><![CDATA[{{q_pass2.step_by_step_solution}}]]></text>
    </generalfeedback>
    <defaultgrade>{{q_pass2.default_grade}}</defaultgrade>
    <penalty>{{q_pass2.penalty}}</penalty>
    <hidden>0</hidden>
    <single>{{single_str}}</single>
    <shuffleanswers>{{shuffle_str}}</shuffleanswers>
    <answernumbering>{{q_pass2.answernumbering}}</answernumbering>
{{answers_xml}}    {{tags_xml}}
  </question>\\n'''

                xml_output += xml_block

        xml_output += "</quiz>"

        with open("/workspace/output/{output_filename}", "w", encoding="utf-8") as f:
            f.write(xml_output)
        print("XML written successfully to /workspace/output/{output_filename}.")

        print("Uploading generated XML to GCS...")
        upload_url = "{bucket_upload_url}"
        up_headers = {{"Content-Type": "application/xml", "Authorization": "Bearer {gcp_token}"}}
        with open("/workspace/output/{output_filename}", "rb") as f:
            data = f.read()

        up_resp = requests.post(upload_url, headers=up_headers, data=data)
        print(f"GCS Upload Status Code: {{up_resp.status_code}}")
        if up_resp.status_code not in [200, 201]:
            raise RuntimeError("GCS Upload failed")
        ```
        """

        run_remote_sandbox(client, args.agent_name, execution_prompt, gcp_token, agents_md_content, args.verbose)
        download_from_gcs(args.bucket_name, gcs_upload_path, pdf_path.parent / output_filename)


def cmd_generate_chapter(args, client, gcp_token, rules_dict):
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

        tags_instruction = f"- **Global Tags**: {args.tags}" if args.output_format == "xml" and args.tags else ""
        execution_prompt = f"""
        You are an autonomous synthetic question generator.
        Your goal is to read the chapter document mounted at `{fpath.name}` and generate a valid {args.output_format.upper()} question bank.

        ### ⚙️ Generation Constraints:
        - **Target Standards**: {args.standards}
        - **Target Languages**: {args.languages}
        - **Target Difficulty**: {args.difficulty.upper()}
        - **Max Questions**: {args.num_questions}
        - **Scope**: {page_rule}
        - **PDF Generation Engine**: {args.pdf_engine.upper()}
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

    tags_instruction = f"- **Global Tags**: {args.tags}" if args.output_format == "xml" and args.tags else ""
    execution_prompt = f"""
    You are an autonomous master test construction and typesetting agent.
    Your objective is to synthesize a complete, calibrated, full-length exam for **{exam_name}** in a SINGLE execution.

    ### ⚙️ Global Exam Blueprint & Constraints:
    - **Exam Standard**: {args.standards}
    - **Target Languages**: {args.languages}
    - **Difficulty Breakdown Ratio**: {args.difficulty_mix} (e.g. easy:0.2,medium:0.5,hard:0.3 ratio across subjects).
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
    shared_parser = argparse.ArgumentParser(add_help=False)

    shared_parser.add_argument("--languages", default="english", help="Target languages (e.g. english,bengali)")
    shared_parser.add_argument("--standards", default="general", help="Target standards (e.g. neet_ug, jee_main)")
    shared_parser.add_argument("--tags", default="", help="Global tags (e.g. year:2026)")
    shared_parser.add_argument("--output-dir", default="./output", type=Path)
    shared_parser.add_argument("--bucket-name", required=True, help="GCS Bucket name for staging and output")
    shared_parser.add_argument("--agent-name", default="antigravity-preview-05-2026")
    shared_parser.add_argument("--verbose", action="store_true", help="Print detailed remote execution logs")
    shared_parser.add_argument("--output-format", choices=["xml", "pdf"], default="xml", help="Choose output format (xml or pdf).")
    shared_parser.add_argument("--pdf-engine", choices=["html", "tex"], default="html", help="Choose PDF renderer: html (Chrome) or tex (LaTeX).")

    shared_parser.add_argument("--prompt", required=True, type=Path, help="Path to the main exam prompt (e.g. jee_main.md)")
    shared_parser.add_argument("--instruction-file", default=None, type=Path, help="Path to exam instruction file")

    shared_parser.add_argument("--xml-rules", default="prompts/core/moodle_xml_rules.md", type=Path)
    shared_parser.add_argument("--tags-rules", default="prompts/core/naming_and_tags_rules.md", type=Path)
    shared_parser.add_argument("--templates", default="prompts/core/moodle_xml_templates.md", type=Path)

    shared_parser.add_argument("--extractor-rules", default="prompts/core/extractor_rules.md", type=Path)

    shared_parser.add_argument("--pdf-rules", default=None, type=Path, help="Legacy single PDF rules file; optional compatibility fallback")
    shared_parser.add_argument("--pdf-rules-html", default="prompts/core/pdf_html_rules.md", type=Path)
    shared_parser.add_argument("--pdf-rules-tex", default="prompts/core/pdf_tex_rules.md", type=Path)

    parser = argparse.ArgumentParser(description="academic_content_pipeline.py - Master Unified Academic Content Pipeline")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    ext_parser = subparsers.add_parser("extract", parents=[shared_parser], help="Extract questions from exam paper PDFs")
    ext_parser.add_argument("--input-dir", required=True, type=Path, help="Local directory containing input PDFs")
    ext_parser.add_argument("--page-range", type=int, nargs=2, metavar=('START', 'END'), help="Page range to extract (e.g., 1 10)")
    ext_parser.add_argument("--no-instruction-page", action="store_true", help="Skip front instruction page")
    ext_parser.add_argument(
        "--verify-online",
        action="store_true",
        help="Verify each extracted answer with Google Search before writing XML (slower).",
    )

    chap_parser = subparsers.add_parser("generate-chapter", parents=[shared_parser], help="Synthesize practice questions from chapter documents")
    chap_parser.add_argument("--input-dir", required=True, type=Path)
    chap_parser.add_argument("--page-range", type=int, nargs=2, metavar=('START', 'END'))
    chap_parser.add_argument("--num-questions", type=int, default=5)
    chap_parser.add_argument("--difficulty", choices=["easy", "medium", "hard"], default="medium")

    mock_parser = subparsers.add_parser("generate-mock", parents=[shared_parser], help="Synthesize full mock exam papers from JSON blueprints")
    mock_parser.add_argument("--blueprint", required=True, type=Path)
    mock_parser.add_argument("--sample-pdf", type=Path)
    mock_parser.add_argument("--difficulty-mix", default="easy:0.2,medium:0.5,hard:0.3")

    args = parser.parse_args()

    if not os.environ.get("GEMINI_API_KEY"):
        print("❌ Error: GEMINI_API_KEY environment variable is not set.")
        sys.exit(1)

    if args.mode == "extract" and args.output_format == "pdf":
        print("❌ Error: 'extract' mode only supports --output-format xml.")
        sys.exit(1)

    gcp_token = get_gcloud_access_token()
    client = genai.Client()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    active_core_rules = args.extractor_rules if args.mode == "extract" else args.xml_rules

    rules_dict = {
        "main_prompt": args.prompt,
        "instruction_file": args.instruction_file,
        "xml_rules": active_core_rules,
        "tags_rules": args.tags_rules,
        "templates": args.templates if args.mode != "extract" else None,
        "pdf_rules": args.pdf_rules,
        "pdf_rules_html": args.pdf_rules_html,
        "pdf_rules_tex": args.pdf_rules_tex,
    }

    if args.mode == "extract":
        cmd_extract(args, client, gcp_token, rules_dict)
    elif args.mode == "generate-chapter":
        cmd_generate_chapter(args, client, gcp_token, rules_dict)
    elif args.mode == "generate-mock":
        cmd_generate_mock(args, client, gcp_token, rules_dict)

if __name__ == "__main__":
    main()