#!/usr/bin/env python3
"""
pipeline_utils.py - Shared utilities for question generation, XML/HTML/LaTeX/PDF output, and agent workflows.
"""

import base64
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sys
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import xml.etree.ElementTree as ET

logger = logging.getLogger("academic_content_pipeline")

PROMPT_ROOT = Path(__file__).resolve().parents[2] / "prompts"
LANGUAGE_CATALOG_PATH = PROMPT_ROOT / "core" / "languages.json"
LANGUAGE_RULES_PATH = PROMPT_ROOT / "core" / "language_rules.md"


def load_prompt_template(template_path: Union[Path, str], **values: str) -> str:
    """Load a UTF-8 prompt template and replace its named placeholders."""
    template = Path(template_path).read_text(encoding="utf-8")
    for key, value in values.items():
        template = template.replace("{{" + key + "}}", str(value))
    return template.strip()


def load_prompt_section(template_path: Union[Path, str], heading: str, **values: str) -> str:
    """Load one Markdown level-three section from a prompt template."""
    template = Path(template_path).read_text(encoding="utf-8")
    match = re.search(
        rf"^### {re.escape(heading)}\s*$\n(.*?)(?=^### |\Z)",
        template,
        flags=re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise ValueError(f"Prompt section '{heading}' not found in {template_path}")
    section = match.group(1)
    for key, value in values.items():
        section = section.replace("{{" + key + "}}", str(value))
    return section.strip()


def _load_language_catalog() -> Dict[str, Dict[str, str]]:
    """Load language metadata used to build runtime language instructions."""
    try:
        return json.loads(LANGUAGE_CATALOG_PATH.read_text(encoding="utf-8"))["languages"]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        raise RuntimeError(f"Unable to load language catalog: {LANGUAGE_CATALOG_PATH}") from exc


LANGUAGE_CATALOG = _load_language_catalog()
LANG_ISO_MAP = {name: data["iso"] for name, data in LANGUAGE_CATALOG.items()}
INDIC_LANGUAGE_CONFIG: Dict[str, Dict[str, str]] = {
    name: {
        "polyglossia": data["polyglossia"],
        "script": data["script"],
        "font": data["font"],
        "cmd": data["command"],
    }
    for name, data in LANGUAGE_CATALOG.items()
    if "polyglossia" in data
}


def unique_image_items(image_map: Dict[str, Any]) -> List[Tuple[str, Any]]:
    """Return one attachment per image path while preserving the first reference ID."""
    seen_paths = set()
    unique_items = []
    for image_id, filepath in image_map.items():
        path_key = str(Path(filepath).resolve())
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        unique_items.append((image_id, filepath))
    return unique_items

def load_file_content(file_path: Optional[Union[Path, str]]) -> str:
    """Reads and returns text content from a Path safely."""
    if not file_path:
        return ""
    p = Path(file_path)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.error(f"Failed to read file {file_path}: {e}")
        return ""


def validate_local_image_references(
    document_text: str,
    output_dir: Path,
    image_map: Optional[Dict[str, str]],
    document_format: str,
) -> None:
    """Fail when a PDF document references a source image that is unavailable locally."""
    if not image_map:
        return

    if document_format == "html":
        references = re.findall(r"<img\b[^>]*\bsrc=[\"']([^\"']+)[\"']", document_text, re.IGNORECASE)
    else:
        references = re.findall(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}", document_text)

    available_names = {Path(name).name for name in image_map}
    missing = []
    for reference in references:
        reference_name = Path(reference).name
        if reference_name in available_names and not (Path(output_dir) / reference_name).exists():
            missing.append(reference_name)

    if missing:
        missing_text = ", ".join(sorted(set(missing)))
        raise FileNotFoundError(
            f"PDF output references source image(s) that are not available beside the document: {missing_text}"
        )


def normalize_html_math_source(html_content: str) -> str:
    """Restore lost backslashes from common escaped TeX commands in HTML output."""
    html_content = html_content.replace("\f", r"\f")
    html_content = html_content.replace("\v", r"\v")
    html_content = html_content.replace("\a", r"\a")
    html_content = re.sub(r"\r(?=(?:ight|ightarrow|mathrm|text)(?:\W|$))", r"\\r", html_content)
    html_content = re.sub(r"\t(?=(?:ext|imes|heta|ag|op)(?:\W|$))", r"\\t", html_content)
    return html_content


def format_instruction_profile(profile: Dict[str, Any]) -> str:
    """Render a structured exam profile as a readable AI instruction contract."""
    lines = ["## STRUCTURED EXAM PROFILE"]
    exam = profile.get("exam", {})
    for label, key in (("Exam", "name"), ("Paper", "paper_name"), ("Duration", "duration_minutes"), ("Total Marks", "total_marks")):
        if key in exam:
            suffix = " minutes" if key == "duration_minutes" else ""
            lines.append(f"- {label}: {exam[key]}{suffix}")

    lines.extend(["", "### GLOBAL INSTRUCTIONS"])
    lines.extend(
        f"- {item}"
        for item in profile.get("generation_instructions", profile.get("global_instructions", []))
    )

    policy = profile.get("generation_policy", {})
    if policy:
        lines.extend(["", "### GENERATION POLICY"])
        lines.extend(f"- {key.replace('_', ' ').capitalize()}: {value}" for key, value in policy.items())

    calibration = profile.get("calibration", {})
    if calibration:
        lines.extend(["", "### EXAM CALIBRATION"])
        for key, value in calibration.items():
            if isinstance(value, list):
                lines.append(f"- {key.replace('_', ' ').capitalize()}: {', '.join(map(str, value))}")
            else:
                lines.append(f"- {key.replace('_', ' ').capitalize()}: {value}")

    for section in profile.get("sections", []):
        lines.extend(
            [
                "",
                f"### SECTION: {section.get('name', section.get('id', 'Unnamed'))}",
                f"- Subject: {section.get('subject', 'General')}",
                f"- Questions: {section.get('question_count', section.get('total_questions', 0))}",
                f"- Attempt count: {section.get('attempt_count', 'all')}",
            ]
        )
        if "question_number_start" in section or "question_number_end" in section:
            lines.append(
                f"- Question numbers: {section.get('question_number_start', '?')}-"
                f"{section.get('question_number_end', '?')}"
            )
        for allocation in section.get("question_types", []):
            lines.append(
                f"- Question type: {allocation.get('type', allocation)}"
                + (f"; count: {allocation['count']}" if "count" in allocation else "")
            )
        scoring = section.get("scoring", {})
        if scoring:
            lines.append(f"- Scoring: {json.dumps(scoring, ensure_ascii=False, sort_keys=True)}")
        answer_format = section.get("answer_format")
        if answer_format:
            lines.append(f"- Answer format: {json.dumps(answer_format, ensure_ascii=False, sort_keys=True)}")

    lines.extend(
        [
            "",
            "### PROFILE COMPLIANCE",
            "Treat this profile as authoritative. Do not change section counts, numbering, scoring, instructions, or allowed question types.",
            "Place the paper header and examination instructions before the first question for PDF output.",
        ]
    )
    return "\n".join(lines)


def write_prompt_snapshot(
    snapshot_path: Path,
    system_instruction: str,
    contents: List[Any],
) -> Path:
    """Write the exact textual AI inputs and attachment references to Markdown."""
    sections = [
        "# AI Prompt Snapshot",
        "",
        "## System Instructions",
        system_instruction,
        "",
        "## User Prompt and Attachments",
    ]
    for index, content in enumerate(contents, 1):
        if isinstance(content, str):
            sections.extend([f"### Content {index} (text)", content, ""])
        else:
            sections.extend(
                [
                    f"### Content {index} (attachment)",
                    f"```text\n{type(content).__name__}\n{getattr(content, 'filename', '')}\n```",
                    "",
                ]
            )
    snapshot_path = Path(snapshot_path)
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text("\n".join(sections), encoding="utf-8")
    logger.info("📝 Wrote AI prompt snapshot: %s", snapshot_path)
    return snapshot_path


def assemble_prompt_files(
    rule_files: Dict[str, Any],
    mode: str,
    output_format: str = "xml",
    pdf_engine: str = "html",
    verify_online: bool = False,
) -> str:
    """Combines specified prompt markdown files into a single context string."""
    engine_name = "LaTeX (XeLaTeX)" if pdf_engine == "tex" else "HTML5 (KaTeX)"
    top_contract = (
        f"# SYSTEM INSTRUCTIONS & PIPELINE RULES\n"
        f"TARGET FORMAT: Complete standalone {engine_name} document.\n"
        if output_format == "pdf"
        else "# SYSTEM INSTRUCTIONS & PIPELINE RULES\n"
    )
    content_blocks = [top_contract]

    instruction_profile = rule_files.get("instruction_profile")
    if instruction_profile:
        content_blocks.append(format_instruction_profile(instruction_profile))

    if output_format == "pdf":
        pdf_rule_key = "pdf_rules_tex" if pdf_engine == "tex" else "pdf_rules_html"
        valid_keys = ["main_prompt", "instruction_file", pdf_rule_key, "pdf_rules", "reasoning_rules"]
    else:
        valid_keys = [
            "main_prompt",
            "instruction_file",
            "xml_rules",
            "reasoning_rules",
            "tags_rules",
            "templates",
        ]
        if mode == "extract":
            valid_keys.insert(4, "xml_extraction_rules")

    ordered_names = ["main_prompt", "reasoning_rules"]
    if output_format == "pdf":
        ordered_names.append(pdf_rule_key)
    else:
        ordered_names.append("xml_rules")
        if mode == "extract":
            ordered_names.append("xml_extraction_rules")
        ordered_names.extend(["tags_rules", "templates"])
    ordered_names.extend(name for name in rule_files if name not in ordered_names)

    seen_rule_paths = set()
    for name in ordered_names:
        filepath = rule_files.get(name)
        if name not in valid_keys or not filepath:
            continue
        path = Path(filepath)
        path_key = str(path.resolve())
        if path_key in seen_rule_paths:
            continue
        seen_rule_paths.add(path_key)
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()

                content_blocks.append(f"## File: {path.name}\n\n{content}\n\n---\n")
        else:
            logger.warning(f"Prompt file '{filepath}' not found. Skipping.")

    if mode == "extract" and output_format != "pdf":
        content_blocks.append("""
## CRITICAL EXTRACTION OVERRIDE RULE FOR DIAGRAMS & IMAGES:
1. DO NOT GENERATE INLINE <svg> CODE IN EXTRACTION MODE!
2. All diagrams, circuits, graphs, and figures are provided as attached images with reference IDs (e.g., img-0.jpeg, img-1.jpeg).
3. Whenever a question or option refers to a visual diagram, circuit, graph, or figure, embed it using:
   <img src="@@PLUGINFILE@@/EXACT_IMAGE_ID.jpeg" />
4. DO NOT write <file> or Base64 payload tags! The Python post-processor will read the local image files and inject the <file> Base64 nodes automatically.
---
""")
    elif output_format == "pdf":
        if pdf_engine == "tex":
            content_blocks.append("""
## CRITICAL LATEX OUTPUT OVERRIDE RULE:
1. TARGET FORMAT: Output a complete, standalone, compilable LaTeX document starting with `\\documentclass{article}` and ending with `\\end{document}`.
2. STRICT PROHIBITIONS:
    - NEVER output HTML tags (`<!DOCTYPE html>`, `<html>`, `<p>`, `<div>`, `<hr/>`, `<b>`, `<br/>`).
3. USE NATIVE LATEX:
   - For bold: `\\textbf{...}`
   - For italic: `\\textit{...}`
   - For choices/lists: `\\begin{enumerate}` / `\\item`
   - For spacing: `\\par\\medskip`
   - For math: `$ ... $` or `\\[ ... \\]`
---
""")
        else:
            content_blocks.append("""
## CRITICAL HTML5 OUTPUT OVERRIDE RULE:
1. TARGET FORMAT: Output a complete, standalone, valid HTML5 document starting with `<!DOCTYPE html><html>` and ending with `</html>`.
2. STRICT PROHIBITIONS:
    - NEVER output LaTeX document preambles (`\\documentclass`, `\\begin{document}`).
3. USE CLEAN HTML5:
   - Include KaTeX and Google Web Fonts in `<head>`.
   - Use `$ ... $` and `$$ ... $$` for math.
---
""")
        if mode == "extract":
            content_blocks.append(f"""
## CRITICAL EXTRACTION PDF OVERRIDE RULE:
1. Preserve source visuals when they are required to understand a question.
2. In HTML, reference supplied images by their exact attachment filename using relative `<img src=\"FILENAME\" />` paths.
3. In TeX, reference supplied images by their exact attachment filename using `\\includegraphics{{FILENAME}}`.
4. Do not synthesize a replacement visual when the supplied source image is needed for faithful extraction.
5. Keep all extracted questions in source order and include the complete answer key and reasoning required by `reasoning_rules.md`.
6. Do not invent exam title, subject, duration, marks, class, or other metadata that is not present in the supplied source or runtime parameters.
---
""")

    return "\n".join(content_blocks)


def build_language_instructions(
    languages: List[str], output_format: str = "xml", pdf_engine: str = "html"
) -> Tuple[str, List[str]]:
    """Load format-specific language instructions and return language tags."""
    clean_langs = [l.strip().lower() for l in languages if l.strip()]
    if not clean_langs:
        clean_langs = ["english"]

    iso_codes = [LANG_ISO_MAP.get(l, l[:2]) for l in clean_langs]
    lang_tags = [f"lang:{code}" for code in iso_codes]
    primary_lang = "English"
    secondary_langs = [l.capitalize() for l in clean_langs if l != "english"]
    target_secondary = ", ".join(secondary_langs)
    bilingual = len(secondary_langs) > 0
    if output_format.lower() == "pdf":
        section = "Bilingual TeX" if pdf_engine.lower() == "tex" and bilingual else None
        section = section or ("Bilingual HTML" if bilingual else ("English TeX" if pdf_engine.lower() == "tex" else "English HTML"))
    else:
        section = "Bilingual XML" if bilingual else "English XML"

    preamble_lines = []
    font_notes = []
    for lang in clean_langs:
        if lang == "english":
            continue
        cfg = INDIC_LANGUAGE_CONFIG.get(lang, {
            "polyglossia": lang,
            "script": lang.capitalize(),
            "font": f"Noto Serif {lang.capitalize()}",
            "cmd": f"{lang}font",
        })
        preamble_lines.append(
            f"\\setotherlanguage{{{cfg['polyglossia']}}}\n"
            f"\\newfontfamily\\{cfg['cmd']}[Script={cfg['script']},AutoFakeBold=true,AutoFakeSlant=true]{{{cfg['font']}}}"
        )
        font_notes.append(
            f"   - Wrap translated {lang.capitalize()} text in `{{\\{cfg['cmd']} ...}}`; keep Latin labels and formulas outside that font block."
        )

    return load_prompt_section(
        LANGUAGE_RULES_PATH,
        section,
        primary_lang=primary_lang,
        target_secondary=target_secondary,
        preamble_snippet="\n".join(preamble_lines),
        font_usage_notes="\n".join(font_notes),
    ), lang_tags


def setup_logger(
    log_file: Path,
    verbose: bool = False,
    max_bytes: int = 5 * 1024 * 1024,  # 5 MB per log file
    backup_count: int = 5,              # Keep up to 5 rotated backup files
) -> None:
    """Configures dual logging to both stdout and a rotating file log."""
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    def _configure_logger(name: str) -> None:
        logger_obj = logging.getLogger(name)
        logger_obj.setLevel(logging.DEBUG if verbose else logging.INFO)
        logger_obj.propagate = False

        for handler in list(logger_obj.handlers):
            logger_obj.removeHandler(handler)
            handler.close()

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger_obj.addHandler(console_handler)

        if log_file:
            log_path = Path(log_file)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                filename=log_path,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            logger_obj.addHandler(file_handler)

    _configure_logger("academic_content_pipeline")
    _configure_logger("moodle_system")


def extract_clean_question_nodes_with_status(raw_text: str) -> Tuple[List[str], Optional[str]]:
    """
    Parses XML nodes and returns both the list of valid questions and any XML parse error encountered.
    """
    if not raw_text or not raw_text.strip():
        return [], None

    raw_text = raw_text.replace("```xml", "").replace("```", "").strip()
    pattern = re.compile(r"(<question\b[^>]*>.*?</question>)", re.DOTALL | re.IGNORECASE)
    matches = pattern.findall(raw_text)

    def _normalize_question_type(node_xml: str) -> str:
        question_open = re.search(r"<question\b([^>]*)>", node_xml, flags=re.IGNORECASE)
        if not question_open:
            return node_xml

        attrs_text = question_open.group(1) or ""
        has_type_attr = re.search(r"\btype\s*=", attrs_text, flags=re.IGNORECASE) is not None
        if has_type_attr:
            return node_xml

        type_match = re.search(r"<type>\s*([^<]+?)\s*</type>", node_xml, flags=re.IGNORECASE)
        if not type_match:
            return re.sub(
                r"<question\b([^>]*)>",
                r'<question\1 type="multichoice">',
                node_xml,
                count=1,
                flags=re.IGNORECASE,
            )

        qtype = type_match.group(1).strip().lower()
        alias_map = {
            "multiplechoice": "multichoice",
            "multiple_choice": "multichoice",
            "mcq": "multichoice",
        }
        qtype = alias_map.get(qtype, qtype)
        node_xml = re.sub(
            r"<question\b([^>]*)>",
            lambda m: f'<question{m.group(1)} type="{qtype}">',
            node_xml,
            count=1,
            flags=re.IGNORECASE,
        )

        node_xml = re.sub(r"\s*<type>\s*[^<]+?\s*</type>", "", node_xml, count=1, flags=re.IGNORECASE)
        return node_xml

    valid_nodes = []
    last_error = None
    for node in matches:
        node = _normalize_question_type(node.strip())
        try:
            root = ET.fromstring(node)
            qtype = (root.attrib.get("type") or "").strip().lower()
            if qtype:
                valid_nodes.append(node)
            else:
                # Add default type if missing
                node = re.sub(r"<question\b", '<question type="multichoice"', node, count=1)
                valid_nodes.append(node)
        except ET.ParseError as e:
            last_error = str(e)
            logger.warning(f"XML parse issue in question node ({e}). Checking if recoverable...")
            # If standard tags exist, salvage the node
            if "<questiontext" in node and ("<answer" in node or "<generalfeedback" in node):
                valid_nodes.append(node)

    return valid_nodes, last_error


# =======================================================================
# Moodle XML Validation and Base64 Embedding Injection
# =======================================================================

def fix_and_inject_moodle_xml(raw_xml: str, image_map: Dict[str, str]) -> str:
    """
    Post-processes and validates Moodle XML structure.
    Injects Base64 <file> tags for any referenced @@PLUGINFILE@@ images.
    """
    logger.info("Post-processing and validating Moodle XML structure...")

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
        r"<(questiontext|generalfeedback)([^>]*)>(.*?)</\1>",
        ensure_text_wrapper,
        raw_xml,
        flags=re.DOTALL,
    )

    def inject_file_tag(match):
        full_block = match.group(0)
        found_imgs = re.findall(r"@@PLUGINFILE@@/([a-zA-Z0-9_\-\.]+)", full_block)

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

    pattern = r"<(questiontext|answer|generalfeedback)[^>]*>.*?</\1>"
    processed_xml = re.sub(pattern, inject_file_tag, xml_fixed, flags=re.DOTALL)

    processed_xml = re.sub(
        r"<name>\s*<!\[CDATA\[(.*?)\]\]>\s*</name>",
        r"<name>\n        <text>\1</text>\n    </name>",
        processed_xml,
    )

    clean_xml = processed_xml.strip()
    if "<quiz>" in clean_xml:
        start_pos = clean_xml.find("<quiz>")
        clean_xml = clean_xml[start_pos:]
        if "</quiz>" in clean_xml:
            end_pos = clean_xml.rfind("</quiz>") + len("</quiz>")
            clean_xml = clean_xml[:end_pos]
        else:
            clean_xml = clean_xml + "\n</quiz>"
        clean_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + clean_xml
    else:
        clean_xml = f'<?xml version="1.0" encoding="UTF-8"?>\n<quiz>\n{clean_xml}\n</quiz>'

    return clean_xml


