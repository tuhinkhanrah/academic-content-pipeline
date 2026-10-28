#!/usr/bin/env python3
"""Generate PDF tutorials for source PDFs/Markdown while mirroring their directory tree.

Example:
    python scripts/run_path_mirrored_tutorial.py \
        --source-root /data/chapters \
        --output-root /workspace/summaries \
        --languages english
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


def language_tokens(languages: str) -> List[str]:
    tokens = [
        re.sub(r"[^a-z0-9]+", "_", item.strip().lower()).strip("_")
        for item in languages.split(",")
    ]
    return list(dict.fromkeys(token for token in tokens if token)) or ["english"]


def output_prefix(input_path: Path, standard: str, languages: str) -> str:
    parts = [re.sub(r"[^a-zA-Z0-9._-]+", "_", input_path.stem).strip("._-") or "output"]
    standard_token = re.sub(r"[^a-z0-9]+", "_", standard.strip().lower()).strip("_")
    if standard_token and standard_token not in {"general", "mock_exam"}:
        parts.append(standard_token)
    parts.extend(language_tokens(languages))
    return "_".join(parts)


def find_existing_tutorial(output_dir: Path, prefix: str) -> Path | None:
    """Find a non-empty tutorial PDF for this input, standard, and language set."""
    candidates = sorted(
        (
            path
            for path in output_dir.iterdir()
            if path.is_file()
            and path.suffix.lower() == ".pdf"
            and path.name.startswith(f"{prefix}_")
            and path.stat().st_size > 0
        ),
        key=lambda path: path.name,
        reverse=True,
    )
    return candidates[0] if candidates else None


def command_for_input(
    input_path: Path,
    output_dir: Path,
    mode: str,
    languages: str,
    standard: str,
    tags: List[str],
    model_name: str | None = None,
    ocr_model: str | None = None,
    pdf_engine: str = "html",
    page_range: List[int] | None = None,
    watermark_text: str = "",
    header_text: str = "",
    footer_text: str = "",
    bucket_name: str | None = None,
    disable_ocr_cache: bool = False,
) -> List[str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "academic_content_pipeline.unified_pipeline",
        mode,
        "generate-tutorial",
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
        "--pdf-engine",
        pdf_engine,
    ]
    if model_name:
        command.extend(["--model-name", model_name])
    if ocr_model:
        command.extend(["--ocr-model", ocr_model])
    if page_range:
        command.extend(["--page-range", str(page_range[0]), str(page_range[1])])
    if watermark_text:
        command.extend(["--watermark-text", watermark_text])
    if header_text:
        command.extend(["--header-text", header_text])
    if footer_text:
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
        description="Generate PDF summaries while mirroring source folders."
    )
    parser.add_argument("--source-root", type=Path, required=True, help="Root containing PDF/Markdown sources.")
    parser.add_argument("--output-root", type=Path, required=True, help="Root for mirrored tutorial PDFs.")
    parser.add_argument(
        "--mode",
        choices=["context", "agent", "remote", "batch", "sequential"],
        default="context",
        help="AI communication mode.",
    )
    parser.add_argument("--languages", default="english", help="Comma-separated target languages.")
    parser.add_argument("--model-name", default=None, help="Gemini model override.")
    parser.add_argument(
        "--ocr-model",
        default=os.environ.get("MISTRAL_OCR_MODEL", "mistral-ocr-latest"),
        help="Mistral OCR model for PDF inputs.",
    )
    parser.add_argument("--pdf-engine", choices=["html", "tex"], default="html", help="PDF rendering engine.")
    parser.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"), help="Optional 1-based PDF page range.")
    parser.add_argument("--parallel-workers", type=int, default=1, help="Maximum concurrent source files; use 1 to limit OCR load.")
    parser.add_argument("--bucket-name", default=None, help="GCS bucket for remote mode.")
    parser.add_argument("--watermark-text", default="", help="Optional watermark on generated PDFs.")
    parser.add_argument("--header-text", default="", help="Optional repeating header on HTML-rendered PDFs.")
    parser.add_argument("--footer-text", default="", help="Optional repeating footer on HTML-rendered PDFs.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    parser.add_argument("--force", action="store_true", help="Regenerate even if a matching tutorial PDF exists.")
    parser.add_argument("--disable-ocr-cache", action="store_true", help="Force fresh Mistral OCR for PDF inputs.")
    parser.add_argument("--verbose", action="store_true", help="Print per-file inferred metadata.")
    args = parser.parse_args(argv)

    if args.mode == "sequential":
        args.mode = "context"
    max_workers = max(1, args.parallel_workers)
    source_root = args.source_root.resolve()
    output_root = args.output_root.resolve()
    if not source_root.is_dir():
        raise NotADirectoryError(f"Source root does not exist or is not a directory: {source_root}")
    output_root.mkdir(parents=True, exist_ok=True)

    skipped_files: List[tuple[Path, Path]] = []
    jobs: List[tuple[Path, List[str]]] = []
    source_files = list(iter_source_files(source_root))
    for input_path in source_files:
        output_dir = build_output_dir(source_root, input_path, output_root)
        standard, tags = infer_metadata_from_path(input_path)
        if args.verbose:
            print(f"{input_path.relative_to(source_root)} -> standard={standard}, tags={tags}")
        prefix = output_prefix(input_path, standard, args.languages)
        existing_tutorial = find_existing_tutorial(output_dir, prefix) if output_dir.exists() else None
        if existing_tutorial is not None and not args.force:
            print(f"Skipping {input_path}: tutorial already exists at {existing_tutorial}")
            skipped_files.append((input_path, existing_tutorial))
            continue

        command = command_for_input(
            input_path=input_path,
            output_dir=output_dir,
            mode=args.mode,
            languages=args.languages,
            standard=standard,
            tags=tags,
            model_name=args.model_name,
            ocr_model=args.ocr_model if input_path.suffix.lower() == ".pdf" else None,
            pdf_engine=args.pdf_engine,
            page_range=args.page_range if input_path.suffix.lower() == ".pdf" else None,
            watermark_text=args.watermark_text,
            header_text=args.header_text,
            footer_text=args.footer_text,
            bucket_name=args.bucket_name,
            disable_ocr_cache=args.disable_ocr_cache,
        )
        if args.dry_run:
            print(subprocess.list2cmdline(command))
        else:
            jobs.append((input_path, command))

    if args.dry_run:
        print(f"Prepared {len(source_files)} source file(s); skipped {len(skipped_files)} existing tutorial file(s).")
        return 0
    if not jobs:
        print(f"No tutorials to generate. Skipped {len(skipped_files)} existing tutorial file(s).")
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
        f"Generated tutorials for {len(jobs) - failures} file(s); "
        f"skipped {len(skipped_files)} existing tutorial file(s); "
        f"failed {failures} file(s)."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
