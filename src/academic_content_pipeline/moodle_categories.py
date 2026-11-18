"""Build and insert Moodle category markers for generated question files."""

import re
from pathlib import Path
from xml.dom import Node, minidom
from xml.parsers.expat import ExpatError


def build_category_path(
    top_category: str, source_root: Path, source_file: Path, languages: list[str]
) -> str:
    top_category = top_category.strip()
    if not top_category or "/" in top_category or "\\" in top_category:
        raise ValueError("--top-category must be a non-empty single category name.")
    root = Path(source_root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Source root is not a directory: {root}")
    source = Path(source_file).expanduser().resolve()
    try:
        relative = source.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Source file is not under --source-root: {source}") from exc
    tokens = []
    for language in languages:
        token = re.sub(r"[^a-z0-9]+", "_", language.strip().lower()).strip("_")
        if not token:
            raise ValueError(f"Invalid language name: {language!r}")
        tokens.append(token)
    if not tokens:
        raise ValueError("At least one language is required for a Moodle category.")
    parts = (*relative.parent.parts, relative.stem, "_".join(tokens))
    return "/".join(("$module$", "top", top_category, *parts))


def build_exam_practice_paper_category_path(
    top_category: str,
    exam_name: str,
    year: str,
    languages: list[str],
) -> str:
    """Build the stable exam/year/language category for generated full papers."""
    category_parts = {
        "top category": top_category.strip(),
        "exam name": exam_name.strip(),
    }
    for label, value in category_parts.items():
        if not value or "/" in value or "\\" in value:
            raise ValueError(f"{label} must be a non-empty single category name.")
    exam_slug = re.sub(r"[^a-z0-9]+", "-", category_parts["exam name"].lower()).strip("-")
    if not exam_slug:
        raise ValueError("exam name must contain letters or digits.")
    if exam_slug == "neet":
        exam_slug = "neet-ug"
    if not re.fullmatch(r"\d{4}", year):
        raise ValueError("The paper category year must be a four-digit year.")
    language_tokens = []
    for language in languages:
        token = re.sub(r"[^a-z0-9]+", "_", language.strip().lower()).strip("_")
        if not token:
            raise ValueError(f"Invalid language name: {language!r}")
        language_tokens.append(token)
    if not language_tokens:
        raise ValueError("At least one language is required for a Moodle category.")
    return "/".join(
        (
            "$module$",
            "top",
            category_parts["top category"],
            exam_slug,
            year,
            "_".join(language_tokens),
        )
    )


def _parse_quiz(xml: str) -> minidom.Document:
    try:
        document = minidom.parseString(xml)
    except ExpatError as exc:
        raise ValueError("Cannot add a category to malformed Moodle XML.") from exc
    root = document.documentElement
    if root.tagName != "quiz":
        raise ValueError("Moodle XML root must be <quiz>.")
    return document


def _remove_categories(root: minidom.Element) -> None:
    for child in list(root.childNodes):
        if (child.nodeType == Node.ELEMENT_NODE and child.tagName == "question"
                and child.getAttribute("type") == "category"):
            root.removeChild(child)
            child.unlink()


def remove_moodle_categories(xml: str) -> str:
    """Keep category metadata out of the translation request."""
    document = _parse_quiz(xml)
    _remove_categories(document.documentElement)
    return document.toxml(encoding="UTF-8").decode("UTF-8")


def set_moodle_category(xml: str, category_path: str) -> str:
    """Set one category for all questions in a single-source output."""
    document = _parse_quiz(xml)
    root = document.documentElement
    _remove_categories(root)
    question = document.createElement("question")
    question.setAttribute("type", "category")
    category = document.createElement("category")
    text = document.createElement("text")
    text.appendChild(document.createTextNode(category_path))
    category.appendChild(text)
    question.appendChild(category)
    root.insertBefore(question, root.firstChild)
    return document.toxml(encoding="UTF-8").decode("UTF-8")
