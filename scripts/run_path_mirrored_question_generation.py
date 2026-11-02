#!/usr/bin/env python3
"""Generate practice questions from PDFs/Markdown while mirroring source folders.

Example:
    python scripts/run_path_mirrored_question_generation.py \
        --source-root /data/chapters \
        --output-root /workspace/question-output \
        --mode batch \
        --standards neet_ug

    Add --spec with question_types.mode: exact or adaptive, then pass
    --separate-by-question-type to save one Moodle XML file per type.
    Without --spec, matching subject-specific templates are selected from
    the inferred exam and subject folders under resources/spec_templates.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, List

import yaml


def normalize_standard(value: str) -> str:
    value = value.strip().lower()
    replacements = {
        "jee_main": "jee_main",
        "jee main": "jee_main",
        "jee-main": "jee_main",
        "jee advanced": "jee_advanced",
        "jee_advanced": "jee_advanced",
        "jee-advanced": "jee_advanced",
        "neet": "neet_ug",
        "neet-ug": "neet_ug",
        "neet ug": "neet_ug",
        "neet_ug": "neet_ug",
        "wbjee": "wbjee",
        "wb-jee": "wbjee",
    }
    return replacements.get(value, re.sub(r"[^a-z0-9]+", "_", value).strip("_"))


def infer_metadata_from_path(
    input_path: Path,
    context_path: Path | None = None,
) -> tuple[str, List[str]]:
    """Infer exam standard and year tags from the input's relative path."""
    path_parts = [part for part in input_path.parts if part not in (".", "")]
    context_parts = (
        [part for part in context_path.parts if part not in (".", "")]
        if context_path is not None
        else []
    )
    inference_parts = context_parts + path_parts
    path_text = "/".join(inference_parts).lower()
    standard_aliases = {
        "jee_main": ("jee main", "jee-main", "jee_main"),
        "jee_advanced": ("jee advanced", "jee-advanced", "jee_advanced"),
        "neet_ug": ("neet", "neet ug", "neet_ug", "neet-ug"),
        "wbjee": ("wbjee", "wb-jee", "wb_jee"),
    }

    standard = "general"
    for canonical, aliases in standard_aliases.items():
        if any(alias in path_text for alias in aliases):
            standard = canonical
            break

    if standard == "general":
        for part in inference_parts:
            normalized = normalize_standard(part)
            if normalized not in {"", "general", "2026", "21_06_2026"} and any(
                token in part.lower() for token in ("neet", "jee", "wbjee")
            ):
                standard = normalized
                break

    years = list(dict.fromkeys(re.findall(r"(?<!\d)\d{4}(?!\d)", input_path.as_posix())))
    return standard, [f"year:{year}" for year in years]


def iter_source_files(source_root: Path) -> Iterable[Path]:
    for path in sorted(source_root.rglob("*")):
        if (
            path.is_file()
            and path.suffix.lower() in {".pdf", ".md"}
            and not path.name.startswith(("sliced_", "temp_"))
        ):
            yield path


def build_output_dir(source_root: Path, input_path: Path, output_root: Path) -> Path:
    return output_root / input_path.relative_to(source_root).parent


def normalize_name_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def language_tokens(languages: str) -> List[str]:
    tokens = [normalize_name_token(language) for language in languages.split(",")]
    return list(dict.fromkeys(token for token in tokens if token)) or ["english"]


def load_spec_exam_name(spec_path: Path | None) -> str | None:
    if spec_path is None:
        return None
    if not spec_path.is_file():
        raise FileNotFoundError(f"Question-generation spec does not exist: {spec_path}")
    if spec_path.suffix.lower() not in {".yaml", ".yml", ".json"}:
        raise ValueError("--spec must point to a YAML or JSON paper spec.")
    with spec_path.open("r", encoding="utf-8") as spec_file:
        if spec_path.suffix.lower() == ".json":
            import json

            spec_data = json.load(spec_file)
        else:
            spec_data = yaml.safe_load(spec_file) or {}
    if not isinstance(spec_data, dict):
        raise ValueError(f"Question-generation spec must contain a mapping: {spec_path}")
    exam_name = str(spec_data.get("exam_name", "")).strip()
    return exam_name or None


