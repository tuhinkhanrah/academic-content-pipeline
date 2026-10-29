#!/usr/bin/env python3
"""Generate practice questions from PDFs/Markdown while mirroring source folders.

Example:
    python scripts/run_path_mirrored_question_generation.py \
        --source-root /data/chapters \
        --output-root /workspace/question-output \
        --mode batch \
        --standards neet_ug
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
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
        "neet": "neet",
        "neet ug": "neet",
        "neet_ug": "neet",
        "wbjee": "wbjee",
        "wb-jee": "wbjee",
    }
    return replacements.get(value, re.sub(r"[^a-z0-9]+", "_", value).strip("_"))


def infer_metadata_from_path(input_path: Path) -> tuple[str, List[str]]:
    """Infer exam standard and year tags from the input's relative path."""
    path_parts = [part for part in input_path.parts if part not in (".", "")]
    path_text = input_path.as_posix().lower()
    standard_aliases = {
        "jee_main": ("jee main", "jee-main", "jee_main"),
        "jee_advanced": ("jee advanced", "jee_advanced"),
        "neet": ("neet", "neet ug", "neet_ug"),
        "wbjee": ("wbjee", "wb-jee", "wb_jee"),
    }

    standard = "general"
    for canonical, aliases in standard_aliases.items():
        if any(alias in path_text for alias in aliases):
            standard = canonical
            break

    if standard == "general":
        for part in path_parts:
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
    parser.add_argument("--spec", type=Path, default=None, help="Optional YAML/JSON paper specification.")
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
    max_workers = max(1, args.parallel_workers)
    source_root = args.source_root.resolve()
    output_root = args.output_root.resolve()
    if not source_root.is_dir():
        raise NotADirectoryError(f"Source root does not exist or is not a directory: {source_root}")
    spec_path = args.spec.resolve() if args.spec else None
    spec_exam_name = load_spec_exam_name(spec_path)
    output_root.mkdir(parents=True, exist_ok=True)

    source_files = list(iter_source_files(source_root))
    skipped_files: List[tuple[Path, Path]] = []
    jobs: List[tuple[Path, List[str]]] = []
    for input_path in source_files:
        standard, tags = infer_metadata_from_path(input_path.relative_to(source_root))
        if args.standards:
            standard = normalize_standard(args.standards)
        output_dir = build_output_dir(source_root, input_path, output_root)
        prefix = build_output_prefix(input_path, standard, args.languages, spec_exam_name)
        existing_output = (
            find_existing_output(output_dir, prefix, args.output_format)
            if output_dir.exists()
            else None
        )
        if existing_output is not None and not args.force:
            print(f"Skipping {input_path}: matching output already exists at {existing_output}")
            skipped_files.append((input_path, existing_output))
            continue
        if args.verbose:
            print(f"{input_path.relative_to(source_root)} -> standard={standard}, tags={tags}")

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
            spec_path=spec_path,
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
