"""Checkpointed generation of multiple spec-defined mock-paper sets."""

from __future__ import annotations

import copy
import hashlib
import html
import json
import logging
import re
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import yaml

logger = logging.getLogger("academic_content_pipeline")


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


def _safe_token(value: str) -> str:
    token = re.sub(r"[^a-zA-Z0-9_-]+", "_", value.strip()).strip("_")
    if not token:
        raise ValueError(f"Cannot create a safe identifier from {value!r}.")
    return token.lower()


def _question_records(xml_path: Path, expected_count: int) -> List[Dict[str, str]]:
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
    for index, question in enumerate(questions, start=1):
        question_text = question.find("questiontext")
        if question_text is None:
            raise ValueError(f"Question {index} in {xml_path} has no <questiontext>.")
        name_node = question.find("name")
        name = " ".join(" ".join(name_node.itertext()).split()) if name_node is not None else ""

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
        records.append(
            {
                "fingerprint": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                "name": name or f"Question {index}",
                "summary": canonical[:240],
            }
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
    matching = [
        copy.deepcopy(section)
        for section in sections
        if str(section.get("subject", "all")).casefold() == subject_name.casefold()
    ]
    selected = matching or [
        copy.deepcopy(section)
        for section in sections
        if str(section.get("subject", "all")).casefold() == "all"
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
    combined_root = ET.Element("quiz")
    for subject_path in subject_paths:
        try:
            subject_root = ET.parse(subject_path).getroot()
        except (OSError, ET.ParseError) as exc:
            raise ValueError(
                f"Generated subject output is not well-formed XML: {subject_path}"
            ) from exc
        if subject_root.tag != "quiz":
            raise ValueError(
                f"Generated subject XML root must be <quiz>: {subject_path}"
            )
        combined_root.extend(list(subject_root))
    xml_text = ET.tostring(combined_root, encoding="unicode")
    _write_text_atomically(
        output_path,
        '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_text + "\n",
    )
    return output_path


def _has_saved_subject_response(
    attempt_record: Dict[str, Any],
    state_dir: Path,
    subject_specs: List[Dict[str, Any]],
) -> bool:
    response_files = attempt_record.get("response_files", {})
    return any(
        (state_dir / response_files.get(subject["token"], "")).is_file()
        and (state_dir / response_files.get(subject["token"], "")).stat().st_size > 0
        for subject in subject_specs
    )


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

            spec_value = entry.get("spec")
            spec_path = batch_spec_path if spec_value is None else Path(str(spec_value))
            if not spec_path.is_absolute():
                spec_path = (batch_spec_path.parent / spec_path).resolve()
            else:
                spec_path = spec_path.resolve()
            if not spec_path.is_file():
                raise FileNotFoundError(f"Paper spec not found for '{paper_id}': {spec_path}")
            paper_data = _read_spec(spec_path)
            question_count = _question_count(paper_data, spec_path)
            resolved.append(
                {
                    "id": paper_id,
                    "spec_path": spec_path,
                    "spec_data": paper_data,
                    "question_count": question_count,
                    "subject_specs": _subject_generation_specs(paper_data, spec_path),
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
                syllabus_path = Path(str(syllabus_value))
                if not syllabus_path.is_absolute() and not syllabus_path.is_file():
                    relative_to_spec = spec_path.parent / syllabus_path
                    if relative_to_spec.is_file():
                        syllabus_path = relative_to_spec
                syllabus_path = syllabus_path.resolve()
                candidates.add(syllabus_path)

        for path in sorted(candidates, key=str):
            if not path.is_file():
                if path == batch_spec_path.resolve():
                    raise FileNotFoundError(f"Spec file not found: {path}")
                raise FileNotFoundError(f"Referenced syllabus/spec file not found: {path}")
            files[str(path)] = _sha256_file(path)
        return files

    def _configuration(
        self,
        batch_spec: Dict[str, Any],
        papers: List[Dict[str, Any]],
        source_fingerprints: Dict[str, Optional[str]],
        test_count: int,
        uniqueness_scope: str,
    ) -> Dict[str, Any]:
        communicator = self.generator.communicator
        return {
            "generation_strategy": "subject",
            "test_count": test_count,
            "uniqueness_scope": uniqueness_scope,
            "papers": [
                {
                    "id": paper["id"],
                    "spec_path": str(paper["spec_path"]),
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
            "batch_spec": batch_spec,
        }

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
        _write_json_atomically(path, manifest)

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
        configured_test_count = num_tests if num_tests is not None else batch_spec.get("test_count")
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
        )
        configuration_sha256 = hashlib.sha256(
            json.dumps(configuration, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
        ).hexdigest()

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
            if not manifest_path.is_file():
                raise FileNotFoundError(
                    f"No batch-generation checkpoint exists at {manifest_path}; "
                    "start without --resume."
                )
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"Unable to read batch-generation checkpoint: {manifest_path}") from exc
            if (
                not isinstance(manifest, dict)
                or manifest.get("schema_version") != 1
                or manifest.get("configuration_sha256") != configuration_sha256
            ):
                raise ValueError(
                    "Cannot resume batch generation: the spec, syllabus files, model, "
                    "or generation settings differ from the saved checkpoint."
                )
            saved_items = manifest.get("items")
            if not isinstance(saved_items, list) or [
                (item.get("test_index"), item.get("paper_id"), item.get("output_file"))
                for item in saved_items
            ] != [
                (item["test_index"], item["paper_id"], item["output_file"])
                for item in items
            ]:
                raise ValueError("The saved batch checkpoint has a different paper plan.")
            items = saved_items
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
                "schema_version": 1,
                "status": "in_progress",
                "exam_name": exam_name,
                "test_count": test_count,
                "uniqueness_scope": uniqueness_scope,
                "configuration": configuration,
                "configuration_sha256": configuration_sha256,
                "items": items,
            }
            self._write_manifest(manifest_path, manifest)

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
                output_paths.append(output_path)
                continue
            if item.get("status") == "ready_to_commit":
                self._commit_ready_item(item, manifest, manifest_path, state_dir, output_dir)
                output_paths.append(output_path)
                continue

            paper = paper_by_id[item["paper_id"]]
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
                and _has_saved_subject_response(
                    attempt_list[-1],
                    state_dir,
                    paper["subject_specs"],
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
                        and _has_saved_subject_response(
                            latest_attempt,
                            state_dir,
                            paper["subject_specs"],
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
                            subject["token"]: (
                                f"{item['test_index']:03d}_{item['paper_id']}_"
                                f"attempt_{attempt_number:02d}_"
                                f"{subject['token']}.response.txt"
                            )
                            for subject in paper["subject_specs"]
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
                subject_paths = []
                invalid_subject_output = False
                new_request_started = False
                for subject in paper["subject_specs"]:
                    response_path = state_dir / attempt_record["response_files"][subject["token"]]
                    raw_response: Optional[str] = None
                    if response_path.is_file():
                        saved_response = response_path.read_text(encoding="utf-8")
                        if saved_response.strip():
                            raw_response = saved_response
                    if raw_response is None and not new_request_started:
                        attempts_this_run += 1
                        new_request_started = True
                    subject_dir = attempt_dir / subject["token"]
                    subject_dir.mkdir(parents=True, exist_ok=True)
                    subject_spec_path = subject_dir / "subject_spec.yaml"
                    subject_spec_data = copy.deepcopy(subject["spec_data"])
                    for subject_entry in subject_spec_data.get("subjects", []):
                        syllabus_value = subject_entry.get("syllabus_file")
                        if syllabus_value:
                            syllabus_path = Path(str(syllabus_value))
                            if not syllabus_path.is_absolute() and not syllabus_path.is_file():
                                relative_to_spec = paper["spec_path"].parent / syllabus_path
                                if relative_to_spec.is_file():
                                    syllabus_path = relative_to_spec
                            if syllabus_path.is_file():
                                subject_entry["syllabus_file"] = str(syllabus_path.resolve())
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
                        f"- Generate exactly {subject['question_count']} questions for "
                        f"{subject['name']} only; do not generate questions for other subjects.\n"
                    )
                    subject_stem = f"{artifact_stem}_{subject['token']}"
                    try:
                        subject_path = self.generator.process_spec(
                            subject_spec_path,
                            subject_dir,
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
                        _question_records(
                            Path(subject_path),
                            subject["question_count"],
                        )
                    except ValueError as exc:
                        attempt_record["status"] = "invalid"
                        attempt_record["error"] = (
                            f"Invalid {subject['name']} output: {exc}"
                        )
                        item["status"] = "pending"
                        self._write_manifest(manifest_path, manifest)
                        invalid_subject_output = True
                        break
                    subject_paths.append(Path(subject_path))

                if invalid_subject_output:
                    if attempts_this_run >= max_attempts:
                        break
                    continue

                candidate_path = _combine_subject_xml(
                    subject_paths,
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
        return output_paths


def is_batch_paper_spec(spec_path: Path, num_tests: Optional[int] = None) -> bool:
    """Return whether a spec requests repeated or multi-blueprint paper generation."""
    if num_tests is not None:
        return True
    spec_path = Path(spec_path)
    if not spec_path.is_file() or spec_path.suffix.casefold() not in {".yaml", ".yml", ".json"}:
        return False
    spec_data = _read_spec(spec_path)
    return "papers" in spec_data or "test_count" in spec_data
