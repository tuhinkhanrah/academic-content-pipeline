#!/usr/bin/env python3
"""
pipeline_utils.py - Shared utilities for question generation, XML/HTML/LaTeX/PDF output, and agent workflows.
"""

import base64
import hashlib
import io
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

import pymupdf
from google import genai
from google.genai import types

try:
    from PIL import Image, ImageEnhance
except ImportError:
    Image, ImageEnhance = None, None

logger = logging.getLogger("moodle_system")

LANG_ISO_MAP = {
    "english": "en",
    "bengali": "bn",
    "bangla": "bn",
    "hindi": "hi",
    "tamil": "ta",
    "telugu": "te",
    "marathi": "mr",
    "gujarati": "gu",
    "kannada": "kn",
    "malayalam": "ml",
    "punjabi": "pa",
    "assamese": "as",
    "odia": "or",
    "oriya": "or",
    "urdu": "ur",
}

INDIC_LANGUAGE_CONFIG: Dict[str, Dict[str, str]] = {
    "bengali": {"polyglossia": "bengali", "script": "Bengali", "font": "Noto Serif Bengali", "cmd": "bengalifont"},
    "bangla": {"polyglossia": "bengali", "script": "Bengali", "font": "Noto Serif Bengali", "cmd": "bengalifont"},
    "hindi": {"polyglossia": "hindi", "script": "Devanagari", "font": "Noto Serif Devanagari", "cmd": "hindifont"},
    "tamil": {"polyglossia": "tamil", "script": "Tamil", "font": "Noto Serif Tamil", "cmd": "tamilfont"},
    "telugu": {"polyglossia": "telugu", "script": "Telugu", "font": "Noto Serif Telugu", "cmd": "telugufont"},
    "marathi": {"polyglossia": "marathi", "script": "Devanagari", "font": "Noto Serif Devanagari", "cmd": "marathifont"},
    "gujarati": {"polyglossia": "gujarati", "script": "Gujarati", "font": "Noto Serif Gujarati", "cmd": "gujaratifont"},
    "kannada": {"polyglossia": "kannada", "script": "Kannada", "font": "Noto Serif Kannada", "cmd": "kannadafont"},
    "malayalam": {"polyglossia": "malayalam", "script": "Malayalam", "font": "Noto Serif Malayalam", "cmd": "malayalamfont"},
    "punjabi": {"polyglossia": "punjabi", "script": "Gurmukhi", "font": "Noto Serif Gurmukhi", "cmd": "punjabifont"},
    "assamese": {"polyglossia": "assamese", "script": "Bengali", "font": "Noto Serif Bengali", "cmd": "assamesefont"},
    "odia": {"polyglossia": "oriya", "script": "Oriya", "font": "Noto Serif Oriya", "cmd": "odiafont"},
    "oriya": {"polyglossia": "oriya", "script": "Oriya", "font": "Noto Serif Oriya", "cmd": "oriyafont"},
    "urdu": {"polyglossia": "urdu", "script": "Arabic", "font": "Noto Nastaliq Urdu", "cmd": "urdufont"},
}


def _find_matching_brace(text: str, open_index: int) -> int:
    """Returns the index of the closing brace matching the opening brace at open_index."""
    depth = 0
    escaped = False

    for idx in range(open_index, len(text)):
        ch = text[idx]
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return idx
    return len(text) - 1


def validate_tex_syntax(tex_content: str) -> bool:
    r"""Validate raw TeX for structural errors before it is returned or compiled.

    This is intentionally strict: it fails on unmatched braces and malformed
    item-label groups before they reach XeLaTeX with errors like "Too many }'s."
    or "Argument of \@item has an extra }".
    """
    if tex_content is None or not tex_content.strip():
        raise ValueError("LaTeX source is empty.")

    brace_stack: List[str] = []
    escaped = False
    in_comment = False
    i = 0

    while i < len(tex_content):
        ch = tex_content[i]

        if in_comment:
            if ch == "\n":
                in_comment = False
            i += 1
            continue

        if ch == "%":
            in_comment = True
            i += 1
            continue

        if escaped:
            escaped = False
            i += 1
            continue

        if ch == "\\":
            escaped = True
            i += 1
            continue

        if ch == "{":
            brace_stack.append("{")
        elif ch == "}":
            if not brace_stack:
                raise ValueError(
                    f"Unmatched closing brace detected in TeX source near: {tex_content[max(0, i - 32): i + 32]!r}"
                )
            brace_stack.pop()

        i += 1

    if brace_stack:
        raise ValueError(f"Unbalanced opening braces remain in TeX source: {len(brace_stack)} unmatched '{{'.")

    return True