# =======================================================================
# Local Compilers for LaTeX and HTML -> PDF
# =======================================================================

def compile_html_to_pdf(
    html_content: str, output_pdf_path: Path, image_map: Optional[Dict[str, str]] = None
) -> Path:
    """Compiles HTML string to PDF locally via Headless Chrome."""
    html_content = normalize_html_math_source(html_content)
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    html_file = output_pdf_path.with_suffix(".html")
    html_file.write_text(html_content, encoding="utf-8")

    # Copy associated diagram images to output directory so relative image tags resolve
    if image_map:
        for img_name, img_path in image_map.items():
            if os.path.exists(img_path):
                dest = output_pdf_path.parent / img_name
                try:
                    shutil.copy2(img_path, dest)
                except Exception as e:
                    logger.debug(f"Image copy note: {e}")

    validate_local_image_references(html_content, output_pdf_path.parent, image_map, "html")

    logger.info(f"Compiling HTML to PDF via Headless Chrome -> {output_pdf_path}...")
    cmd = [
        "google-chrome",
        "--headless",
        "--disable-gpu",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=5000",
        "--no-pdf-header-footer",
        f"--print-to-pdf={output_pdf_path}",
        str(html_file),
    ]
    try:
        subprocess.run(cmd, check=True, cwd=str(output_pdf_path.parent))
    except FileNotFoundError:
        logger.warning("google-chrome not found in PATH, trying chromium...")
        cmd[0] = "chromium"
        subprocess.run(cmd, check=True, cwd=str(output_pdf_path.parent))

    return output_pdf_path


