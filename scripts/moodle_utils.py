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

    valid_nodes = []
    for node in matches:
        node = node.strip()
        try:
            ET.fromstring(node)
            valid_nodes.append(node)
        except ET.ParseError as e:
            return [], str(e)

    return valid_nodes, None