"""Checkpointed generation of multiple spec-defined mock-paper sets."""

from __future__ import annotations

import copy
import hashlib
import html
import json
import logging
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from xml.dom import Node, minidom
from xml.parsers.expat import ExpatError
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

from .chapter_generation import (
    allocate_matrix,
    group_chapters,
    parse_syllabus_chapters,
)
from .reference_paper import (
    REFERENCE_BATCH_SIZE, prepare_reference, prepare_reference_storage, reference_fingerprint,
)

logger = logging.getLogger("academic_content_pipeline")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
ABSOLUTE_PATH_IN_TEXT = re.compile(r"(?<![\w:])(?:[A-Za-z]:[\\/]|/)[^\s\"'<>]+")


def _absolute_path_parts(value: str) -> Optional[Tuple[str, ...]]:
    normalized = value.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    if posix_path.is_absolute():
        return posix_path.parts
    windows_path = PureWindowsPath(value)
    if windows_path.is_absolute():
        return tuple(part.replace("\\", "/") for part in windows_path.parts)
    return None


def _repository_relative_path_from_text(value: str) -> Optional[str]:
    """Map paths from this or an older checkout root to the current repo-relative form."""
    parts = _absolute_path_parts(value)
    if parts is None:
        return None

    root_name = REPOSITORY_ROOT.name.casefold()
    for index in range(len(parts) - 1, -1, -1):
        if parts[index].casefold() != root_name:
            continue
        relative_parts = parts[index + 1 :]
        if not relative_parts:
            return "."
        candidate = REPOSITORY_ROOT.joinpath(*relative_parts)
        if candidate.exists():
            return PurePosixPath(*relative_parts).as_posix()

    for index in range(len(parts) - 1):
        if (
            parts[index].casefold() == "src"
            and parts[index + 1].casefold() == "academic_content_pipeline"
        ):
            relative_parts = parts[index:]
            candidate = REPOSITORY_ROOT.joinpath(*relative_parts)
            if candidate.exists():
                return PurePosixPath(*relative_parts).as_posix()
    return None


def _portable_path_reference(
    value: str | Path,
    *,
    content_sha256: Optional[str] = None,
) -> str:
    """Represent an absolute path without embedding a machine-specific checkout root."""
    path_text = str(value)
    absolute_parts = _absolute_path_parts(path_text)
    if absolute_parts is None:
        return path_text.replace("\\", "/")

    try:
        resolved_path = Path(path_text).expanduser().resolve()
    except (OSError, ValueError):
        resolved_path = None
    if resolved_path is not None:
        try:
            return resolved_path.relative_to(REPOSITORY_ROOT).as_posix() or "."
        except ValueError:
            pass

    legacy_relative = _repository_relative_path_from_text(path_text)
    if legacy_relative is not None:
        return legacy_relative

    basename = PurePosixPath(*absolute_parts).name or "unknown"
    if content_sha256 is None:
        return f"external/{basename}"
    return f"external/{content_sha256}/{basename}"


def _portable_configuration_value(value: Any) -> Any:
    """Normalize absolute paths recursively before checkpoint configuration is saved."""
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if key == "source_fingerprints" and isinstance(item, dict):
                normalized[key] = {
                    _portable_path_reference(str(path), content_sha256=str(digest)): digest
                    for path, digest in item.items()
                }
            else:
                normalized[key] = _portable_configuration_value(item)
        return normalized
    if isinstance(value, list):
        return [_portable_configuration_value(item) for item in value]
    if isinstance(value, tuple):
        return [_portable_configuration_value(item) for item in value]
    if isinstance(value, (str, Path)) and _absolute_path_parts(str(value)) is not None:
        return _portable_path_reference(value)
    return value


def _portable_manifest_value(value: Any) -> Any:
    """Keep absolute local paths out of persisted checkpoint values and diagnostics."""
    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if key == "source_fingerprints" and isinstance(item, dict):
                normalized[key] = {
                    _portable_path_reference(str(path), content_sha256=str(digest)): digest
                    for path, digest in item.items()
                }
            else:
                normalized_key = (
                    _portable_path_reference(key)
                    if isinstance(key, str)
                    and _absolute_path_parts(key) is not None
                    else key
                )
                normalized[normalized_key] = _portable_manifest_value(item)
        return normalized
    if isinstance(value, list):
        return [_portable_manifest_value(item) for item in value]
    if isinstance(value, tuple):
        return [_portable_manifest_value(item) for item in value]
    if isinstance(value, str):
        normalized = _portable_configuration_value(value)
        if normalized != value:
            return normalized

        def replace_path(match: re.Match[str]) -> str:
            path_text = match.group(0)
            suffix = ""
            while path_text and path_text[-1] in ".,;:)]}":
                suffix = path_text[-1] + suffix
                path_text = path_text[:-1]
            return _portable_path_reference(path_text) + suffix

        return ABSOLUTE_PATH_IN_TEXT.sub(replace_path, value)
    if isinstance(value, Path):
        return _portable_path_reference(value)
    return value


def _resolve_input_path(value: str | Path, relative_to: Path) -> Path:
    """Resolve current or stale checkout-absolute spec paths portably."""
    candidate = Path(value).expanduser()
    if _absolute_path_parts(str(value)) is not None:
        if candidate.is_file():
            return candidate.resolve()
        relative = _repository_relative_path_from_text(str(value))
        if relative is not None:
            rebased = REPOSITORY_ROOT / Path(relative)
            if rebased.is_file():
                return rebased.resolve()
        return candidate.resolve()
    if candidate.is_file():
        return candidate.resolve()
    relative_candidate = relative_to / candidate
    if relative_candidate.is_file():
        return relative_candidate.resolve()
    repository_candidate = REPOSITORY_ROOT / candidate
    if repository_candidate.is_file():
        return repository_candidate.resolve()
    return relative_candidate.resolve()