def infer_subject_from_path(input_path: Path, source_root: Path) -> str | None:
    """Infer a unique subject from exact path components."""
    relative_parts = input_path.relative_to(source_root).parts
    path_parts = (*source_root.parts, *relative_parts)
    aliases = {
        "physics": {"physics"},
        "chemistry": {"chemistry"},
        "biology": {"biology", "botany", "zoology"},
        "mathematics": {"mathematics", "maths", "math"},
    }
    matches = {
        subject
        for part in path_parts
        for subject, names in aliases.items()
        if normalize_name_token(part) in names
    }
    return next(iter(matches)) if len(matches) == 1 else None


def resolve_default_subject_spec(
    standard: str,
    subject: str | None,
) -> Path | None:
    """Find a subject-specific chapter-bank template for the inferred exam."""
    if subject is None:
        return None

    template_root = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "academic_content_pipeline"
        / "resources"
        / "spec_templates"
    )
    exam_directory = normalize_standard(standard)
    exam_templates = template_root / exam_directory
    candidate_names = (
        f"{subject}_chapter_bank.template.yaml",
        f"{exam_directory}_{subject}_chapter_bank.template.yaml",
        f"{subject}.template.yaml",
    )
    for candidate_name in candidate_names:
        candidate = exam_templates / candidate_name
        if candidate.is_file():
            return candidate.resolve()
    return None


def load_separate_question_type_suffixes(spec_path: Path | None) -> List[str]:
    """Return filename suffixes for positive exact question-type allocations."""
    if spec_path is None:
        raise ValueError("--separate-by-question-type requires --spec.")
    if not spec_path.is_file():
        raise FileNotFoundError(f"Question-generation spec does not exist: {spec_path}")
    with spec_path.open("r", encoding="utf-8") as spec_file:
        if spec_path.suffix.lower() == ".json":
            import json

            spec_data = json.load(spec_file)
        else:
            spec_data = yaml.safe_load(spec_file) or {}
    if not isinstance(spec_data, dict):
        raise ValueError(f"Question-generation spec must contain a mapping: {spec_path}")
    config = spec_data.get("question_types")
    if isinstance(config, list):
        config = {"mode": "exact", "types": config}
    mode = config.get("mode", "automatic") if isinstance(config, dict) else None
    if not isinstance(mode, str) or mode.casefold() != "exact":
        raise ValueError(
            "--separate-by-question-type requires question_types.mode: exact "
            "with explicit per-type counts in the selected spec."
        )
    allocations = config.get("types")
    if not isinstance(allocations, list) or not allocations:
        raise ValueError(
            "Exact question_types must contain a non-empty list of {type, count} entries."
        )

    repository_root = Path(__file__).resolve().parents[1]
    catalog_path = (
        repository_root
        / "src"
        / "academic_content_pipeline"
        / "resources"
        / "catalogs"
        / "mcq_types.yaml"
    )
    catalog_data = yaml.safe_load(catalog_path.read_text(encoding="utf-8")) or {}
    entries = catalog_data.get("mcq_types", [])
    by_key = {entry["id"]: entry["name"] for entry in entries}
    by_key.update({entry["name"]: entry["name"] for entry in entries})

    suffixes = []
    seen = set()
    for allocation in allocations:
        if not isinstance(allocation, dict) or "type" not in allocation:
            raise ValueError("Each question-type allocation must include 'type' and 'count'.")
        count = allocation.get("count")
        if isinstance(count, bool) or not isinstance(count, int):
            raise ValueError("Each question-type allocation needs an integer count.")
        if count < 0:
            raise ValueError("Question-type counts cannot be negative.")
        if count == 0:
            continue
        type_key = allocation["type"]
        if not isinstance(type_key, str):
            raise ValueError("Question type names and IDs must be strings.")
        if type_key not in by_key:
            raise ValueError(f"Unknown MCQ type in spec: {type_key}")
        suffix = normalize_name_token(by_key[type_key])
        if suffix in seen:
            raise ValueError(f"Question type is allocated more than once: {type_key}")
        seen.add(suffix)
        suffixes.append(suffix)
    if not suffixes:
        raise ValueError("At least one question type must have a positive count.")
    return suffixes


