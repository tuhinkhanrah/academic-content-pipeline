#!/usr/bin/env python3
"""
moodle_utils.py - Shared Utilities for Moodle XML Agents.
"""

import base64
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple
import xml.etree.ElementTree as ET

logger = logging.getLogger("moodle_system")

LANG_ISO_MAP = {
    "english": "en",
    "bengali": "bn",
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
    "urdu": "ur",
}


def load_file_content(file_path: Optional[Path]) -> str:
    """Reads and returns text content from a Path safely."""
    if not file_path or not file_path.exists():
        return ""
    try:
        return file_path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.error(f"Failed to read file {file_path}: {e}")
        return ""


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


def build_language_instructions(languages: List[str]) -> Tuple[str, List[str]]:
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
            return node_xml

        qtype = type_match.group(1).strip().lower()
        alias_map = {
            "multiplechoice": "multichoice",
            "multiple_choice": "multichoice",
            "mcq": "multichoice",
        }
        qtype = alias_map.get(qtype, qtype)
        if not qtype:
            return node_xml

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
    for node in matches:
        node = _normalize_question_type(node.strip())
        try:
            root = ET.fromstring(node)
            qtype = (root.attrib.get("type") or "").strip().lower()
            if not qtype:
                return [], "Missing question type attribute on <question>."
            valid_nodes.append(node)
        except ET.ParseError as e:
            return [], str(e)

    return valid_nodes, None