def _read_spec(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.casefold() in {".yaml", ".yml"}:
        payload = yaml.safe_load(raw)
    elif path.suffix.casefold() == ".json":
        payload = json.loads(raw)
    else:
        raise ValueError(f"Batch and paper specs must be YAML or JSON: {path}")
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a mapping in spec: {path}")
    return payload


def _write_json_atomically(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _write_text_atomically(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(content, encoding="utf-8")
    temporary_path.replace(path)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _configuration_differences(
    saved: Any,
    current: Any,
    path: str = "configuration",
) -> List[str]:
    """Describe leaf-level differences between saved and requested settings."""
    missing = object()

    def format_value(value: Any) -> str:
        if value is missing:
            return "<missing>"
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if len(rendered) > 180:
            rendered = rendered[:177] + "..."
        return rendered

    if isinstance(saved, dict) and isinstance(current, dict):
        differences = []
        for key in sorted(set(saved) | set(current), key=str):
            child_path = f"{path}.{key}"
            if key not in saved or key not in current:
                differences.append(
                    f"{child_path}: saved={format_value(saved.get(key, missing))}, "
                    f"requested={format_value(current.get(key, missing))}"
                )
            else:
                differences.extend(
                    _configuration_differences(saved[key], current[key], child_path)
                )
        return differences

    if isinstance(saved, list) and isinstance(current, list):
        differences = []
        for index in range(max(len(saved), len(current))):
            child_path = f"{path}[{index}]"
            if index >= len(saved) or index >= len(current):
                differences.append(
                    f"{child_path}: saved="
                    f"{format_value(saved[index] if index < len(saved) else missing)}, "
                    f"requested="
                    f"{format_value(current[index] if index < len(current) else missing)}"
                )
            else:
                differences.extend(
                    _configuration_differences(saved[index], current[index], child_path)
                )
        return differences

    if saved != current:
        return [
            f"{path}: saved={format_value(saved)}, requested={format_value(current)}"
        ]
    return []


def _safe_token(value: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip()).strip("_")
    if not token:
        raise ValueError(f"Cannot create a safe identifier from {value!r}.")
    return token.lower()


def _question_count_for_log(xml_path: Path) -> Optional[int]:
    """Count question nodes for diagnostics, even when other validation fails."""
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError):
        return None
    return sum(
        node.tag == "question" and node.get("type") != "category"
        for node in root
    )


def _question_records(
    xml_path: Path,
    expected_count: int,
    chapter_counts: Optional[Dict[str, int]] = None,
) -> List[Dict[str, str]]:
    try:
        root = ET.parse(xml_path).getroot()
    except (OSError, ET.ParseError) as exc:
        raise ValueError(f"Generated output is not well-formed XML: {xml_path}") from exc
    if root.tag != "quiz":
        raise ValueError(f"Generated XML root must be <quiz>: {xml_path}")

    questions = [
        node
        for node in root
        if node.tag == "question" and node.get("type") != "category"
    ]
    if len(questions) != expected_count:
        raise ValueError(
            f"Expected exactly {expected_count} questions in {xml_path}, "
            f"but found {len(questions)}."
        )

    records = []
    actual_chapter_counts = {chapter_id: 0 for chapter_id in (chapter_counts or {})}
    for index, question in enumerate(questions, start=1):
        question_text = question.find("questiontext")
        if question_text is None:
            raise ValueError(f"Question {index} in {xml_path} has no <questiontext>.")
        name_node = question.find("name")
        name = " ".join(" ".join(name_node.itertext()).split()) if name_node is not None else ""
        chapter_id = None
        if chapter_counts is not None:
            chapter_id = next(
                (
                    candidate
                    for candidate in chapter_counts
                    if name.startswith(f"[{candidate}]")
                ),
                None,
            )
            if chapter_id is None:
                raise ValueError(
                    f"Question {index} in {xml_path} must start its Moodle question "
                    f"name with one of the chapter IDs: {', '.join(chapter_counts)}."
                )
            actual_chapter_counts[chapter_id] += 1

        parts = [" ".join(question_text.itertext())]
        parts.extend(
            " ".join(answer.itertext())
            for answer in question.findall("answer")
        )
        canonical_parts = []
        for part in parts:
            decoded = html.unescape(html.unescape(part))
            decoded = re.sub(r"<[^>]*>", " ", decoded)
            decoded = unicodedata.normalize("NFKC", decoded).casefold()
            decoded = re.sub(r"\s+", " ", decoded).strip()
            canonical_parts.append(decoded)
        canonical = " ".join(part for part in canonical_parts if part)
        canonical = re.sub(r"^(?:question\s*)?\d{1,4}\s*[\.\):\-]\s*", "", canonical)
        if not canonical:
            raise ValueError(f"Question {index} in {xml_path} has empty question content.")
        record = {
            "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "name": name or f"Question {index}",
            "summary": canonical[:240],
        }
        if chapter_id is not None:
            record["chapter_id"] = chapter_id
        records.append(record)
    if chapter_counts is not None and actual_chapter_counts != chapter_counts:
        raise ValueError(
            f"Chapter question counts in {xml_path} do not match the requested "
            f"allocation. Expected {chapter_counts}, found {actual_chapter_counts}."
        )
    return records


def _question_count(spec_data: Dict[str, Any], spec_path: Path) -> int:
    subjects = spec_data.get("subjects") or []
    count = sum(int(subject.get("total_questions", 0)) for subject in subjects)
    if count <= 0:
        count = int(spec_data.get("total_questions", 0))
    if count <= 0:
        raise ValueError(
            f"Paper spec must declare positive question counts in subjects or "
            f"total_questions: {spec_path}"
        )
    return count


def _subject_sections(
    sections: List[Dict[str, Any]],
    subject_name: str,
) -> List[Dict[str, Any]]:
    selected = [
        copy.deepcopy(section)
        for section in sections
        if str(section.get("subject", "all")).casefold()
        in {"all", subject_name.casefold()}
    ]
    for section in selected:
        if str(section.get("subject", "all")).casefold() == "all":
            section["subject"] = subject_name
    return selected


def _subject_generation_specs(
    spec_data: Dict[str, Any],
    spec_path: Path,
) -> List[Dict[str, Any]]:
    subjects = spec_data.get("subjects") or []
    if not subjects:
        question_count = int(spec_data.get("total_questions", 0))
        if question_count <= 0:
            return []
        name = str(spec_data.get("paper_title", spec_data.get("exam_name", "Paper")))
        return [
            {
                "name": name,
                "token": _safe_token(name),
                "question_count": question_count,
                "spec_data": copy.deepcopy(spec_data),
            }
        ]

    subject_names = [str(subject.get("name", "")).strip() for subject in subjects]
    if any(not name for name in subject_names):
        raise ValueError(f"Every subject in the paper spec must have a name: {spec_path}")

    result = []
    seen_tokens: Set[str] = set()
    profile = spec_data.get("instruction_profile") or {}
    profile_sections = profile.get("sections") or []
    paper_sections = spec_data.get("sections") or []
    instructions = profile.get("global_instructions") or []
    paper_instructions = spec_data.get("paper_instructions") or []

    for subject in subjects:
        subject_name = str(subject["name"]).strip()
        question_count = int(subject.get("total_questions", 0))
        if question_count <= 0:
            raise ValueError(
                f"Subject '{subject_name}' must declare a positive total_questions "
                f"in {spec_path}"
            )
        subject_token = _safe_token(subject_name)
        if subject_token in seen_tokens:
            raise ValueError(
                f"Subject names must produce unique identifiers in {spec_path}: "
                f"{subject_name!r}"
            )
        seen_tokens.add(subject_token)

        subject_spec = copy.deepcopy(spec_data)
        subject_spec["subjects"] = [copy.deepcopy(subject)]
        subject_spec["subjects"][0]["total_questions"] = question_count
        subject_spec["total_questions"] = question_count
        subject_spec["paper_title"] = (
            f"{spec_data.get('paper_title', spec_data.get('exam_name', 'Paper'))} "
            f"- {subject_name}"
        )

        selected_sections = _subject_sections(paper_sections, subject_name)
        selected_profile_sections = _subject_sections(profile_sections, subject_name)
        if selected_sections:
            subject_spec["sections"] = selected_sections
        elif "sections" in subject_spec:
            subject_spec.pop("sections")
        if isinstance(profile, dict):
            subject_profile = copy.deepcopy(profile)
            if selected_profile_sections:
                subject_profile["sections"] = selected_profile_sections
            elif "sections" in subject_profile:
                subject_profile.pop("sections")
            subject_profile["global_instructions"] = [
                str(instruction)
                for instruction in instructions
                if not any(
                    other_name.casefold() in str(instruction).casefold()
                    for other_name in subject_names
                    if other_name.casefold() != subject_name.casefold()
                )
                and not re.search(
                    r"\b(?:exactly|total of)\b.*\bquestions?\b|\bfor each subject\b",
                    str(instruction),
                    flags=re.IGNORECASE,
                )
            ]
            subject_profile["global_instructions"].append(
                f"Generate exactly {question_count} questions for {subject_name}."
            )
            exam_profile = subject_profile.get("exam")
            if isinstance(exam_profile, dict):
                exam_profile["paper_name"] = subject_spec["paper_title"]
            subject_spec["instruction_profile"] = subject_profile

        subject_spec["paper_instructions"] = [
            str(instruction)
            for instruction in paper_instructions
            if not any(
                other_name.casefold() in str(instruction).casefold()
                for other_name in subject_names
                if other_name.casefold() != subject_name.casefold()
            )
        ]

        type_sections = selected_sections or selected_profile_sections
        if selected_sections and any(
            "question_count" in section or "total_questions" in section
            for section in selected_sections
        ):
            section_question_count = sum(
                int(section.get("question_count", section.get("total_questions", 0)))
                for section in selected_sections
            )
            if section_question_count != question_count:
                raise ValueError(
                    f"Sections for subject '{subject_name}' total "
                    f"{section_question_count} questions in {spec_path}, expected "
                    f"{question_count}."
                )
        type_counts: Dict[str, int] = {}
        for section in type_sections:
            for allocation in section.get("question_types", []):
                if not isinstance(allocation, dict):
                    raise ValueError(
                        f"Question-type allocations must be mappings in {spec_path}"
                    )
                type_name = str(allocation.get("type", "")).strip()
                if not type_name:
                    raise ValueError(f"Question type is missing its type in {spec_path}")
                type_counts[type_name] = (
                    type_counts.get(type_name, 0) + int(allocation.get("count", 0))
                )

        if type_counts and sum(type_counts.values()) != question_count:
            original_config = spec_data.get("question_types") or {}
            original_types = original_config.get("types", [])
            if len(subjects) == 1 and sum(
                int(allocation.get("count", 0))
                for allocation in original_types
                if isinstance(allocation, dict)
            ) == question_count:
                type_counts = {
                    str(allocation["type"]): int(allocation["count"])
                    for allocation in original_types
                    if isinstance(allocation, dict)
                }
            else:
                raise ValueError(
                    f"Cannot determine exact question-type counts for subject "
                    f"'{subject_name}' in {spec_path}: section allocations total "
                    f"{sum(type_counts.values())}, expected {question_count}."
                )
        elif not type_counts and spec_data.get("question_types"):
            original_config = spec_data["question_types"]
            original_types = original_config.get("types", [])
            if len(subjects) == 1 and sum(
                int(allocation.get("count", 0))
                for allocation in original_types
                if isinstance(allocation, dict)
            ) == question_count:
                type_counts = {
                    str(allocation["type"]): int(allocation["count"])
                    for allocation in original_types
                    if isinstance(allocation, dict)
                }
            else:
                subject_spec.pop("question_types", None)

        if type_counts:
            subject_spec["question_types"] = {
                "mode": "exact",
                "types": [
                    {"type": type_name, "count": count}
                    for type_name, count in type_counts.items()
                ],
            }

        if selected_sections:
            subject_total_marks = sum(
                int(section.get("question_count", section.get("total_questions", 0)))
                * float(
                    section.get("scoring", {}).get(
                        "correct",
                        section.get("marks_per_question", 0),
                    )
                )
                for section in selected_sections
            )
            subject_spec["total_marks"] = subject_total_marks
            exam_profile = subject_spec.get("instruction_profile", {}).get("exam")
            if isinstance(exam_profile, dict):
                exam_profile["total_marks"] = subject_total_marks

        result.append(
            {
                "name": subject_name,
                "token": subject_token,
                "question_count": question_count,
                "spec_data": subject_spec,
            }
        )
    return result


def _combine_subject_xml(
    subject_paths: List[Path],
    output_path: Path,
) -> Path:
    combined_document = minidom.Document()
    combined_root = combined_document.createElement("quiz")
    combined_document.appendChild(combined_root)
    for subject_path in subject_paths:
        try:
            subject_document = minidom.parse(str(subject_path))
        except (OSError, ExpatError) as exc:
            raise ValueError(
                f"Generated subject output is not well-formed XML: {subject_path}"
            ) from exc
        subject_root = subject_document.documentElement
        if subject_root.tagName != "quiz":
            raise ValueError(
                f"Generated subject XML root must be <quiz>: {subject_path}"
            )
        for child in subject_root.childNodes:
            if child.nodeType == Node.ELEMENT_NODE:
                combined_root.appendChild(combined_document.importNode(child, True))
    xml_text = combined_document.toxml(encoding="UTF-8").decode("UTF-8")
    _write_text_atomically(
        output_path,
        xml_text,
    )
    return output_path


def _has_saved_generation_response(
    attempt_record: Dict[str, Any],
    state_dir: Path,
    generation_units: List[Dict[str, Any]],
) -> bool:
    response_files = attempt_record.get("response_files", {})
    return any(
        (state_dir / response_files.get(unit["token"], "")).is_file()
        and (state_dir / response_files.get(unit["token"], "")).stat().st_size > 0
        for unit in generation_units
    )


def _decode_chapter_plan(raw_response: str, chapters: List[Dict[str, str]], total: int) -> Dict[str, int]:
    cleaned = raw_response.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match is None:
        raise ValueError("AI chapter allocation response must contain a JSON object.")
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise ValueError("AI chapter allocation response is not valid JSON.") from exc
    entries = payload.get("chapters") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError("AI chapter allocation JSON must contain a 'chapters' list.")

    expected_ids = {chapter["id"] for chapter in chapters}
    counts: Dict[str, int] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each chapter allocation must be a JSON object.")
        chapter_id = entry.get("id")
        count = entry.get("questions")
        if chapter_id not in expected_ids:
            raise ValueError(f"Unknown chapter ID in AI allocation: {chapter_id!r}.")
        if chapter_id in counts:
            raise ValueError(f"Duplicate chapter ID in AI allocation: {chapter_id!r}.")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(
                f"Question count for chapter {chapter_id!r} must be a non-negative integer."
            )
        counts[chapter_id] = count
    missing = expected_ids - counts.keys()
    if missing:
        raise ValueError(
            "AI chapter allocation omitted chapter IDs: " + ", ".join(sorted(missing))
        )
    if sum(counts.values()) != total:
        raise ValueError(
            f"AI chapter allocation totals {sum(counts.values())}; expected {total}."
        )
    return counts


def _chapter_plan_prompt(
    subject_name: str,
    total_questions: int,
    chapters: List[Dict[str, str]],
) -> str:
    chapter_text = "\n\n".join(
        f"### {chapter['id']} — {chapter['title']}\n{chapter['content']}"
        for chapter in chapters
    )
    return (
        "Create a proposed chapter-level question allocation for a competitive-exam "
        "mock paper. Use the complete syllabus below to judge each chapter's breadth "
        "and question potential. This is a reasoned allocation, not an official "
        "historical weighting. Allocate integer question counts; a chapter may receive "
        "zero only when its scope does not support a suitable question. Every listed "
        "chapter ID must appear exactly once, and counts must sum exactly to "
        f"{total_questions}. Return only JSON in this shape: "
        '{"chapters":[{"id":"chapter-id","questions":2}]}.\n\n'
        f"Subject: {subject_name}\nTotal questions: {total_questions}\n\n"
        f"Complete syllabus chapters:\n{chapter_text}"
    )


def _build_chapter_generation_units(
    subject: Dict[str, Any],
    counts: Dict[str, int],
    max_questions: int,
) -> List[Dict[str, Any]]:
    chapters = subject["chapters"]
    groups = group_chapters(chapters, counts, max_questions)
    if not groups:
        raise ValueError(f"Chapter plan for {subject['name']} has no positive allocations.")

    sections = subject["spec_data"].get("sections", [])
    if not sections:
        raise ValueError(
            f"Chapter generation requires explicit section allocations for {subject['name']}."
        )
    group_totals = [int(group["question_count"]) for group in groups]
    section_totals = [
        int(section.get("question_count", section.get("total_questions", 0)))
        for section in sections
    ]
    group_section_counts = allocate_matrix(group_totals, section_totals)
    unit_groups = []

    for group_index, group in enumerate(groups, start=1):
        group_sections = []
        group_type_counts: Dict[str, int] = {}
        for section_index, source_section in enumerate(sections):
            type_allocations = source_section.get("question_types", [])
            type_names = [str(allocation["type"]) for allocation in type_allocations]
            type_totals = [int(allocation["count"]) for allocation in type_allocations]
            allocation = allocate_matrix(
                [group_section_counts[index][section_index] for index in range(len(groups))],
                type_totals,
            )
            group_types = []
            for type_index, type_name in enumerate(type_names):
                count = allocation[group_index - 1][type_index]
                if count:
                    group_types.append({"type": type_name, "count": count})
                    group_type_counts[type_name] = group_type_counts.get(type_name, 0) + count
            section = copy.deepcopy(source_section)
            section["question_count"] = group_section_counts[group_index - 1][section_index]
            section.pop("question_number_start", None)
            section.pop("question_number_end", None)
            section["question_types"] = group_types
            if section["question_count"]:
                group_sections.append(section)

        group_count = int(group["question_count"])
        if sum(section["question_count"] for section in group_sections) != group_count:
            raise ValueError(f"Section allocation failed for chapter group {group_index}.")

        unit_spec = copy.deepcopy(subject["spec_data"])
        unit_spec["paper_title"] = (
            f"{unit_spec.get('paper_title', subject['name'])} - Group {group_index}"
        )
        unit_spec["total_questions"] = group_count
        unit_spec["subjects"][0]["total_questions"] = group_count
        unit_spec["sections"] = group_sections
        unit_spec["question_types"] = {
            "mode": "exact",
            "types": [
                {"type": type_name, "count": count}
                for type_name, count in group_type_counts.items()
            ],
        }

        profile = unit_spec.get("instruction_profile")
        if isinstance(profile, dict):
            profile["sections"] = copy.deepcopy(group_sections)
            profile["global_instructions"] = [
                instruction
                for instruction in profile.get("global_instructions", [])
                if not re.search(
                    r"\b(?:exactly|total of)\b.*\bquestions?\b",
                    str(instruction),
                    flags=re.IGNORECASE,
                )
            ]
            profile["global_instructions"].append(
                f"Generate exactly {group_count} questions for {subject['name']} "
                "using only the chapter quotas specified in the user request."
            )

        section_marks = sum(
            int(section["question_count"])
            * float(section.get("scoring", {}).get("correct", 0))
            for section in group_sections
        )
        unit_spec["total_marks"] = section_marks
        if isinstance(profile, dict) and isinstance(profile.get("exam"), dict):
            profile["exam"]["total_marks"] = section_marks

        unit_groups.append(
            {
                "id": f"{subject['token']}_group_{group_index:03d}",
                "name": subject["name"],
                "token": f"{subject['token']}_group_{group_index:03d}",
                "question_count": group_count,
                "chapters": group["chapters"],
                "chapter_counts": {
                    chapter["id"]: counts[chapter["id"]]
                    for chapter in group["chapters"]
                },
                "spec_data": unit_spec,
                "sections": group_sections,
            }
        )
    return unit_groups


class PaperBatchRunner:
    """Runs paper blueprints sequentially with validated, resumable checkpoints."""

    VALID_UNIQUENESS_SCOPES = {"all_papers", "paper_type", "test_set", "none"}

    def __init__(self, generator: Any):
        self.generator = generator

    def _resolve_papers(
        self,
        batch_spec_path: Path,
        batch_spec: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        paper_entries = batch_spec.get("papers")
        if not paper_entries:
            paper_entries = [{"id": batch_spec.get("paper_id", "paper"), "spec": batch_spec_path.name}]
        if not isinstance(paper_entries, list) or not paper_entries:
            raise ValueError("'papers' must be a non-empty list of paper spec references.")

        resolved: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        for entry in paper_entries:
            if not isinstance(entry, dict):
                raise ValueError("Every batch 'papers' entry must be a mapping.")
            paper_id = _safe_token(str(entry.get("id", "")))
            if paper_id in seen_ids:
                raise ValueError(f"Duplicate paper id in batch spec: {paper_id}")
            seen_ids.add(paper_id)

            reference_value = entry.get("reference_paper")
            if "reference_paper" in entry and (
                not isinstance(reference_value, str) or not reference_value.strip()
            ):
                raise ValueError(f"Reference for paper '{paper_id}' must be a non-empty path string.")
            reference_path = None
            if reference_value is not None:
                reference_path = Path(reference_value).expanduser()
                if not reference_path.is_absolute():
                    reference_path = batch_spec_path.parent / reference_path
                reference_path = reference_path.resolve()

            spec_value = entry.get("spec")
            spec_path = (
                batch_spec_path
                if spec_value is None
                else _resolve_input_path(str(spec_value), batch_spec_path.parent)
            )
            if not spec_path.is_file():
                raise FileNotFoundError(f"Paper spec not found for '{paper_id}': {spec_path}")
            paper_data = _read_spec(spec_path)
            question_count = _question_count(paper_data, spec_path)
            chapter_settings = paper_data.get("chapter_generation") or {}
            if not isinstance(chapter_settings, dict):
                raise ValueError(f"'chapter_generation' must be a mapping in {spec_path}.")
            if chapter_settings.get("enabled"):
                max_questions = int(
                    chapter_settings.get("max_questions_per_request", 15)
                )
                if max_questions < 1:
                    raise ValueError(
                        f"chapter_generation.max_questions_per_request must be "
                        f"positive in {spec_path}."
                    )
                subject_specs = _subject_generation_specs(paper_data, spec_path)
                for subject in subject_specs:
                    subject_data = subject["spec_data"]["subjects"][0]
                    syllabus_path = _resolve_input_path(
                        str(subject_data.get("syllabus_file", "")),
                        spec_path.parent,
                    )
                    if not syllabus_path.is_file():
                        raise FileNotFoundError(
                            f"Syllabus not found for chapter generation: {syllabus_path}"
                        )
                    subject["syllabus_path"] = syllabus_path.resolve()
                    subject["chapters"] = parse_syllabus_chapters(syllabus_path)
            else:
                subject_specs = _subject_generation_specs(paper_data, spec_path)
            resolved.append(
                {
                    "id": paper_id,
                    "spec_path": spec_path,
                    "spec_data": paper_data,
                    "question_count": question_count,
                    "subject_specs": subject_specs,
                    "chapter_generation": chapter_settings,
                    "reference_paper": reference_path,
                }
            )
        return resolved

    @staticmethod
    def _source_fingerprints(
        batch_spec_path: Path,
        papers: List[Dict[str, Any]],
    ) -> Dict[str, Optional[str]]:
        files: Dict[str, Optional[str]] = {}
        candidates = {batch_spec_path.resolve()}
        for paper in papers:
            spec_path = paper["spec_path"]
            candidates.add(spec_path)
            for subject in paper["spec_data"].get("subjects", []):
                syllabus_value = subject.get("syllabus_file")
                if not syllabus_value:
                    continue
                syllabus_path = _resolve_input_path(
                    str(syllabus_value),
                    spec_path.parent,
                )
                candidates.add(syllabus_path)

        for path in sorted(candidates, key=str):
            if not path.is_file():
                if path == batch_spec_path.resolve():
                    raise FileNotFoundError(f"Spec file not found: {path}")
                raise FileNotFoundError(f"Referenced syllabus/spec file not found: {path}")
            content_sha256 = _sha256_file(path)
            files[
                _portable_path_reference(path, content_sha256=content_sha256)
            ] = content_sha256
        return files

    def _configuration(
        self,
        batch_spec: Dict[str, Any],
        papers: List[Dict[str, Any]],
        source_fingerprints: Dict[str, Optional[str]],
        test_count: int,
        uniqueness_scope: str,
        reference_paper: Optional[Path],
        reference_papers: Dict[str, Path],
    ) -> Dict[str, Any]:
        communicator = self.generator.communicator
        configuration = {
            "generation_strategy": (
                "chapter-grouped"
                if any(paper.get("chapter_generation", {}).get("enabled") for paper in papers)
                else "subject"
            ),
            "test_count": test_count,
            "uniqueness_scope": uniqueness_scope,
            "papers": [
                {
                    "id": paper["id"],
                    "spec_path": _portable_path_reference(paper["spec_path"]),
                    "question_count": paper["question_count"],
                }
                for paper in papers
            ],
            "source_fingerprints": source_fingerprints,
            "model_name": getattr(communicator, "model_name", None),
            "agent_name": getattr(communicator, "agent_name", None),
            "communicator": type(communicator).__name__,
            "languages": list(getattr(self.generator, "languages", [])),
            "standards": getattr(self.generator, "standards", None),
            "tags": getattr(self.generator, "tags", None),
            "difficulty_mix": getattr(self.generator, "difficulty_mix", None),
            "output_format": getattr(self.generator, "output_format", None),
            "pdf_engine": getattr(self.generator, "pdf_engine", None),
            "exam_duration_minutes": getattr(
                self.generator,
                "exam_duration_minutes",
                None,
            ),
            "batch_spec": _portable_configuration_value(batch_spec),
        }
        if reference_paper is not None:
            configuration["reference_paper"] = reference_fingerprint(reference_paper)
            configuration["reference_analysis"] = {
                "mode": getattr(self.generator, "reference_analysis_mode", "batched"),
                "batch_pages": REFERENCE_BATCH_SIZE,
                "batch_questions": REFERENCE_BATCH_SIZE,
            }
        if reference_papers:
            configuration["reference_papers"] = {
                paper_id: reference_fingerprint(path)
                for paper_id, path in sorted(reference_papers.items())
            }
            configuration["reference_analysis"] = {
                "mode": getattr(self.generator, "reference_analysis_mode", "batched"),
                "batch_pages": REFERENCE_BATCH_SIZE,
                "batch_questions": REFERENCE_BATCH_SIZE,
            }
        return configuration

    @staticmethod
    def _scope_items(
        items: List[Dict[str, Any]],
        current: Dict[str, Any],
        scope: str,
    ) -> List[Dict[str, Any]]:
        if scope == "all_papers":
            return items
        if scope == "paper_type":
            return [item for item in items if item["paper_id"] == current["paper_id"]]
        if scope == "test_set":
            return [item for item in items if item["test_index"] == current["test_index"]]
        return []

    @staticmethod
    def _write_manifest(path: Path, manifest: Dict[str, Any]) -> None:
        manifest["updated_at"] = __import__("datetime").datetime.now().astimezone().isoformat()
        _write_json_atomically(path, _portable_manifest_value(manifest))

    def _commit_ready_item(
        self,
        item: Dict[str, Any],
        manifest: Dict[str, Any],
        manifest_path: Path,
        state_dir: Path,
        output_dir: Path,
    ) -> None:
        candidate_path = state_dir / item["candidate_file"]
        output_path = output_dir / item["output_file"]
        expected_hash = item["candidate_sha256"]
        if candidate_path.is_file():
            if _sha256_file(candidate_path) != expected_hash:
                raise ValueError(f"Generated candidate changed before commit: {candidate_path}")
            _write_text_atomically(output_path, candidate_path.read_text(encoding="utf-8"))
        elif not output_path.is_file() or _sha256_file(output_path) != expected_hash:
            raise ValueError(
                f"Cannot recover paper commit; candidate and final output are unavailable: {output_path}"
            )
        item["status"] = "completed"
        item["output_sha256"] = _sha256_file(output_path)
        item.pop("candidate_file", None)
        item.pop("candidate_sha256", None)
        self._write_manifest(manifest_path, manifest)

    def run(
        self,
        spec_path: Path,
        output_dir: Path,
        *,
        num_tests: Optional[int] = None,
        resume: bool = False,
        force: bool = False,
        max_attempts: int = 3,
        no_reference_papers: bool = False,
    ) -> List[Path]:
        if getattr(self.generator, "output_format", "xml") != "xml":
            raise ValueError("Checkpointed batch-paper generation currently requires Moodle XML output.")
        if resume and force:
            raise ValueError("--resume and --force cannot be combined.")
        if max_attempts < 1:
            raise ValueError("--max-paper-attempts must be at least 1.")

        spec_path = Path(spec_path).resolve()
        output_dir = Path(output_dir).resolve()
        if not spec_path.is_file():
            raise FileNotFoundError(f"Batch spec file not found: {spec_path}")
        batch_spec = _read_spec(spec_path)
        papers = self._resolve_papers(spec_path, batch_spec)
        cli_reference_papers = getattr(self.generator, "reference_papers", {})
        reference_paper = getattr(self.generator, "reference_paper", None)
        if reference_paper is not None and cli_reference_papers:
            raise ValueError("A shared reference cannot be combined with paper-specific references.")
        if no_reference_papers and (reference_paper is not None or cli_reference_papers):
            raise ValueError("--no-reference-paper cannot be combined with reference options.")
        unknown_ids = set(cli_reference_papers) - {paper["id"] for paper in papers}
        if unknown_ids:
            raise ValueError("Unknown reference paper ID(s): " + ", ".join(sorted(unknown_ids)))
        reference_papers: Dict[str, Path] = {}
        if not no_reference_papers and reference_paper is None:
            reference_papers = {
                paper["id"]: paper["reference_paper"]
                for paper in papers if paper["reference_paper"] is not None
            }
            reference_papers.update(cli_reference_papers)
        self.generator.reference_profile = None
        batch_chapter_settings = batch_spec.get("chapter_generation") or {}
        if not isinstance(batch_chapter_settings, dict):
            raise ValueError("'chapter_generation' in the batch spec must be a mapping.")
        chapter_generation_enabled = bool(
            batch_chapter_settings.get("enabled")
            or any(paper.get("chapter_generation", {}).get("enabled") for paper in papers)
        )
        configured_test_count = (
            num_tests
            if num_tests is not None
            else batch_spec.get("test_count", 1 if chapter_generation_enabled else None)
        )
        if configured_test_count is None:
            raise ValueError("Batch spec requires 'test_count' or the CLI option --num-tests.")
        test_count = int(configured_test_count)
        if test_count < 1:
            raise ValueError("The number of test sets must be at least 1.")
        uniqueness_scope = str(batch_spec.get("uniqueness_scope", "all_papers")).casefold()
        if uniqueness_scope not in self.VALID_UNIQUENESS_SCOPES:
            raise ValueError(
                "'uniqueness_scope' must be one of: "
                + ", ".join(sorted(self.VALID_UNIQUENESS_SCOPES))
            )

        exam_name = str(batch_spec.get("exam_name", "mock_exam"))
        exam_token = _safe_token(exam_name)
        manifest_path = output_dir / f"{exam_token}_batch_generation.json"
        state_dir = output_dir / f".{exam_token}_batch_generation"
        state_dir.mkdir(parents=True, exist_ok=True)
        source_fingerprints = self._source_fingerprints(spec_path, papers)
        configuration = self._configuration(
            batch_spec,
            papers,
            source_fingerprints,
            test_count,
            uniqueness_scope,
            reference_paper,
            reference_papers,
        )
        configuration_sha256 = hashlib.sha256(
            json.dumps(configuration, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()
        if resume and not manifest_path.is_file():
            logger.warning(
                "No batch-generation checkpoint exists at %s; starting a new run.",
                manifest_path,
            )
            resume = False

        items = [
            {
                "test_index": test_index,
                "paper_id": paper["id"],
                "question_count": paper["question_count"],
                "output_file": f"{exam_token}_test_{test_index:03d}_{paper['id']}.xml",
                "status": "pending",
                "attempts": [],
                "question_fingerprints": [],
                "question_summaries": [],
            }
            for test_index in range(1, test_count + 1)
            for paper in papers
        ]
        if resume:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Unable to read batch-generation checkpoint: {manifest_path}") from exc
            saved_configuration = (
                manifest.get("configuration")
                if isinstance(manifest, dict)
                else None
            )
            normalized_saved_configuration = (
                _portable_configuration_value(saved_configuration)
                if isinstance(saved_configuration, dict)
                else saved_configuration
            )
            current_configuration = _portable_configuration_value(configuration)
            saved_test_count = (
                manifest.get("test_count")
                if isinstance(manifest, dict)
                else None
            )
            if (
                isinstance(saved_test_count, bool)
                or not isinstance(saved_test_count, int)
                or saved_test_count < 1
            ):
                saved_test_count = None
            saved_compatible_configuration = (
                dict(normalized_saved_configuration)
                if isinstance(normalized_saved_configuration, dict)
                else None
            )
            ignored_resume_fields = {"model_name", "agent_name"}
            if saved_compatible_configuration is not None:
                saved_compatible_configuration.pop("test_count", None)
                for key in ignored_resume_fields:
                    saved_compatible_configuration.pop(key, None)
            current_configuration.pop("test_count", None)
            for key in ignored_resume_fields:
                current_configuration.pop(key, None)
            same_configuration_except_count = (
                saved_compatible_configuration is not None
                and saved_compatible_configuration == current_configuration
            )
            incompatible_checkpoint = (
                not isinstance(manifest, dict)
                or manifest.get("schema_version") != 2
                or (
                    manifest.get("configuration_sha256") != configuration_sha256
                    and not same_configuration_except_count
                )
                or saved_test_count is None
            )
            if incompatible_checkpoint:
                differences = []
                if not isinstance(manifest, dict):
                    differences.append("checkpoint: expected a JSON object")
                else:
                    schema_version = manifest.get("schema_version")
                    if schema_version != 2:
                        differences.append(
                            f"schema_version: saved={schema_version!r}, requested=2"
                        )
                    if not isinstance(normalized_saved_configuration, dict):
                        differences.append(
                            "configuration: saved checkpoint has no valid configuration mapping"
                        )
                    else:
                        diagnostic_saved_configuration = dict(
                            saved_compatible_configuration or {}
                        )
                        diagnostic_current_configuration = dict(
                            current_configuration
                        )
                        differences.extend(
                            _configuration_differences(
                                diagnostic_saved_configuration,
                                diagnostic_current_configuration,
                            )
                        )
                        if (
                            manifest.get("configuration_sha256")
                            != configuration_sha256
                            and not differences
                        ):
                            differences.append(
                                "configuration_sha256 differs even though the saved "
                                "configuration values match"
                            )
                    if saved_test_count is None:
                        differences.append(
                            f"test_count: saved={manifest.get('test_count')!r}, "
                            "requested a positive integer"
                        )
                difference_text = "\n".join(f"- {entry}" for entry in differences)
                logger.error(
                    "Cannot resume batch generation because checkpoint settings "
                    "differ:\n%s",
                    difference_text or "- No specific difference could be determined.",
                )
                raise ValueError(
                    "Cannot resume batch generation: settings differ from the saved "
                    "checkpoint. See the preceding log entries for field-by-field "
                    "differences."
                )
            saved_items = manifest.get("items")
            if test_count < saved_test_count:
                raise ValueError(
                    f"Cannot resume with --num-tests {test_count}: the checkpoint "
                    f"already contains {saved_test_count} test set(s). Increase "
                    "--num-tests to append more sets, or use a new output directory."
                )
            expected_saved_items = [
                (test_index, paper["id"], f"{exam_token}_test_{test_index:03d}_{paper['id']}.xml")
                for test_index in range(1, saved_test_count + 1)
                for paper in papers
            ]
            if not isinstance(saved_items, list) or [
                (item.get("test_index"), item.get("paper_id"), item.get("output_file"))
                for item in saved_items
            ] != expected_saved_items:
                raise ValueError("The saved batch checkpoint has a different paper plan.")
            items = saved_items + [
                item for item in items if item["test_index"] > saved_test_count
            ]
            if (
                test_count > saved_test_count
                or manifest.get("configuration_sha256") != configuration_sha256
            ):
                manifest["test_count"] = test_count
                manifest["configuration"] = configuration
                manifest["configuration_sha256"] = configuration_sha256
                manifest["status"] = "in_progress"
                manifest["items"] = items
                self._write_manifest(manifest_path, manifest)
        elif manifest_path.exists() and not force:
            raise FileExistsError(
                f"A batch-generation checkpoint already exists: {manifest_path}. "
                "Use --resume to continue or --force to start a new run."
            )
        else:
            existing_outputs = [
                output_dir / item["output_file"]
                for item in items
                if (output_dir / item["output_file"]).exists()
            ]
            if existing_outputs and not force:
                raise FileExistsError(
                    "Batch output files already exist without a matching checkpoint. "
                    "Use a new --output-dir or --force."
                )
            manifest = {
                "schema_version": 2,
                "status": "in_progress",
                "exam_name": exam_name,
                "test_count": test_count,
                "uniqueness_scope": uniqueness_scope,
                "configuration": configuration,
                "configuration_sha256": configuration_sha256,
                "chapter_plans": {},
                "items": items,
            }
            self._write_manifest(manifest_path, manifest)

        batch_was_complete = bool(items) and all(
            item.get("status") == "completed" for item in items
        )
        if reference_paper is not None and not batch_was_complete:
            profile_path = state_dir / "reference_style_profile.json"
            saved_profile_hash = manifest.get("reference_profile_sha256")
            if saved_profile_hash is not None and (
                not profile_path.is_file() or _sha256_file(profile_path) != saved_profile_hash
            ):
                raise ValueError(
                    "Cannot resume batch generation: the reference style profile is missing or changed."
                )
            subject_names = list(dict.fromkeys(
                subject["name"] for paper in papers for subject in paper["subject_specs"]
            ))
            chapter_titles: Dict[str, List[str]] = {}
            for paper in papers:
                for subject in paper["subject_specs"]:
                    titles = chapter_titles.setdefault(subject["name"], [])
                    for chapter in subject.get("chapters", []):
                        if chapter["title"] not in titles:
                            titles.append(chapter["title"])
            self.generator.reference_profile = prepare_reference(
                reference_paper, subject_names, self.generator.communicator,
                self.generator.ocr_engine, prepare_reference_storage(
                    output_dir, exam_name, "shared", configuration["reference_paper"]
                ),
                profile_path, chapter_titles,
                analysis_mode=getattr(self.generator, "reference_analysis_mode", "batched"),
            )
            if reference_fingerprint(reference_paper) != configuration["reference_paper"]:
                raise ValueError("Reference paper or its images changed during batch preparation.")
            manifest["reference_profile_sha256"] = _sha256_file(profile_path)
            self._write_manifest(manifest_path, manifest)
        paper_profiles: Dict[str, Any] = {}
        if reference_papers and not batch_was_complete:
            profile_hashes = manifest.setdefault("reference_profile_sha256_by_paper", {})
            for paper in papers:
                paper_id = paper["id"]
                if paper_id not in reference_papers:
                    continue
                path = reference_papers[paper_id]
                fingerprint = configuration["reference_papers"][paper_id]
                reference_dir = prepare_reference_storage(output_dir, exam_name, paper_id, fingerprint)
                profile_path = reference_dir / "reference_style_profile.json"
                saved_hash = profile_hashes.get(paper_id)
                if saved_hash is not None and (
                    not profile_path.is_file() or _sha256_file(profile_path) != saved_hash
                ):
                    raise ValueError(
                        f"Cannot resume batch generation: reference style profile for "
                        f"'{paper_id}' is missing or changed."
                    )
                subjects = paper["subject_specs"]
                subject_names = list(dict.fromkeys(subject["name"] for subject in subjects))
                chapter_titles: Dict[str, List[str]] = {}
                for subject in subjects:
                    titles = chapter_titles.setdefault(subject["name"], [])
                    for chapter in subject.get("chapters", []):
                        if chapter["title"] not in titles:
                            titles.append(chapter["title"])
                paper_profiles[paper_id] = prepare_reference(
                    path, subject_names, self.generator.communicator,
                    self.generator.ocr_engine, reference_dir, profile_path, chapter_titles,
                    analysis_mode=getattr(self.generator, "reference_analysis_mode", "batched"),
                    purpose_label=paper_id,
                )
                if reference_fingerprint(path) != fingerprint:
                    raise ValueError(f"Reference for paper '{paper_id}' changed during batch preparation.")
                profile_hashes[paper_id] = _sha256_file(profile_path)
                self._write_manifest(manifest_path, manifest)
        manifest.setdefault("chapter_plans", {})
        for paper in papers:
            if not paper.get("chapter_generation", {}).get("enabled"):
                paper["generation_units"] = paper["subject_specs"]
                continue
            chapter_settings = paper["chapter_generation"]
            max_questions = int(chapter_settings.get("max_questions_per_request", 15))
            generation_units = []
            for subject in paper["subject_specs"]:
                plan_key = f"{paper['id']}:{subject['token']}"
                chapter_counts = manifest["chapter_plans"].get(plan_key)
                if chapter_counts is None:
                    manual_counts = chapter_settings.get("chapter_counts")
                    if isinstance(manual_counts, dict):
                        chapter_counts = {}
                        for chapter in subject["chapters"]:
                            count = manual_counts.get(
                                chapter["id"],
                                manual_counts.get(chapter["title"]),
                            )
                            if (
                                isinstance(count, bool)
                                or not isinstance(count, int)
                                or count < 0
                            ):
                                raise ValueError(
                                    f"chapter_generation.chapter_counts must specify a "
                                    f"non-negative integer for {chapter['title']!r} in "
                                    f"{paper['spec_path']}."
                                )
                            chapter_counts[chapter["id"]] = count
                        if sum(chapter_counts.values()) != subject["question_count"]:
                            raise ValueError(
                                f"Chapter allocations for {subject['name']} must sum to "
                                f"{subject['question_count']}."
                            )
                    else:
                        prompt = _chapter_plan_prompt(
                            subject["name"],
                            subject["question_count"],
                            subject["chapters"],
                        )
                        raw_plan = self.generator.communicator.generate(
                            system_instruction=(
                                "You are an exam-paper blueprint planner. Return only "
                                "valid JSON matching the requested chapter allocation "
                                "schema. Do not write questions."
                            ),
                            contents=prompt,
                            reset_session=True,
                            purpose=(
                                f"plan chapter question allocations for {subject['name']} "
                                f"(paper {paper['id']}, {subject['question_count']} questions)"
                            ),
                        )
                        chapter_counts = _decode_chapter_plan(
                            str(raw_plan),
                            subject["chapters"],
                            subject["question_count"],
                        )
                    manifest["chapter_plans"][plan_key] = chapter_counts
                    self._write_manifest(manifest_path, manifest)

                generation_units.extend(
                    _build_chapter_generation_units(
                        subject,
                        chapter_counts,
                        max_questions,
                    )
                )
            paper["generation_units"] = generation_units

        paper_by_id = {paper["id"]: paper for paper in papers}
        output_paths: List[Path] = []
        for item in items:
            output_path = output_dir / item["output_file"]
            if item.get("status") == "completed":
                if not output_path.is_file() or _sha256_file(output_path) != item.get("output_sha256"):
                    raise ValueError(
                        f"Completed paper output changed or is missing: {output_path}. "
                        "Use --force for a fresh batch."
                    )
                logger.info(
                    "Test set %d paper '%s' is already completed; verified existing "
                    "output at %s (%d questions).",
                    item["test_index"],
                    item["paper_id"],
                    output_path,
                    len(item.get("question_fingerprints", [])),
                )
                output_paths.append(output_path)
                continue
            if item.get("status") == "ready_to_commit":
                self._commit_ready_item(item, manifest, manifest_path, state_dir, output_dir)
                logger.info(
                    "Test set %d paper '%s' completed; %d questions added to %s.",
                    item["test_index"],
                    item["paper_id"],
                    len(item.get("question_fingerprints", [])),
                    output_path,
                )
                output_paths.append(output_path)
                continue

            paper = paper_by_id[item["paper_id"]]
            if reference_papers:
                self.generator.reference_profile = paper_profiles.get(item["paper_id"])
            accepted_scope_items = self._scope_items(items, item, uniqueness_scope)
            used_fingerprints = {
                fingerprint
                for accepted_item in accepted_scope_items
                if accepted_item.get("status") == "completed"
                for fingerprint in accepted_item.get("question_fingerprints", [])
            }
            used_summaries = [
                summary.get("summary", "")
                for accepted_item in accepted_scope_items
                if accepted_item.get("status") == "completed"
                for summary in accepted_item.get("question_summaries", [])
            ]
            attempt_list = item.setdefault("attempts", [])
            attempts_this_run = 0
            while attempts_this_run < max_attempts or (
                attempt_list
                and attempt_list[-1].get("status") in {
                    "in_progress",
                    "response_saved",
                    "failed",
                }
                and _has_saved_generation_response(
                    attempt_list[-1],
                    state_dir,
                    paper["generation_units"],
                )
            ):
                attempt_record = None
                if attempt_list:
                    latest_attempt = attempt_list[-1]
                    if latest_attempt.get("status") in {
                        "in_progress",
                        "response_saved",
                    }:
                        attempt_record = latest_attempt
                    elif (
                        latest_attempt.get("status") == "failed"
                        and _has_saved_generation_response(
                            latest_attempt,
                            state_dir,
                            paper["generation_units"],
                        )
                    ):
                        attempt_record = latest_attempt
                if attempt_record is not None and attempt_record.get("status") == "failed":
                    attempt_record["status"] = "response_saved"
                if attempt_record is None:
                    attempt_number = len(attempt_list) + 1
                    attempt_record = {
                        "attempt": attempt_number,
                        "status": "in_progress",
                        "response_files": {
                            unit["token"]: (
                                f"{item['test_index']:03d}_{item['paper_id']}_"
                                f"{unit['token']}.response.txt"
                            )
                            for unit in paper["generation_units"]
                        },
                        "error": None,
                    }
                    attempt_list.append(attempt_record)
                    self._write_manifest(manifest_path, manifest)

                attempt_dir = state_dir / (
                    f"test_{item['test_index']:03d}_{item['paper_id']}_"
                    f"attempt_{attempt_record['attempt']:02d}"
                )
                artifact_stem = f"{exam_token}_test_{item['test_index']:03d}_{item['paper_id']}"
                additional_prompt = (
                    f"\n\nBATCH GENERATION REQUIREMENTS:\n"
                    f"- This is test set {item['test_index']} of {test_count}, "
                    f"paper '{item['paper_id']}'. Follow this paper specification exactly.\n"
                    "- Generate novel questions; do not copy or paraphrase any previously "
                    "used question stems listed below.\n"
                )
                if used_summaries:
                    additional_prompt += "\nPreviously used question stems:\n"
                    additional_prompt += "\n".join(
                        f"- {summary[:240]}" for summary in used_summaries[-40:]
                    )
                attempt_record["status"] = "in_progress"
                item["status"] = "in_progress"
                self._write_manifest(manifest_path, manifest)
                unit_paths = []
                invalid_unit_output = False
                new_request_started = False
                for unit_index, unit in enumerate(
                    paper["generation_units"],
                    start=1,
                ):
                    logger.info(
                        "Test set %d paper '%s' group %d/%d (%s): requesting "
                        "%d question(s).",
                        item["test_index"],
                        item["paper_id"],
                        unit_index,
                        len(paper["generation_units"]),
                        unit["name"],
                        unit["question_count"],
                    )
                    response_path = state_dir / attempt_record["response_files"][unit["token"]]
                    raw_response: Optional[str] = None
                    if response_path.is_file():
                        saved_response = response_path.read_text(encoding="utf-8")
                        if saved_response.strip():
                            raw_response = saved_response
                    if raw_response is None and not new_request_started:
                        attempts_this_run += 1
                        new_request_started = True
                    unit_dir = attempt_dir / unit["token"]
                    unit_dir.mkdir(parents=True, exist_ok=True)
                    subject_spec_path = unit_dir / "subject_spec.yaml"
                    subject_spec_data = copy.deepcopy(unit["spec_data"])
                    if unit.get("chapters"):
                        syllabus_path = unit_dir / "group_syllabus.md"
                        syllabus_text = "\n\n".join(
                            chapter["content"] for chapter in unit["chapters"]
                        )
                        syllabus_path.write_text(syllabus_text, encoding="utf-8")
                        subject_spec_data["subjects"][0]["syllabus_file"] = (
                            syllabus_path.name
                        )
                    else:
                        for subject_entry in subject_spec_data.get("subjects", []):
                            syllabus_value = subject_entry.get("syllabus_file")
                            if syllabus_value:
                                syllabus_path = _resolve_input_path(
                                    str(syllabus_value),
                                    paper["spec_path"].parent,
                                )
                                if syllabus_path.is_file():
                                    subject_entry["syllabus_file"] = Path(
                                        os.path.relpath(
                                            syllabus_path,
                                            start=unit_dir,
                                        ).replace("\\", "/")
                                    ).as_posix()
                    subject_spec_path.write_text(
                        yaml.safe_dump(
                            subject_spec_data,
                            allow_unicode=True,
                            sort_keys=False,
                        ),
                        encoding="utf-8",
                    )
                    subject_prompt = (
                        f"{additional_prompt}\n"
                        f"- Generate exactly {unit['question_count']} questions for "
                        f"{unit['name']} only; do not generate questions for other subjects.\n"
                    )
                    if unit.get("chapters"):
                        subject_prompt += (
                            "- Assign questions to chapters exactly as listed below. "
                            "For every question, set its Moodle <name><text> to start "
                            "with the exact chapter ID in square brackets, e.g. "
                            "[ch001-example] Question title.\n"
                        )
                        subject_prompt += "\nChapter quotas:\n"
                        subject_prompt += "\n".join(
                            f"- [{chapter_id}] {next(chapter['title'] for chapter in unit['chapters'] if chapter['id'] == chapter_id)}: "
                            f"{count} question(s)"
                            for chapter_id, count in unit["chapter_counts"].items()
                        )
                        subject_prompt += "\nQuestion-type quotas for this group:\n"
                        subject_prompt += "\n".join(
                            f"- {section['name']}: {section['question_count']} question(s); "
                            + ", ".join(
                                f"{allocation['type']}={allocation['count']}"
                                for allocation in section["question_types"]
                            )
                            for section in unit["sections"]
                        )
                    subject_stem = f"{artifact_stem}_{unit['token']}"
                    try:
                        subject_path = self.generator.process_spec(
                            subject_spec_path,
                            unit_dir,
                            output_stem=subject_stem,
                            additional_prompt=subject_prompt,
                            raw_response_path=response_path,
                            raw_response_override=raw_response,
                        )
                    except Exception as exc:
                        attempt_record["status"] = "failed"
                        attempt_record["error"] = f"{type(exc).__name__}: {exc}"
                        item["status"] = "failed"
                        self._write_manifest(manifest_path, manifest)
                        raise

                    try:
                        unit_records = _question_records(
                            Path(subject_path),
                            unit["question_count"],
                            unit.get("chapter_counts"),
                        )
                    except ValueError as exc:
                        received_count = _question_count_for_log(Path(subject_path))
                        logger.warning(
                            "Test set %d paper '%s' group %d/%d (%s): requested "
                            "%d question(s), received %s, but the response failed "
                            "validation: %s",
                            item["test_index"],
                            item["paper_id"],
                            unit_index,
                            len(paper["generation_units"]),
                            unit["name"],
                            unit["question_count"],
                            (
                                f"{received_count} question(s)"
                                if received_count is not None
                                else "an uncountable/malformed XML response"
                            ),
                            exc,
                        )
                        attempt_record["status"] = "invalid"
                        attempt_record["error"] = (
                            f"Invalid {unit['name']} group output: {exc}"
                        )
                        if response_path.is_file():
                            response_path.unlink()
                        item["status"] = "pending"
                        self._write_manifest(manifest_path, manifest)
                        invalid_unit_output = True
                        break
                    logger.info(
                        "Test set %d paper '%s' group %d/%d (%s): received %d/%d "
                        "question(s).",
                        item["test_index"],
                        item["paper_id"],
                        unit_index,
                        len(paper["generation_units"]),
                        unit["name"],
                        len(unit_records),
                        unit["question_count"],
                    )
                    unit_paths.append(Path(subject_path))

                if invalid_unit_output:
                    if attempts_this_run >= max_attempts:
                        break
                    continue

                candidate_path = _combine_subject_xml(
                    unit_paths,
                    attempt_dir / f"{artifact_stem}_combined.xml",
                )

                try:
                    question_records = _question_records(
                        Path(candidate_path),
                        item["question_count"],
                    )
                except ValueError as exc:
                    attempt_record["status"] = "invalid"
                    attempt_record["error"] = str(exc)
                    item["status"] = "pending"
                    self._write_manifest(manifest_path, manifest)
                    if attempts_this_run >= max_attempts:
                        break
                    continue

                current_fingerprints = [record["fingerprint"] for record in question_records]
                local_seen: Set[str] = set()
                duplicate_records = []
                used_summary_by_hash = {
                    summary["fingerprint"]: summary
                    for accepted_item in accepted_scope_items
                    if accepted_item.get("status") == "completed"
                    for summary in accepted_item.get("question_summaries", [])
                }
                for record in question_records:
                    if (
                        record["fingerprint"] in used_fingerprints
                        or record["fingerprint"] in local_seen
                    ):
                        duplicate_records.append(
                            used_summary_by_hash.get(
                                record["fingerprint"],
                                {"summary": record["summary"], "name": record["name"]},
                            )
                        )
                    local_seen.add(record["fingerprint"])
                if duplicate_records:
                    attempt_record["status"] = "duplicate_questions"
                    attempt_record["duplicate_count"] = len(duplicate_records)
                    attempt_record["error"] = (
                        f"{len(duplicate_records)} duplicate question(s) detected."
                    )
                    item["status"] = "pending"
                    item["last_duplicate_summaries"] = [
                        entry.get("summary", "") for entry in duplicate_records[:40]
                    ]
                    for response_file in attempt_record.get("response_files", {}).values():
                        response_path = state_dir / response_file
                        if response_path.is_file():
                            response_path.unlink()
                    self._write_manifest(manifest_path, manifest)
                    if attempts_this_run >= max_attempts:
                        break
                    used_summaries.extend(
                        entry.get("summary", "") for entry in duplicate_records
                    )
                    continue

                candidate_name = (
                    f"{item['test_index']:03d}_{item['paper_id']}_candidate.xml"
                )
                candidate_xml = Path(candidate_path).read_text(encoding="utf-8")
                candidate_checkpoint = state_dir / candidate_name
                _write_text_atomically(candidate_checkpoint, candidate_xml)
                item["candidate_file"] = candidate_name
                item["candidate_sha256"] = _sha256_file(candidate_checkpoint)
                item["question_fingerprints"] = current_fingerprints
                item["question_summaries"] = question_records
                item["status"] = "ready_to_commit"
                attempt_record["status"] = "completed"
                attempt_record["error"] = None
                self._write_manifest(manifest_path, manifest)
                self._commit_ready_item(
                    item,
                    manifest,
                    manifest_path,
                    state_dir,
                    output_dir,
                )
                logger.info(
                    "Test set %d paper '%s' completed; %d questions added to %s.",
                    item["test_index"],
                    item["paper_id"],
                    len(question_records),
                    output_path,
                )
                output_paths.append(output_path)
                break

            if item.get("status") != "completed":
                item["status"] = "failed"
                manifest["status"] = "failed"
                self._write_manifest(manifest_path, manifest)
                raise ValueError(
                    f"Could not generate a valid, distinct paper for test "
                    f"{item['test_index']} ({item['paper_id']}) after "
                    f"{max_attempts} attempt(s). The checkpoint is saved at {manifest_path}."
                )

        manifest["status"] = "completed"
        self._write_manifest(manifest_path, manifest)
        questions_by_test: Dict[int, int] = {}
        papers_by_test: Dict[int, int] = {}
        for item in items:
            if item.get("status") != "completed":
                continue
            test_index = int(item["test_index"])
            questions_by_test[test_index] = (
                questions_by_test.get(test_index, 0)
                + len(item.get("question_fingerprints", []))
            )
            papers_by_test[test_index] = papers_by_test.get(test_index, 0) + 1
        for test_index in sorted(questions_by_test):
            logger.info(
                "Test set %d completed: %d total question(s) added across %d paper(s).",
                test_index,
                questions_by_test[test_index],
                papers_by_test[test_index],
            )
        if batch_was_complete:
            logger.info(
                "Batch is already complete: all %d paper output(s) exist and were "
                "verified; no new questions were generated.",
                len(items),
            )
        return output_paths


def is_batch_paper_spec(spec_path: Path, num_tests: Optional[int] = None) -> bool:
    """Return whether a spec requests repeated or multi-blueprint paper generation."""
    if num_tests is not None:
        return True
    spec_path = Path(spec_path)
    if not spec_path.is_file() or spec_path.suffix.casefold() not in {".yaml", ".yml", ".json"}:
        return False
    spec_data = _read_spec(spec_path)
    chapter_generation = spec_data.get("chapter_generation") or {}
    return (
        "papers" in spec_data
        or "test_count" in spec_data
        or (
            isinstance(chapter_generation, dict)
            and bool(chapter_generation.get("enabled"))
        )
    )
