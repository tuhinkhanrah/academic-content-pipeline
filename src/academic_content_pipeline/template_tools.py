"""Deterministic generation and validation of exam paper templates."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

import yaml

DEFAULT_TEMPLATE_DIR = (
    Path(__file__).resolve().parent / "resources" / "spec_templates"
)
DEFAULT_MANIFEST_PATH = DEFAULT_TEMPLATE_DIR / "exam_template_manifest.yaml"
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
QUESTION_TYPE_CATALOG = (
    Path(__file__).resolve().parent / "resources" / "catalogs" / "mcq_types.yaml"
)
VALID_UNIQUENESS_SCOPES = {"all_papers", "paper_type", "test_set", "none"}


class TemplateValidationError(ValueError):
    """Raised when source definitions or generated templates are inconsistent."""


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TemplateValidationError(f"Unable to read YAML file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TemplateValidationError(f"Expected a YAML mapping in {path}.")
    return payload


def _write_yaml_atomically(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8", newline="\n") as output_file:
        output_file.write(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True))
    temporary_path.replace(path)


def _load_definitions(
    manifest_path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    manifest_path = Path(manifest_path).expanduser().resolve()
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Exam template manifest not found: {manifest_path}")
    manifest = _read_yaml(manifest_path)
    exams = manifest.get("exams")
    if not isinstance(exams, dict) or not exams:
        raise TemplateValidationError("'exams' must be a non-empty mapping.")
    target_questions = manifest.get("chapter_bank_target_questions")
    if isinstance(target_questions, bool) or not isinstance(target_questions, int) or target_questions < 1:
        raise TemplateValidationError(
            "'chapter_bank_target_questions' must be a positive integer."
        )

    template_source_dir = manifest_path.parent
    paper_specs: dict[tuple[str, str], dict[str, Any]] = {}
    for exam_id, exam in exams.items():
        if not isinstance(exam, dict):
            raise TemplateValidationError(f"Exam definition '{exam_id}' must be a mapping.")
        if not re.fullmatch(r"[a-z][a-z0-9_]*", str(exam_id)):
            raise TemplateValidationError(f"Invalid exam identifier: {exam_id!r}")
        if not str(exam.get("name", "")).strip():
            raise TemplateValidationError(f"Exam '{exam_id}' requires a display name.")
        test_count = exam.get("test_count")
        if isinstance(test_count, bool) or not isinstance(test_count, int) or test_count < 1:
            raise TemplateValidationError(
                f"Exam '{exam_id}' requires a positive integer test_count."
            )
        scope = exam.get("uniqueness_scope", "all_papers")
        if scope not in VALID_UNIQUENESS_SCOPES:
            raise TemplateValidationError(
                f"Exam '{exam_id}' has invalid uniqueness_scope {scope!r}."
            )
        papers = exam.get("papers")
        if not isinstance(papers, list) or not papers:
            raise TemplateValidationError(f"Exam '{exam_id}' requires paper references.")
        instructions = exam.get("instructions")
        if not isinstance(instructions, list) or not instructions:
            raise TemplateValidationError(f"Exam '{exam_id}' requires instruction file references.")
        for instruction in instructions:
            if not (REPOSITORY_ROOT / str(instruction)).is_file():
                raise FileNotFoundError(
                    f"Instruction file for '{exam_id}' not found: "
                    f"{REPOSITORY_ROOT / str(instruction)}"
                )

        seen_ids: set[str] = set()
        for paper in papers:
            if not isinstance(paper, dict):
                raise TemplateValidationError(
                    f"Every paper in exam '{exam_id}' must be a mapping."
                )
            paper_id = str(paper.get("id", ""))
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", paper_id):
                raise TemplateValidationError(
                    f"Invalid paper id in exam '{exam_id}': {paper_id!r}"
                )
            if paper_id in seen_ids:
                raise TemplateValidationError(
                    f"Duplicate paper id '{paper_id}' in exam '{exam_id}'."
                )
            seen_ids.add(paper_id)
            spec_value = paper.get("spec")
            if not isinstance(spec_value, str) or not spec_value:
                raise TemplateValidationError(
                    f"Paper '{exam_id}/{paper_id}' requires a spec path."
                )
            source_spec_path = (template_source_dir / spec_value).resolve()
            if template_source_dir.resolve() not in source_spec_path.parents:
                raise TemplateValidationError(
                    f"Paper spec path escapes template directory: {spec_value}"
                )
            if not source_spec_path.is_file():
                raise FileNotFoundError(
                    f"Paper spec for '{exam_id}/{paper_id}' not found: {source_spec_path}"
                )
            paper_specs[(exam_id, paper_id)] = _read_yaml(source_spec_path)

    catalog = _read_yaml(QUESTION_TYPE_CATALOG)
    type_names = {
        entry["name"]
        for entry in catalog.get("mcq_types", [])
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    return manifest, paper_specs, {"type_names": type_names}


def _section_type_counts(
    spec: dict[str, Any],
    exam_id: str,
    paper_id: str,
    type_names: set[str],
) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    subjects = spec.get("subjects")
    if not isinstance(subjects, list) or not subjects:
        raise TemplateValidationError(f"{exam_id}/{paper_id} must define subjects.")
    subject_totals: dict[str, int] = {}
    for subject in subjects:
        if not isinstance(subject, dict):
            raise TemplateValidationError(f"{exam_id}/{paper_id} has an invalid subject.")
        name = subject.get("name")
        count = subject.get("total_questions")
        if not isinstance(name, str) or not name:
            raise TemplateValidationError(f"{exam_id}/{paper_id} has a subject without a name.")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise TemplateValidationError(
                f"{exam_id}/{paper_id} subject '{name}' needs a positive total_questions."
            )
        if name in subject_totals:
            raise TemplateValidationError(
                f"{exam_id}/{paper_id} defines subject '{name}' more than once."
            )
        syllabus = subject.get("syllabus_file")
        if not isinstance(syllabus, str) or not (REPOSITORY_ROOT / syllabus).is_file():
            raise FileNotFoundError(
                f"Syllabus for {exam_id}/{paper_id}/{name} not found: {syllabus}"
            )
        subject_totals[name] = count

    sections = spec.get("sections")
    if not isinstance(sections, list) or not sections:
        raise TemplateValidationError(f"{exam_id}/{paper_id} must define sections.")
    profile_sections = spec.get("instruction_profile", {}).get("sections")
    if not isinstance(profile_sections, list) or len(profile_sections) != len(sections):
        raise TemplateValidationError(
            f"{exam_id}/{paper_id} instruction_profile.sections must mirror sections."
        )
    mirrored_keys = (
        "id",
        "name",
        "subject",
        "question_count",
        "question_types",
        "scoring",
        "answer_format",
    )
    for section, profile_section in zip(sections, profile_sections):
        if any(section.get(key) != profile_section.get(key) for key in mirrored_keys):
            raise TemplateValidationError(
                f"{exam_id}/{paper_id} section {section.get('id')!r} differs "
                "from instruction_profile.sections."
            )
    per_subject_types = {name: {} for name in subject_totals}
    per_subject_questions = {name: 0 for name in subject_totals}
    per_subject_marks = {name: 0.0 for name in subject_totals}
    global_type_counts: dict[str, int] = {}

    for section in sections:
        section_name = str(section.get("name", section.get("id", "section")))
        count = section.get("question_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise TemplateValidationError(
                f"{exam_id}/{paper_id}/{section_name} has an invalid question_count."
            )
        target_subject = section.get("subject", "all")
        targets = list(subject_totals) if target_subject == "all" else [target_subject]
        if any(target not in subject_totals for target in targets):
            raise TemplateValidationError(
                f"{exam_id}/{paper_id}/{section_name} references unknown subject "
                f"{target_subject!r}."
            )
        allocations = section.get("question_types")
        if not isinstance(allocations, list) or not allocations:
            raise TemplateValidationError(
                f"{exam_id}/{paper_id}/{section_name} requires question_types."
            )
        section_type_total = 0
        for allocation in allocations:
            type_name = allocation.get("type") if isinstance(allocation, dict) else None
            type_count = allocation.get("count") if isinstance(allocation, dict) else None
            if type_name not in type_names:
                raise TemplateValidationError(
                    f"{exam_id}/{paper_id}/{section_name} uses unknown type {type_name!r}."
                )
            if isinstance(type_count, bool) or not isinstance(type_count, int) or type_count < 0:
                raise TemplateValidationError(
                    f"{exam_id}/{paper_id}/{section_name} has an invalid count for {type_name}."
                )
            section_type_total += type_count
            global_type_counts[type_name] = global_type_counts.get(type_name, 0) + (
                type_count * len(targets)
            )
            for subject_name in targets:
                per_subject_types[subject_name][type_name] = (
                    per_subject_types[subject_name].get(type_name, 0) + type_count
                )
        if section_type_total != count:
            raise TemplateValidationError(
                f"{exam_id}/{paper_id}/{section_name} declares {count} questions "
                f"but question types total {section_type_total}."
            )

        scoring = section.get("scoring")
        if not isinstance(scoring, dict) or not isinstance(scoring.get("correct"), (int, float)):
            raise TemplateValidationError(
                f"{exam_id}/{paper_id}/{section_name} needs numeric scoring.correct."
            )
        for subject_name in targets:
            per_subject_questions[subject_name] += count
            per_subject_marks[subject_name] += count * float(scoring["correct"])

    for subject_name, expected_count in subject_totals.items():
        if per_subject_questions[subject_name] != expected_count:
            raise TemplateValidationError(
                f"{exam_id}/{paper_id}/{subject_name}: sections total "
                f"{per_subject_questions[subject_name]} questions, expected {expected_count}."
            )
    total_marks = sum(per_subject_marks.values())
    declared_marks = spec.get("total_marks")
    if not isinstance(declared_marks, (int, float)) or not math.isclose(
        total_marks, float(declared_marks), abs_tol=1e-8
    ):
        raise TemplateValidationError(
            f"{exam_id}/{paper_id}: section marks total {total_marks:g}, "
            f"declared total_marks is {declared_marks!r}."
        )

    declared_types = spec.get("question_types")
    if not isinstance(declared_types, dict) or declared_types.get("mode") != "exact":
        raise TemplateValidationError(
            f"{exam_id}/{paper_id} requires exact top-level question_types."
        )
    declared_type_counts: dict[str, int] = {}
    for allocation in declared_types.get("types", []):
        name = allocation.get("type")
        value = allocation.get("count")
        if name in declared_type_counts:
            raise TemplateValidationError(
                f"{exam_id}/{paper_id} declares question type {name!r} more than once."
            )
        declared_type_counts[name] = value
    if declared_type_counts != global_type_counts:
        raise TemplateValidationError(
            f"{exam_id}/{paper_id}: top-level question_types do not match section totals. "
            f"Expected {global_type_counts}, got {declared_type_counts}."
        )
    return per_subject_types, global_type_counts


def _normalized_weights(counts: dict[str, int]) -> list[dict[str, Any]]:
    total = sum(counts.values())
    if total < 1:
        raise TemplateValidationError("Cannot derive chapter-bank weights from zero questions.")
    type_names = sorted(counts)
    weights: list[dict[str, Any]] = []
    preceding_weight = 0.0
    for index, type_name in enumerate(type_names):
        if index == len(type_names) - 1:
            weight = round(1.0 - preceding_weight, 6)
        else:
            weight = round(counts[type_name] / total, 6)
            preceding_weight += weight
        weights.append({"type": type_name, "weight": weight})
    return weights


def _derived_templates(
    manifest: dict[str, Any],
    paper_specs: dict[tuple[str, str], dict[str, Any]],
    type_names: set[str],
) -> dict[Path, dict[str, Any]]:
    generated: dict[Path, dict[str, Any]] = {}
    target_questions = manifest["chapter_bank_target_questions"]
    for exam_id, exam in manifest["exams"].items():
        batch_spec = {
            "exam_name": exam["name"],
            "test_count": exam["test_count"],
            "uniqueness_scope": exam.get("uniqueness_scope", "all_papers"),
            "papers": [
                {"id": paper["id"], "spec": Path(paper["spec"]).name}
                for paper in exam["papers"]
            ],
        }
        generated[Path(exam_id) / "batch.template.yaml"] = batch_spec

        combined_counts: dict[str, dict[str, int]] = {}
        subject_syllabuses: dict[str, str] = {}
        for paper in exam["papers"]:
            spec = paper_specs[(exam_id, paper["id"])]
            by_subject, _ = _section_type_counts(
                spec, exam_id, paper["id"], type_names
            )
            for subject in spec["subjects"]:
                name = subject["name"]
                syllabus = subject["syllabus_file"]
                if name in subject_syllabuses and subject_syllabuses[name] != syllabus:
                    raise TemplateValidationError(
                        f"Exam '{exam_id}' has inconsistent syllabus paths for {name}."
                    )
                subject_syllabuses[name] = syllabus
                counts = combined_counts.setdefault(name, {})
                for type_name, count in by_subject[name].items():
                    counts[type_name] = counts.get(type_name, 0) + count

        for subject_name, counts in combined_counts.items():
            safe_subject = re.sub(
                r"[^a-z0-9]+", "_", subject_name.casefold()
            ).strip("_")
            bank_spec = {
                "exam_name": exam["name"],
                "paper_title": f"{exam['name']} {subject_name} Chapter Question Bank",
                "total_questions": target_questions,
                "question_types": {
                    "mode": "adaptive",
                    "types": _normalized_weights(counts),
                },
            }
            generated[
                Path(exam_id) / f"{safe_subject}_chapter_bank.template.yaml"
            ] = bank_spec
    return generated


def generate_exam_templates(
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    output_dir: Path = DEFAULT_TEMPLATE_DIR,
    force: bool = False,
) -> list[Path]:
    """Copy curated paper specs and generate derived batch and chapter-bank templates."""
    manifest_path = Path(manifest_path).expanduser().resolve()
    output_dir = Path(output_dir).expanduser().resolve()
    manifest, paper_specs, catalog = _load_definitions(manifest_path)
    derived = _derived_templates(manifest, paper_specs, catalog["type_names"])

    paper_sources: dict[Path, Path] = {}
    for exam_id, exam in manifest["exams"].items():
        for paper in exam["papers"]:
            relative_path = Path(paper["spec"])
            paper_sources[relative_path] = manifest_path.parent / relative_path
    targets = [output_dir / path for path in paper_sources]
    targets.extend(output_dir / path for path in derived)
    existing = [path for path in targets if path.exists()]
    if existing and not force:
        raise FileExistsError(
            "Template outputs already exist. Use --force to replace them: "
            + ", ".join(str(path) for path in existing[:5])
        )

    for relative_path, source_path in paper_sources.items():
        target_path = output_dir / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = target_path.with_suffix(target_path.suffix + ".tmp")
        temporary_path.write_bytes(source_path.read_bytes())
        temporary_path.replace(target_path)
    for relative_path, payload in derived.items():
        _write_yaml_atomically(output_dir / relative_path, payload)

    generated_paths = targets
    return generated_paths


def validate_exam_templates(
    *,
    manifest_path: Path = DEFAULT_MANIFEST_PATH,
    template_dir: Path = DEFAULT_TEMPLATE_DIR,
) -> list[str]:
    """Validate paper specs, source references, and derived template contents."""
    manifest_path = Path(manifest_path).expanduser().resolve()
    template_dir = Path(template_dir).expanduser().resolve()
    manifest, paper_specs, catalog = _load_definitions(manifest_path)
    expected_derived = _derived_templates(
        manifest,
        paper_specs,
        catalog["type_names"],
    )
    errors: list[str] = []

    for exam_id, exam in manifest["exams"].items():
        for paper in exam["papers"]:
            paper_id = paper["id"]
            spec = paper_specs[(exam_id, paper_id)]
            _section_type_counts(spec, exam_id, paper_id, catalog["type_names"])
            template_path = template_dir / paper["spec"]
            if not template_path.is_file():
                errors.append(f"Missing paper template: {template_path}")
                continue
            try:
                existing_spec = _read_yaml(template_path)
            except TemplateValidationError as exc:
                errors.append(str(exc))
                continue
            if existing_spec != spec:
                errors.append(
                    f"Paper template differs from its manifest source: {template_path}"
                )

    for relative_path, expected in expected_derived.items():
        template_path = template_dir / relative_path
        if not template_path.is_file():
            errors.append(f"Missing generated template: {template_path}")
            continue
        try:
            actual = _read_yaml(template_path)
        except TemplateValidationError as exc:
            errors.append(str(exc))
            continue
        if actual != expected:
            errors.append(
                f"Generated template differs from current definitions: {template_path}"
            )
        if "question_types" in actual:
            question_types = actual["question_types"]
            if isinstance(question_types, dict) and question_types.get("mode") == "adaptive":
                weights = [item["weight"] for item in question_types.get("types", [])]
                if not weights or not math.isclose(sum(weights), 1.0, abs_tol=1e-9):
                    errors.append(
                        f"Adaptive type weights must sum to 1.0: {template_path}"
                    )
    return errors


def format_validation_errors(errors: list[str]) -> str:
    """Render validation errors in a readable CLI format."""
    return "\n".join(f"- {error}" for error in errors)
