"""Category and checksum validation for reusable chapter question outputs."""

import hashlib
import xml.etree.ElementTree as ET
from pathlib import Path


def chapter_output_record(
    path: Path, category: str | None, question_count: int | None = None,
) -> dict[str, str]:
    try:
        data = path.read_bytes()
        root = ET.fromstring(data)
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"Cannot reuse chapter XML {path}: {exc}") from exc
    questions = [node for node in root if node.get("type") != "category"]
    markers = [node for node in root if node.get("type") == "category"]
    if (
        root.tag != "quiz" or not questions
        or any(node.tag != "question" or not node.get("type") for node in root)
        or (question_count is not None and len(questions) != question_count)
    ):
        raise ValueError(f"Cannot reuse chapter XML {path}: invalid question structure or count.")
    if category is not None and (
        len(markers) != 1 or root[0] is not markers[0]
        or markers[0].findtext("category/text") != category
    ):
        raise ValueError(
            f"Cannot reuse chapter XML {path}: expected category {category!r}. "
            "Migrate the existing output or use --force to regenerate."
        )
    return {"category_path": category or "", "sha256": hashlib.sha256(data).hexdigest()}


def check_chapter_records(
    directory: Path, manifest: dict, expected: dict[str, tuple[str | None, int]],
) -> dict[str, dict[str, str]]:
    saved = manifest.get("output_integrity")
    if "output_integrity" in manifest:
        completed = manifest.get("completed_files")
        if (
            not isinstance(saved, dict) or not isinstance(completed, list)
            or any(not isinstance(name, str) for name in completed)
            or set(saved) != set(completed) or not saved.keys() <= expected.keys()
        ):
            raise ValueError(f"Invalid chapter checkpoint output integrity metadata: {directory}")
    records = {}
    for name, (category, count) in expected.items():
        path = directory / name
        if not path.exists() and (saved is None or name not in saved):
            continue
        record = chapter_output_record(path, category, count)
        if saved is not None and name in saved and saved[name] != record:
            raise ValueError(f"Chapter checkpoint output integrity mismatch: {path}")
        records[name] = record
    return records
