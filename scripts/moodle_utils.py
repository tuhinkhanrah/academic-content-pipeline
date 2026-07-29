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


def build_language_instructions(languages: List[str]) -> Tuple[str, List[str]]:
    """
    Returns:
      1. Dynamic prompt instructions for Gemini.
      2. List of XML tags (e.g., ['lang:en', 'lang:bn']).
    """
    clean_langs = [l.strip().lower() for l in languages if l.strip()]
    if not clean_langs:
        clean_langs = ["english"]

    # Generate XML tags (e.g., lang:en, lang:ta)
    lang_tags = [f"lang:{LANG_ISO_MAP.get(l, l[:2])}" for l in clean_langs]

    # Case A: Monolingual English
    if len(clean_langs) == 1 and clean_langs[0] == "english":
        instruction = (
            "=== LANGUAGE & FORMAT LAWS ===\n"
            "- Output all questions, choices, and explanations strictly in English.\n"
            "- Output only complete <question ...>...</question> nodes.\n"
            "- Question type must be in the root attribute, e.g. <question type=\"multichoice\">.\n"
        )
        return instruction, lang_tags

    # Case B: Bilingual (English + Regional Language)
    primary_lang = "English"
    secondary_langs = [l.capitalize() for l in clean_langs if l != "english"]
    target_secondary = ", ".join(secondary_langs)

    instruction = (
        f"=== BILINGUAL LANGUAGE & FORMAT LAWS ({primary_lang} + {target_secondary}) ===\n"
        f"Generate/Extract every question, choice, and feedback explanation in a STACKED BILINGUAL format:\n"
        f"1. Provide the complete text in {primary_lang} first.\n"
        f"2. Immediately follow with the complete translation in {target_secondary}.\n"
        f"3. Separate the two language versions in <questiontext> using a clean line break or <hr/> tag.\n"
        f"4. For choices (<answer>), output only the {primary_lang} option text. Do not include translated/regional option text.\n"
        f"5. Output only complete <question ...>...</question> nodes.\n"
        f"6. Question type must be in the root attribute, e.g. <question type=\"multichoice\">.\n"
        f"7. DO NOT translate mathematical symbols, formulas, chemical equations, or LaTeX variables inside \\(...\\) or \\[...\\] delimiters.\n"
        f"8. Use standard, formal K-12 NCERT/Exam-board terminology for {target_secondary}.\n"
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


def load_file_content(file_path: Optional[Path]) -> str:
    if not file_path or not file_path.exists():
        return ""
    try:
        return file_path.read_text(encoding="utf-8").strip()
    except Exception as e:
        logger.error(f"Failed to read file {file_path}: {e}")
        return ""


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
        """
        Moodle XML requires question type as an attribute on <question>, e.g.:
          <question type="multichoice"> ... </question>
        Some model outputs place it as a child node:
          <question> ... <type>multichoice</type> ... </question>
        Normalize that form before XML validation.
        """
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
        # Normalize common aliases produced by models.
        alias_map = {
            "multiplechoice": "multichoice",
            "multiple_choice": "multichoice",
            "mcq": "multichoice",
        }
        qtype = alias_map.get(qtype, qtype)
        if not qtype:
            return node_xml

        # Inject missing type attribute in the opening <question> tag.
        node_xml = re.sub(
            r"<question\b([^>]*)>",
            lambda m: f'<question{m.group(1)} type="{qtype}">',
            node_xml,
            count=1,
            flags=re.IGNORECASE,
        )

        # Remove first <type>...</type> child occurrence after promotion.
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
            if qtype != "multichoice":
                return [], f"Non-MCQ question type generated: '{qtype}'. Only 'multichoice' is allowed."
            valid_nodes.append(node)
        except ET.ParseError as e:
            return [], str(e)

    return valid_nodes, None