def load_question_type_mode(spec_path: Path | None) -> str:
    """Return the configured question-type mode, defaulting to automatic."""
    if spec_path is None:
        return "automatic"
    if not spec_path.is_file():
        raise FileNotFoundError(f"Question-generation spec does not exist: {spec_path}")
    raw = spec_path.read_text(encoding="utf-8")
    spec_data = (
        json.loads(raw)
        if spec_path.suffix.lower() == ".json"
        else yaml.safe_load(raw) or {}
    )
    if not isinstance(spec_data, dict):
        raise ValueError(f"Question-generation spec must contain a mapping: {spec_path}")
    config = spec_data.get("question_types")
    if isinstance(config, list):
        return "exact"
    if isinstance(config, dict):
        mode = config.get("mode", "automatic")
        return mode.casefold() if isinstance(mode, str) else ""
    return "automatic"


def has_valid_adaptive_output_manifest(
    output_dir: Path,
    manifest_prefix: str,
    required_prefixes: List[str],
    spec_path: Path,
    languages: str,
    translate_to: List[str],
) -> bool:
    """Check that an adaptive manifest matches this request and all XML outputs."""
    manifest_path = output_dir / f"{manifest_prefix}_question_types.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(manifest, dict) or manifest.get("status") != "completed":
        return False

    expected_languages = [
        language.strip().casefold()
        for language in languages.split(",")
        if language.strip()
    ] or ["english"]
    expected_translations = [language.strip().casefold() for language in translate_to]
    if (
        manifest.get("spec_sha256")
        != hashlib.sha256(spec_path.read_bytes()).hexdigest()
        or manifest.get("languages") != expected_languages
        or manifest.get("translate_to") != expected_translations
    ):
        return False

    allocations = manifest.get("question_types")
    if not isinstance(allocations, list) or not allocations:
        return False
    counts_by_suffix: dict[str, int] = {}
    for allocation in allocations:
        if not isinstance(allocation, dict):
            return False
        type_name = allocation.get("type")
        count = allocation.get("count")
        if (
            not isinstance(type_name, str)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
        ):
            return False
        suffix = normalize_name_token(type_name)
        if not suffix or suffix in counts_by_suffix:
            return False
        counts_by_suffix[suffix] = count

    expected_counts = {
        f"{prefix}_{suffix}.xml": count
        for prefix in required_prefixes
        for suffix, count in counts_by_suffix.items()
    }
    files = manifest.get("files")
    completed_files = manifest.get("completed_files")
    if (
        not isinstance(files, list)
        or any(not isinstance(name, str) or Path(name).name != name for name in files)
        or len(files) != len(expected_counts)
        or set(files) != set(expected_counts)
        or completed_files != files
        or manifest.get("allocated_questions")
        != sum(counts_by_suffix.values())
    ):
        return False

    for filename, expected_count in expected_counts.items():
        output_path = output_dir / filename
        try:
            root = ET.parse(output_path).getroot()
        except (OSError, ET.ParseError):
            return False
        actual_count = sum(
            node.tag == "question" and node.get("type") != "category"
            for node in root
        )
        if root.tag != "quiz" or actual_count != expected_count:
            return False
    return True


def resolve_instruction_file(
    standard: str,
    override: Path | None = None,
) -> Path | None:
    """Use an explicit instruction file or a matching built-in exam profile."""
    if override is not None:
        instruction_file = override.expanduser().resolve()
        if not instruction_file.is_file():
            raise FileNotFoundError(
                f"Instruction file does not exist: {instruction_file}"
            )
        return instruction_file

    profile_path = (
        Path(__file__).resolve().parents[1]
        / "prompts"
        / "instructions"
        / f"{standard}.md"
    )
    return profile_path if profile_path.is_file() else None