def sanitize_indic_font_blocks(tex_content: str) -> str:
    """Forces ASCII labels and numeric markers outside Indic font blocks.

    Noto Serif Bengali (and similar Indic fonts) do not contain Latin glyphs such as
    A, I, P, Q, R, S, or digits used in option labels. When the model wraps those
    characters inside an Indic font block, XeLaTeX emits the missing-character warnings
    and renders them as empty boxes. This sanitizer isolates the Latin fragments so they
    are rendered with the default Roman font instead of the Indic font.
    """
    if not tex_content:
        return tex_content

    def _sanitize_inner_text(inner: str) -> str:
        result: List[str] = []
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == "\\":
                j = i + 1
                while j < len(inner) and (inner[j].isalpha() or inner[j] == "@"):
                    j += 1
                if j > i + 1:
                    result.append(inner[i:j])
                    i = j
                    continue
                result.append(ch)
                i += 1
                continue

            if ch.isalpha() or ch.isdigit():
                # Preserve native Bengali/Unicode text, but wrap plain Latin fragments
                # that should not be rendered by the Indic font.
                if ch.isascii() and (ch.isalpha() or ch.isdigit()):
                    j = i + 1
                    while j < len(inner) and (inner[j].isascii() and (inner[j].isalpha() or inner[j].isdigit())):
                        j += 1
                    token = inner[i:j]
                    if re.fullmatch(r"[A-Za-z0-9]+", token):
                        result.append(r"\textnormal{" + token + "}")
                        i = j
                        continue
                result.append(ch)
                i += 1
                continue

            result.append(ch)
            i += 1
        return "".join(result)

    def _contains_indic_chars(text: str) -> bool:
        for ch in text:
            code = ord(ch)
            if 0x0900 <= code <= 0x097F or 0x0980 <= code <= 0x09FF:
                return True
        return False

    def _wrap_indic_commands(text: str) -> str:
        result: List[str] = []
        i = 0
        while i < len(text):
            if text.startswith(r"{\bengalifont", i):
                close_idx = _find_matching_brace(text, i)
                block = text[i:close_idx + 1]
                prefix = r"{\bengalifont"
                inner = block[len(prefix):-1]
                result.append(prefix + _sanitize_inner_text(inner) + "}")
                i = close_idx + 1
                continue

            if text.startswith(r"\bengalifont", i):
                j = i + len(r"\bengalifont")
                if j < len(text) and text[j] == "{":
                    close_idx = _find_matching_brace(text, j)
                    block = text[i:close_idx + 1]
                    inner = block[len(r"\bengalifont"):-1]
                    result.append(r"\bengalifont" + _sanitize_inner_text(inner) + "}")
                    i = close_idx + 1
                    continue

            matched = False
            for cmd in (r"\textbf", r"\textit", r"\emph"):
                if text.startswith(cmd, i):
                    open_idx = i + len(cmd)
                    if open_idx < len(text) and text[open_idx] == "{":
                        close_idx = _find_matching_brace(text, open_idx)
                        content = text[open_idx + 1:close_idx]
                        if _contains_indic_chars(content):
                            sanitized = _sanitize_inner_text(content)
                            if r"\bengalifont" in content:
                                result.append(cmd + "{" + sanitized + "}")
                            else:
                                result.append("{\\bengalifont " + cmd + "{" + sanitized + "}}")
                            i = close_idx + 1
                            matched = True
                            break
            if matched:
                continue
            result.append(text[i])
            i += 1
        return "".join(result)

    tex_content = re.sub(r"(\\item\[[^\]]*?\})\}\s+", r"\1] ", tex_content)
    tex_content = _wrap_indic_commands(tex_content)
    return tex_content


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
        f"TARGET FORMAT: Complete standalone {engine_name} document. Strict prohibition: NO Moodle XML (<question>, <quiz>).\n"
        if output_format == "pdf"
        else "# SYSTEM INSTRUCTIONS & PIPELINE RULES\n"
    )
    content_blocks = [top_contract]

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
                if output_format == "pdf":
                    # Sanitize any residual XML output contracts in main_prompt / instructions
                    content = re.sub(r"(?i)#\s*Output Contract\s*\n.*?(?=\n#|\Z)", "", content, flags=re.DOTALL)
                    content = re.sub(r"(?i)Moodle XML specialist", f"{engine_name} assessment typesetter", content)
                    content = re.sub(r"(?i)in valid Moodle XML", f"in valid {engine_name} format", content)
                    content = re.sub(r"(?i)output valid Moodle XML question nodes", f"output complete {engine_name} document", content)
                    content = re.sub(r"(?i)<question\b[^>]*>.*?</question>", "", content, flags=re.DOTALL)

                if name == "xml_rules" and mode == "extract" and not verify_online:
                    content = re.sub(
                        r"\n# Mandatory Online Answer Verification\n.*?(?=\n# Feedback Rules & Reasoning Structure\n)",
                        "\n",
                        content,
                        flags=re.DOTALL,
                    )
                content_blocks.append(f"## File: {path.name}\n\n{content}\n\n---\n")
        else:
            logger.warning(f"Prompt file '{filepath}' not found. Skipping.")

    if mode == "extract":
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
   - NEVER output Moodle XML tags (`<quiz>`, `<question>`, `<questiontext>`, `<generalfeedback>`, etc.).
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
   - NEVER output Moodle XML tags (`<quiz>`, `<question>`, `<questiontext>`, `<generalfeedback>`, etc.).
   - NEVER output LaTeX document preambles (`\\documentclass`, `\\begin{document}`).