def compile_tex_to_pdf(
    tex_content: str, output_pdf_path: Path, image_map: Optional[Dict[str, str]] = None
) -> Path:
    """Compiles LaTeX/TeX string to PDF locally via xelatex."""
    output_pdf_path.parent.mkdir(parents=True, exist_ok=True)
    tex_file = output_pdf_path.with_suffix(".tex")
    tex_file.write_text(tex_content, encoding="utf-8")

    # Copy associated diagram images to output directory so \includegraphics resolves
    if image_map:
        for img_name, img_path in image_map.items():
            if os.path.exists(img_path):
                dest = output_pdf_path.parent / img_name
                try:
                    shutil.copy2(img_path, dest)
                except Exception as e:
                    logger.debug(f"Image copy note: {e}")

    validate_local_image_references(tex_content, output_pdf_path.parent, image_map, "tex")
    tex_file.write_text(tex_content, encoding="utf-8")

    logger.info(f"Compiling TeX to PDF via xelatex -> {output_pdf_path}...")
    cmd = [
        "xelatex",
        "-interaction=nonstopmode",
        "-halt-on-error",
        f"-output-directory={output_pdf_path.parent}",
        str(tex_file),
    ]
    subprocess.run(cmd, check=True, cwd=str(output_pdf_path.parent))
    return output_pdf_path