def build_output_prefix(
    input_path: Path,
    standard: str,
    languages: str,
    spec_exam_name: str | None = None,
) -> str:
    source_stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", input_path.stem).strip("._-") or "output"
    parts = [source_stem]
    qualifier = spec_exam_name or (standard if standard.lower() != "general" else "")
    qualifier_token = re.sub(r"[^a-zA-Z0-9._-]+", "_", qualifier).strip("._-").lower()
    if qualifier_token in {"neet", "neet-ug", "neet_ug"}:
        qualifier_token = "neet_ug"
    if qualifier_token and qualifier_token != "mock_exam":
        parts.append(qualifier_token)
    parts.extend(language_tokens(languages))
    return "_".join(parts)


def find_existing_output(output_dir: Path, prefix: str, output_format: str) -> Path | None:
    """Find a non-empty stable question output, including split papers."""
    extension = "pdf" if output_format == "pdf" else "xml"
    stable_output = output_dir / f"{prefix}.{extension}"
    if stable_output.is_file() and stable_output.stat().st_size > 0:
        return stable_output
    split_outputs = sorted(output_dir.glob(f"{prefix}_paper_*.{extension}"), reverse=True)
    return next(
        (path for path in split_outputs if path.is_file() and path.stat().st_size > 0),
        None,
    )


def has_output_set(
    output_dir: Path,
    prefix: str,
    output_format: str,
    num_papers: int,
) -> bool:
    if output_format == "xml" and num_papers > 1:
        return all(
            (output_dir / f"{prefix}_paper_{index:02d}.xml").is_file()
            and (output_dir / f"{prefix}_paper_{index:02d}.xml").stat().st_size > 0
            for index in range(1, num_papers + 1)
        )
    return find_existing_output(output_dir, prefix, output_format) is not None


def command_for_input(
    input_path: Path,
    output_dir: Path,
    mode: str,
    languages: str,
    standard: str,
    tags: List[str],
    *,
    model_name: str | None,
    ocr_model: str | None,
    output_format: str,
    pdf_engine: str,
    num_questions: int,
    num_papers: int,
    difficulty_mix: str,
    page_range: List[int] | None,
    exam_duration_minutes: int | None,
    spec_path: Path | None,
    instruction_file: Path | None,
    separate_by_question_type: bool,
    translate_to: str,
    force: bool,
    watermark_text: str,
    header_text: str,
    footer_text: str,
    disable_ocr_cache: bool,
    bucket_name: str | None,
) -> List[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "academic_content_pipeline.unified_pipeline",
        mode,
        "generate-questions",
        "--input-file",
        str(input_path),
        "--output-dir",
        str(output_dir),
        "--standards",
        standard,
        "--tags",
        ",".join(tags),
        "--languages",
        languages,
        "--output-format",
        output_format,
        "--pdf-engine",
        pdf_engine,
        "--num-questions",
        str(num_questions),
        "--num-papers",
        str(num_papers),
        "--difficulty-mix",
        difficulty_mix,
    ]
    if model_name:
        command.extend(["--model-name", model_name])
    if ocr_model and input_path.suffix.lower() == ".pdf":
        command.extend(["--ocr-model", ocr_model])
    if page_range and input_path.suffix.lower() == ".pdf":
        command.extend(["--page-range", str(page_range[0]), str(page_range[1])])
    if exam_duration_minutes is not None:
        command.extend(["--exam-duration-minutes", str(exam_duration_minutes)])
    if spec_path is not None:
        command.extend(["--spec", str(spec_path)])
    if instruction_file is not None:
        command.extend(["--instruction-file", str(instruction_file)])
    if separate_by_question_type:
        command.append("--separate-by-question-type")
    if translate_to:
        command.extend(["--translate-to", translate_to])
    if force:
        command.append("--force")
    if watermark_text and output_format == "pdf":
        command.extend(["--watermark-text", watermark_text])
    if header_text and output_format == "pdf":
        command.extend(["--header-text", header_text])
    if footer_text and output_format == "pdf":
        command.extend(["--footer-text", footer_text])
    if disable_ocr_cache and input_path.suffix.lower() == ".pdf":
        command.append("--disable-ocr-cache")
    if mode == "remote":
        bucket_name = bucket_name or os.environ.get("GCS_BUCKET_NAME")
        if bucket_name:
            command.extend(["--bucket-name", bucket_name])
    return command