3. USE CLEAN HTML5:
   - Include KaTeX and Google Web Fonts in `<head>`.
   - Use `$ ... $` and `$$ ... $$` for math.
---
""")

    return "\n".join(content_blocks)


def load_combined_prompt(
    main_prompt_path: Optional[Path],
    xml_rules_path: Optional[Path] = None,
    tags_rules_path: Optional[Path] = None,
    templates_path: Optional[Path] = None,
) -> str:
    """
    Combines the primary system prompt (exam profile or question generator) with
    the core Moodle XML rules, naming/tag rules, and XML reference templates.
    """
    parts = []

    # 1. Main Role/Exam/Generator Prompt
    main_text = load_file_content(main_prompt_path)
    if main_text:
        parts.append(main_text)

    # 2. Moodle Core XML Rules
    xml_rules_text = load_file_content(xml_rules_path)
    if xml_rules_text:
        parts.append(f"=== MOODLE XML CORE RULES ===\n{xml_rules_text}")

    # 3. Naming and Tags Rules
    tags_rules_text = load_file_content(tags_rules_path)
    if tags_rules_text:
        parts.append(f"=== NAMING AND TAGS RULES ===\n{tags_rules_text}")

    # 4. Reference XML Templates
    templates_text = load_file_content(templates_path)
    if templates_text:
        parts.append(f"=== MOODLE XML REFERENCE TEMPLATES ===\n{templates_text}")

    return "\n\n".join(parts)


def build_language_instructions(
    languages: List[str], output_format: str = "xml", pdf_engine: str = "html"
) -> Tuple[str, List[str]]:
    """
    Returns:
      1. Dynamic prompt instructions for Gemini.
      2. List of XML tags containing strictly separate individual language tags.
    """
    clean_langs = [l.strip().lower() for l in languages if l.strip()]
    if not clean_langs:
        clean_langs = ["english"]

    iso_codes = [LANG_ISO_MAP.get(l, l[:2]) for l in clean_langs]
    lang_tags = [f"lang:{code}" for code in iso_codes]

    # Handle PDF (HTML / LaTeX) document output format
    if output_format.lower() == "pdf":
        is_tex = pdf_engine.lower() == "tex"

        if len(clean_langs) == 1 and clean_langs[0] == "english":
            if is_tex:
                instruction = (
                    "=== LANGUAGE & FORMAT LAWS (LaTeX XeLaTeX) ===\n"
                    "- Output all questions, choices, and statements strictly in English using native LaTeX.\n"
                    "- DO NOT output HTML tags or Moodle XML tags.\n"
                )
            else:
                instruction = (
                    "=== LANGUAGE & FORMAT LAWS (HTML5) ===\n"
                    "- Output all questions, choices, and statements strictly in English using clean HTML5.\n"
                    "- DO NOT output Moodle XML or <question> tags.\n"
                )
            return instruction, lang_tags

        primary_lang = "English"
        secondary_langs = [l.capitalize() for l in clean_langs if l != "english"]
        target_secondary = ", ".join(secondary_langs)

        if is_tex:
            # Generate exact font family setups for the requested secondary languages
            preamble_font_lines = []
            font_usage_notes = []
            for lang in clean_langs:
                if lang == "english":
                    continue
                cfg = INDIC_LANGUAGE_CONFIG.get(lang, {
                    "polyglossia": lang,
                    "script": lang.capitalize(),
                    "font": f"Noto Serif {lang.capitalize()}",
                    "cmd": f"{lang}font",
                })
                preamble_font_lines.append(
                    f"\\setotherlanguage{{{cfg['polyglossia']}}}\n"
                    f"\\newfontfamily\\{cfg['cmd']}[Script={cfg['script']}]{{{cfg['font']}}}"
                )
                font_usage_notes.append(
                    f"   - Wrap the translated {lang.capitalize()} text in `{{\\{cfg['cmd']} ...}}`.\n"
                    f"   - FONT GLYPH SAFETY: `{cfg['font']}` only contains {cfg['script']} glyphs. NEVER wrap Latin letters (e.g. `(A)`, `(B)`, `(C)`, `(D)`, matching labels `P`, `Q`, `R`, `S`) inside `{{\\{cfg['cmd']}}}`. Keep options outside the font block or in math mode `$P-2, Q-4$`."
                )

            preamble_snippet = "\n".join(preamble_font_lines)
            notes_snippet = "\n".join(font_usage_notes)

            instruction = (
                f"=== BILINGUAL LANGUAGE & FORMAT LAWS (LaTeX / XeLaTeX: {primary_lang} + {target_secondary}) ===\n"
                f"1. PREAMBLE CONFIGURATION FOR TARGET LANGUAGE:\n"
                f"```latex\n"
                f"{preamble_snippet}\n"
                f"```\n"
                f"2. QUESTION LAYOUT:\n"
                f"   - For every bilingual question, render the complete English question block first, followed by `\\par\\medskip`, followed by the translated {target_secondary} block.\n"
                f"   - Each language version must be a self-contained question unit with its own full set of choices (A, B, C, D).\n"
                f"{notes_snippet}\n"
                f"3. NATIVE LATEX:\n"
                f"   - Use `\\textbf{{...}}`, `\\begin{{enumerate}} \\item ... \\end{{enumerate}}`, `\\par\\medskip`.\n"
                f"   - DO NOT use HTML tags (`<p>`, `<div>`, `<hr/>`, `<br/>`) and DO NOT output Moodle XML tags (`<question>`, `<questiontext>`).\n"
            )
        else:
            instruction = (
                f"=== BILINGUAL LANGUAGE & FORMAT LAWS (HTML5: {primary_lang} + {target_secondary}) ===\n"
                f"1. For every bilingual question, provide the complete English question block first, followed by `<hr/>`, followed by the complete {target_secondary} translated block.\n"
                f"2. Each language version must be a self-contained question unit with its own full set of choices (A, B, C, D).\n"
                f"3. DO NOT merge English and translated option values into one list.\n"
                f"4. DO NOT output Moodle XML tags (<question>, <questiontext>, <generalfeedback>, <answer>, etc.).\n"
            )
        return instruction, lang_tags

    # Default: Moodle XML format
    # Case A: Monolingual English
    if len(clean_langs) == 1 and clean_langs[0] == "english":
        instruction = (
            "=== LANGUAGE & FORMAT LAWS ===\n"
            "- Output all questions, choices, and explanations strictly in English.\n"
            "- Output only complete <question ...>...</question> nodes.\n"
            "- Question type must be in the root attribute, e.g. <question type=\"multichoice\">.\n"
        )
        return instruction, lang_tags

    # Case B: Bilingual / Multilingual (e.g., English + Bengali)
    primary_lang = "English"
    secondary_langs = [l.capitalize() for l in clean_langs if l != "english"]
    target_secondary = ", ".join(secondary_langs)

    instruction = (
        f"=== BILINGUAL LANGUAGE & FORMAT LAWS ({primary_lang} + {target_secondary}) ===\n"
        f"Generate/Extract every question, choice, and feedback explanation in a STACKED BILINGUAL format:\n"
        f"1. In <questiontext> and <generalfeedback>, provide the complete text in {primary_lang} first, then immediately follow with the complete translation in {target_secondary}.\n"
        f"2. CRITICAL GENERALFEEDBACK RULE: <generalfeedback> MUST be 100% bilingual. Every numbered step (Step 1, Step 2, ...) and calculation step MUST appear in {primary_lang} and then be fully translated into {target_secondary}. NEVER leave <generalfeedback> in {primary_lang} only.\n"
        f"3. Separate the two language versions in both <questiontext> and <generalfeedback> using a clean line break or <hr/> tag.\n"
        f"4. For choices (<answer>), output only the {primary_lang} option text.\n"
        f"5. Output only complete <question ...>...</question> nodes.\n"
        f"6. DO NOT translate mathematical symbols, formulas, chemical equations, or LaTeX variables inside \\(...\\) or \\[...\\] delimiters.\n"
        f"7. STRICT TAG LAW: Emit ONLY individual language tags (e.g., 'lang:en' and 'lang:{iso_codes[-1]}'). NEVER emit combined tags like 'lang:en_bn'.\n"
    )

    return instruction, lang_tags


def setup_logger(
    log_file: Path,
    verbose: bool = False,
    max_bytes: int = 5 * 1024 * 1024,  # 5 MB per log file
    backup_count: int = 5,              # Keep up to 5 rotated backup files
) -> None:
    """Configures dual logging to both stdout and a rotating file log."""
    logger_obj = logging.getLogger("moodle_system")
    logger_obj.setLevel(logging.DEBUG if verbose else logging.INFO)

    if logger_obj.hasHandlers():
        logger_obj.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 1. Console Stream Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger_obj.addHandler(console_handler)

    # 2. Standard Rotating File Handler
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            filename=log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger_obj.addHandler(file_handler)


def encode_bytes_to_base64(raw_bytes: bytes) -> str:
    return base64.b64encode(raw_bytes).decode("utf-8")


def render_page_to_image_bytes(
    page: pymupdf.Page,
    dpi: int = 200,
    zoom: float = 1.0,
    enhance: bool = False
) -> bytes:
    """
    Renders a PyMuPDF page to PNG bytes.
    Applies an optional zoom matrix and PIL enhancements (contrast/sharpness).
    """
    if zoom != 1.0:
        mat = pymupdf.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, dpi=dpi)
    else:
        pix = page.get_pixmap(dpi=dpi)

    img_bytes = pix.tobytes("png")

    if enhance and Image is not None:
        try:
            # Load into Pillow
            img = Image.open(io.BytesIO(img_bytes))

            # Boost Contrast by 50%
            contrast_enhancer = ImageEnhance.Contrast(img)
            img = contrast_enhancer.enhance(1.5)

            # Double the Sharpness
            sharpness_enhancer = ImageEnhance.Sharpness(img)
            img = sharpness_enhancer.enhance(2.0)

            # Save back to bytes
            out_io = io.BytesIO()
            img.save(out_io, format="PNG")
            img_bytes = out_io.getvalue()
        except Exception as e:
            logger.error(f"Image enhancement failed, falling back to original: {e}")

    elif enhance and Image is None:
        logger.warning("Pillow (PIL) is not installed. Skipping image enhancement. Run `pip install Pillow`.")

    return img_bytes


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


# --- PROMPT CACHE MANAGER UTILITIES ---

REGISTRY_FILE = Path(".cache_registry.json")

def _load_registry() -> dict:
    """Loads the local cache registry file if present."""
    if REGISTRY_FILE.exists():
        try:
            with open(REGISTRY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_registry(data: dict) -> None:
    """Saves updated cache mappings to the local JSON registry."""
    try:
        with open(REGISTRY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save cache registry: {e}")

def get_or_create_prompt_cache(
    client: genai.Client,
    prompt_path: Path,
    prompt_text: str,
    model_name: str,
    ttl_seconds: int = 86400,
    force_refresh: bool = False,
    enable_code_execution: bool = False,
) -> Optional[str]:
    """
    Returns a valid Gemini CachedContent resource name for the given system prompt.
    Automatically invalidates and recreates the cache if the local file hash or tools change.
    """
    prompt_path = Path(prompt_path)

    # Hash includes code_execution state so changing flags automatically refreshes cache
    hash_payload = f"{prompt_text}__code_exec={enable_code_execution}"
    current_hash = hashlib.sha256(hash_payload.encode("utf-8")).hexdigest()

    registry = _load_registry()
    str_path = str(prompt_path.resolve())
    cached_info = registry.get(str_path, {})

    old_cache_name = cached_info.get("cache_name")
    old_hash = cached_info.get("hash")

    needs_recreation = force_refresh or (current_hash != old_hash) or not old_cache_name

    if not needs_recreation and old_cache_name:
        try:
            client.caches.get(name=old_cache_name)
            logger.info(f"⚡ [PROMPT CACHE HIT] Reusing active prompt cache: {old_cache_name}")
            return old_cache_name
        except Exception:
            logger.warning("⚠️ Remote prompt cache expired or deleted. Recreating...")
            needs_recreation = True

    if old_cache_name:
        try:
            logger.info(f"🗑️ Deleting outdated prompt cache: {old_cache_name}")
            client.caches.delete(name=old_cache_name)
        except Exception as e:
            logger.debug(f"Cache cleanup info: {e}")

    safe_display_name = f"prompt-{re.sub(r'[^a-zA-Z0-9_-]', '-', prompt_path.stem)}"

    logger.info(f"📦 [PROMPT CACHE CREATING] Uploading system prompt '{prompt_path.name}' to Gemini Context Cache...")

    tools = None
    if enable_code_execution:
        logger.info("🛠️ Enabling Python Code Execution inside Cached Content...")
        tools = [types.Tool(code_execution=types.ToolCodeExecution())]

    cache_config = types.CreateCachedContentConfig(
        system_instruction=prompt_text,
        display_name=safe_display_name,
        ttl=f"{ttl_seconds}s",
        tools=tools,
    )

    try:
        new_cache = client.caches.create(
            model=model_name,
            config=cache_config
        )
        registry[str_path] = {
            "hash": current_hash,
            "cache_name": new_cache.name,
            "model": model_name,
            "code_execution": enable_code_execution,
        }
        _save_registry(registry)
        logger.info(f"✅ System prompt cached successfully! Resource Name: {new_cache.name}")
        return new_cache.name
    except Exception as e:
        logger.error(f"Failed to create prompt cache on Google servers: {e}")
        return None


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

    tex_content = sanitize_indic_font_blocks(tex_content)
    try:
        validate_tex_syntax(tex_content)
    except ValueError as exc:
        logger.error("TeX validation failed before compilation: %s", exc)
        raise
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


def load_and_merge_config(cli_args_dict: Dict[str, Any], config_file_path: Optional[Path]) -> Dict[str, Any]:
    """
    Merges configuration with precedence:
      Explicit CLI Argument (not None) --> JSON Config File --> Defaults
    """
    merged = {}
    if config_file_path and Path(config_file_path).exists():
        try:
            with open(config_file_path, "r", encoding="utf-8") as f:
                merged = json.load(f)
        except Exception as e:
            logger.warning(f"Failed to read config file {config_file_path}: {e}")

    for k, v in cli_args_dict.items():
        if v is not None:
            merged[k] = v

    return merged