def execute_command(command: List[str], input_path: Path) -> int:
    environment = os.environ.copy()
    source_root = Path(__file__).resolve().parents[1]
    source_path = str(source_root / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not existing_pythonpath
        else source_path + os.pathsep + existing_pythonpath
    )
    result = subprocess.run(command, env=environment, check=False)
    if result.returncode != 0:
        print(f"Failed for {input_path}: exit code {result.returncode}", file=sys.stderr)
    return result.returncode


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate chapter questions while mirroring source folders."
    )
    parser.add_argument("--source-root", type=Path, required=True, help="Root containing PDF/Markdown sources.")
    parser.add_argument("--output-root", type=Path, required=True, help="Root for mirrored question outputs.")
    parser.add_argument(
        "--mode",
        choices=["context", "agent", "remote", "batch", "sequential"],
        default="context",
        help="AI communication mode.",
    )
    parser.add_argument("--languages", default="english", help="Comma-separated target languages.")
    parser.add_argument("--standards", default=None, help="Override inferred exam standard.")
    parser.add_argument("--model-name", default=None, help="Gemini model override.")
    parser.add_argument("--ocr-model", default=os.environ.get("MISTRAL_OCR_MODEL", "mistral-ocr-latest"), help="Mistral OCR model for PDF inputs.")
    parser.add_argument("--output-format", choices=["xml", "pdf"], default="xml", help="Question output format.")
    parser.add_argument("--pdf-engine", choices=["html", "tex"], default="html", help="PDF rendering engine.")
    parser.add_argument("--num-questions", type=int, default=5, help="Questions to generate per source file.")
    parser.add_argument("--num-papers", type=int, default=1, help="Split XML questions across this many papers.")
    parser.add_argument("--difficulty-mix", default="easy:0.2,medium:0.5,hard:0.3", help="Difficulty ratios.")
    parser.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"), help="Optional 1-based PDF page range.")
    parser.add_argument("--exam-duration-minutes", type=int, default=None, help="Optional exam duration for PDF output.")
    parser.add_argument(
        "--spec",
        type=Path,
        default=None,
        help="Explicit YAML/JSON spec override; otherwise select a matching subject template automatically.",
    )
    parser.add_argument(
        "--instruction-file",
        type=Path,
        default=None,
        help="Override the instruction profile; otherwise use a matching built-in exam profile when available.",
    )
    parser.add_argument(
        "--separate-by-question-type",
        action="store_true",
        help="Generate each exact or adaptively planned question type into its own Moodle XML file.",
    )
    parser.add_argument(
        "--translate-to",
        default="",
        help="Create additional English-master bilingual output(s) for comma-separated languages.",
    )
    parser.add_argument("--parallel-workers", type=int, default=1, help="Maximum concurrent files; use 1 to limit OCR/API load.")
    parser.add_argument("--bucket-name", default=None, help="GCS bucket for remote mode.")
    parser.add_argument("--watermark-text", default="", help="Optional PDF watermark.")
    parser.add_argument("--header-text", default="", help="Optional repeating header on HTML-rendered PDFs.")
    parser.add_argument("--footer-text", default="", help="Optional repeating footer on HTML-rendered PDFs.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    parser.add_argument("--force", action="store_true", help="Generate even if a matching output already exists.")
    parser.add_argument("--disable-ocr-cache", action="store_true", help="Force fresh Mistral OCR for PDF inputs.")
    parser.add_argument("--verbose", action="store_true", help="Print per-file inferred metadata.")
    args = parser.parse_args(argv)

    if args.mode == "sequential":
        args.mode = "context"
    if args.num_questions < 1:
        parser.error("--num-questions must be at least 1.")
    if args.num_papers < 1:
        parser.error("--num-papers must be at least 1.")
    if args.num_papers > 1 and args.output_format != "xml":
        parser.error("--num-papers greater than 1 requires --output-format xml.")
    if args.separate_by_question_type:
        if args.output_format != "xml":
            parser.error("--separate-by-question-type requires --output-format xml.")
        if args.num_papers != 1:
            parser.error("--separate-by-question-type cannot be combined with --num-papers.")
    translate_targets = list(
        dict.fromkeys(
            language.strip()
            for language in args.translate_to.split(",")
            if language.strip()
        )
    )
    configured_languages = {
        language.strip().casefold()
        for language in args.languages.split(",")
        if language.strip()
    }
    if any(language.casefold() == "english" for language in translate_targets):
        parser.error("--translate-to accepts non-English language names only.")
    conflicting_targets = [
        language
        for language in translate_targets
        if language.casefold() in configured_languages
        and language.casefold() != "english"
    ]
    if conflicting_targets:
        parser.error(
            "Do not request the same non-English language in both --languages "
            f"and --translate-to: {', '.join(conflicting_targets)}."
        )
    max_workers = max(1, args.parallel_workers)
    source_root = args.source_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    if not source_root.is_dir():
        raise NotADirectoryError(f"Source root does not exist or is not a directory: {source_root}")
    explicit_spec_path = args.spec.expanduser().resolve() if args.spec else None

    source_files = list(iter_source_files(source_root))
    if not source_files:
        parser.error(
            f"No PDF or Markdown source files found under: {source_root}"
        )
    output_root.mkdir(parents=True, exist_ok=True)

    skipped_files: List[tuple[Path, Path]] = []
    jobs: List[tuple[Path, List[str]]] = []
    missing_profile_warnings: set[tuple[str, str | None]] = set()
    for input_path in source_files:
        standard, tags = infer_metadata_from_path(
            input_path.relative_to(source_root),
            context_path=source_root,
        )
        if args.standards:
            standard = normalize_standard(args.standards)
        subject = infer_subject_from_path(input_path, source_root)
        selected_spec_path = explicit_spec_path or resolve_default_subject_spec(
            standard,
            subject,
        )
        if args.separate_by_question_type and selected_spec_path is None:
            raise ValueError(
                "Cannot generate separate question-type outputs for "
                f"{input_path}: no --spec was provided and no default "
                f"subject template was found for exam '{standard}' and "
                f"subject '{subject or 'unknown'}'."
            )
        if selected_spec_path is None:
            missing_profile_key = (standard, subject)
            if missing_profile_key not in missing_profile_warnings:
                print(
                    "Warning: no default subject template found for "
                    f"exam '{standard}' and subject '{subject or 'unknown'}'; "
                    "continuing without a spec.",
                    file=sys.stderr,
                )
                missing_profile_warnings.add(missing_profile_key)
        selected_spec_exam_name = load_spec_exam_name(selected_spec_path)
        adaptive_question_types = (
            args.separate_by_question_type
            and load_question_type_mode(selected_spec_path) == "adaptive"
        )
        question_type_suffixes = (
            load_separate_question_type_suffixes(selected_spec_path)
            if args.separate_by_question_type and not adaptive_question_types
            else []
        )
        instruction_file = resolve_instruction_file(
            standard,
            override=args.instruction_file,
        )
        output_dir = build_output_dir(source_root, input_path, output_root)
        prefix = build_output_prefix(
            input_path,
            standard,
            args.languages,
            selected_spec_exam_name,
        )
        required_prefixes = [prefix]
        if translate_targets:
            if [language.strip().lower() for language in args.languages.split(",") if language.strip()] != ["english"]:
                required_prefixes.append(
                    build_output_prefix(
                        input_path,
                        standard,
                        "english",
                        selected_spec_exam_name,
                    )
                )
            required_prefixes.extend(
                build_output_prefix(
                    input_path,
                    standard,
                    f"english,{target_language}",
                    selected_spec_exam_name,
                )
                for target_language in translate_targets
            )
        if args.separate_by_question_type and not adaptive_question_types:
            required_prefixes = [
                f"{required_prefix}_{type_suffix}"
                for required_prefix in required_prefixes
                for type_suffix in question_type_suffixes
            ]
        if adaptive_question_types:
            has_adaptive_outputs = (
                output_dir.exists()
                and selected_spec_path is not None
                and has_valid_adaptive_output_manifest(
                    output_dir,
                    required_prefixes[0],
                    required_prefixes,
                    selected_spec_path,
                    args.languages,
                    translate_targets,
                )
            )
            existing_outputs = [has_adaptive_outputs]
        else:
            existing_outputs = (
                [
                    has_output_set(
                        output_dir,
                        required_prefix,
                        args.output_format,
                        args.num_papers,
                    )
                    for required_prefix in dict.fromkeys(required_prefixes)
                ]
                if output_dir.exists()
                else []
            )
        if existing_outputs and all(existing_outputs) and not args.force:
            print(f"Skipping {input_path}: all requested outputs already exist.")
            first_suffix = (
                "_paper_01.xml"
                if args.output_format == "xml" and args.num_papers > 1
                else f".{args.output_format}"
            )
            first_existing = (
                output_dir / f"{required_prefixes[0]}_question_types.json"
                if adaptive_question_types
                else output_dir / f"{required_prefixes[0]}{first_suffix}"
            )
            skipped_files.append((input_path, first_existing))
            continue
        if args.verbose:
            print(
                f"{input_path.relative_to(source_root)} -> "
                f"standard={standard}, subject={subject or 'unknown'}, "
                f"spec={selected_spec_path or 'none'}, tags={tags}"
            )

        command = command_for_input(
            input_path,
            output_dir,
            args.mode,
            args.languages,
            standard,
            tags,
            model_name=args.model_name,
            ocr_model=args.ocr_model,
            output_format=args.output_format,
            pdf_engine=args.pdf_engine,
            num_questions=args.num_questions,
            num_papers=args.num_papers,
            difficulty_mix=args.difficulty_mix,
            page_range=args.page_range,
            exam_duration_minutes=args.exam_duration_minutes,
            spec_path=selected_spec_path,
            instruction_file=instruction_file,
            separate_by_question_type=args.separate_by_question_type,
            translate_to=args.translate_to,
            force=args.force,
            watermark_text=args.watermark_text,
            header_text=args.header_text,
            footer_text=args.footer_text,
            disable_ocr_cache=args.disable_ocr_cache,
            bucket_name=args.bucket_name,
        )
        if args.dry_run:
            print(subprocess.list2cmdline(command))
        else:
            jobs.append((input_path, command))

    if args.dry_run:
        print(f"Prepared {len(source_files)} source file(s); skipped {len(skipped_files)} existing output(s).")
        return 0
    if not jobs:
        print(f"No questions to generate. Skipped {len(skipped_files)} existing output(s).")
        return 0

    failures = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_path = {
            executor.submit(execute_command, command, input_path): input_path
            for input_path, command in jobs
        }
        for future in as_completed(future_to_path):
            input_path = future_to_path[future]
            try:
                result_code = future.result()
            except OSError as exc:
                print(f"Failed for {input_path}: {exc}", file=sys.stderr)
                failures += 1
                continue
            if result_code != 0:
                failures += 1

    print(
        f"Generated outputs for {len(jobs) - failures} file(s); "
        f"skipped {len(skipped_files)} existing output(s); "
        f"failed {failures} file(s)."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
