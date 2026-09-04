#!/usr/bin/env python3
"""Mirror a source PDF vault into an output structure and infer metadata from each path.

This wrapper walks every PDF in a source directory, derives the standard and tags
from the relative path layout, and calls the extraction CLI for each file while
preserving the same folder structure under the output root.

Example:
    python scripts/run_path_mirrored_extraction.py \
        --source-root /data/pdfs \
        --output-root /workspace/output \
        --mode batch

The inferred metadata may look like:
    standards = NEET
    tags = year:2026
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


def infer_metadata_from_path(pdf_path: Path, source_root: Path) -> tuple[str, List[str]]:
    """Infer standard and year tags from the full PDF path, not just the first folder segment."""
    path_parts = [part for part in pdf_path.parts if part not in (".", "")]
    path_text = pdf_path.as_posix().lower()

    standard = "general"
    standard_aliases = {
        "jee_main": ["jee main", "jee-main", "jee_main"],
        "jee_advanced": ["jee advanced", "jee_advanced"],
        "neet": ["neet", "neet ug", "neet_ug"],
        "wbjee": ["wbjee", "wb-jee", "wb_jee"],
    }
    for canonical, aliases in standard_aliases.items():
        if any(alias in path_text for alias in aliases):
            standard = canonical
            break

    if standard == "general":
        for part in path_parts:
            normalized = normalize_standard(part)
            if normalized and normalized not in {"general", "2026", "21_06_2026"} and any(token in part.lower() for token in ("neet", "jee", "wbjee")):
                standard = normalized
                break

    years = []
    for match in re.finditer(r"(?<!\d)(\d{4})(?!\d)", pdf_path.as_posix()):
        year = match.group(1)
        if year not in years:
            years.append(year)
    tags = [f"year:{year}" for year in years]

    return standard, tags


def should_skip(output_xml: Path, force: bool = False) -> bool:
    if force:
        return False
    return output_xml.exists() and output_xml.stat().st_size > 0


def iter_pdfs(source_root: Path) -> Iterable[Path]:
    for pdf in sorted(source_root.rglob("*.pdf")):
        if pdf.name.startswith("sliced_") or pdf.name.startswith("temp_"):
            continue
        yield pdf


def build_output_dir(source_root: Path, pdf_path: Path, output_root: Path) -> Path:
    rel = pdf_path.relative_to(source_root)
    return output_root / rel.parent


def command_for_pdf(
    pdf_path: Path,
    source_root: Path,
    output_root: Path,
    mode: str,
    languages: str,
    model_name: str | None = None,
    batch_size: int = 0,
    force: bool = False,
    bucket_name: str | None = None,
    instruction_file: Path | None = None,
) -> List[str]:
    output_dir = build_output_dir(source_root, pdf_path, output_root)
    output_dir.mkdir(parents=True, exist_ok=True)

    standard, tags = infer_metadata_from_path(pdf_path, source_root)
    cmd = [
        sys.executable,
        "-m",
        "academic_content_pipeline.unified_pipeline",
        mode,
        "extract",
        "--input-file",
        str(pdf_path),
        "--output-dir",
        str(output_dir),
        "--standards",
        standard,
        "--tags",
        ",".join(tags),
        "--languages",
        languages,
    ]
    if model_name:
        cmd.extend(["--model-name", model_name])
    if batch_size is not None:
        cmd.extend(["--batch-size", str(int(batch_size))])
    if force:
        cmd.append("--force")
    if instruction_file is not None:
        cmd.extend(["--instruction-file", str(instruction_file)])
    if mode == "remote":
        bucket_name = bucket_name or os.environ.get("GCS_BUCKET_NAME")
        if bucket_name:
            cmd.extend(["--bucket-name", bucket_name])
    return cmd


def execute_pdf_command(cmd: List[str], pdf_path: Path) -> int:
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(Path(__file__).resolve().parents[1] / "src"))
    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        print(f"Failed for {pdf_path}: exit code {result.returncode}", file=sys.stderr)
        return result.returncode
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mirror a PDF vault and infer metadata from source paths.")
    parser.add_argument("--source-root", type=Path, required=True, help="Root directory containing PDFs.")
    parser.add_argument("--output-root", type=Path, required=True, help="Root directory where mirrored XML outputs are written.")
    parser.add_argument("--mode", choices=["context", "agent", "remote", "batch"], default="context", help="Pipeline communication mode.")
    parser.add_argument("--languages", default="english", help="Target languages for extraction (comma-separated, e.g. english,hindi).")
    parser.add_argument("--model-name", default=None, help="Gemini model override. If omitted, the pipeline default is used.")
    parser.add_argument("--batch-size", type=int, default=0, help="Maximum number of pages per extraction request. If 0 or less, processes all pages in one request.")
    parser.add_argument("--parallel-workers", type=int, default=1, help="Maximum number of PDFs to process concurrently. Use 1 for serial execution.")
    parser.add_argument("--bucket-name", default=None, help="GCS bucket name for remote mode.")
    parser.add_argument("--instruction-file", type=Path, default=None, help="Optional instruction file to pass to extraction.")
    parser.add_argument("--dry-run", action="store_true", help="Only print the commands without executing them.")
    parser.add_argument("--force", action="store_true", help="Re-run extraction even when the target XML already exists.")
    parser.add_argument("--verbose", action="store_true", help="Print per-file metadata details.")
    args = parser.parse_args(argv)
    max_workers = max(1, int(args.parallel_workers)) if args.parallel_workers is not None else 1
    args.parallel_workers = max_workers

    source_root = args.source_root.resolve()
    output_root = args.output_root.resolve()
    if not source_root.exists():
        raise FileNotFoundError(f"Source root does not exist: {source_root}")

    output_root.mkdir(parents=True, exist_ok=True)

    processed = 0
    skipped = 0
    skipped_files = []
    pending_jobs = []

    for pdf_path in iter_pdfs(source_root):
        rel = pdf_path.relative_to(source_root)
        output_dir = build_output_dir(source_root, pdf_path, output_root)
        output_dir.mkdir(parents=True, exist_ok=True)

        final_xml = output_dir / f"{pdf_path.stem}.xml"
        if should_skip(final_xml, force=args.force):
            if args.verbose:
                print(f"Skipping {pdf_path}: output already exists at {final_xml}")
            skipped += 1
            skipped_files.append((str(pdf_path), str(final_xml)))
            continue
        elif args.force and final_xml.exists():
            print(f"Forcing rerun for {pdf_path}: existing output at {final_xml} will be overwritten.")

        standard, tags = infer_metadata_from_path(pdf_path, source_root)
        if args.verbose:
            print(f"{rel} -> standard={standard}, tags={tags}")

        cmd = command_for_pdf(
            pdf_path,
            source_root,
            output_root,
            args.mode,
            args.languages,
            model_name=args.model_name,
            batch_size=args.batch_size,
            force=args.force,
            bucket_name=args.bucket_name,
            instruction_file=args.instruction_file,
        )
        if args.dry_run:
            print(" ".join(cmd))
            processed += 1
            continue

        pending_jobs.append((pdf_path, cmd))

    if args.dry_run:
        if processed == 0 and skipped > 0:
            print(f"Skipped {skipped} PDF(s) because their outputs already exist. Use --force to overwrite:")
            for input_pdf, output_xml in skipped_files:
                print(f"  - Input: {input_pdf}")
                print(f"    Output: {output_xml}")
        else:
            print(f"Processed {processed} PDF(s).")
            if skipped > 0:
                print(f"Skipped {skipped} PDF(s):")
                for input_pdf, output_xml in skipped_files:
                    print(f"  - Input: {input_pdf}")
                    print(f"    Output: {output_xml}")
        return 0

    if not pending_jobs:
        if processed == 0 and skipped > 0:
            print(f"Skipped {skipped} PDF(s) because their outputs already exist. Use --force to overwrite:")
            for input_pdf, output_xml in skipped_files:
                print(f"  - Input: {input_pdf}")
                print(f"    Output: {output_xml}")
        else:
            print(f"Processed {processed} PDF(s).")
            if skipped > 0:
                print(f"Skipped {skipped} PDF(s):")
                for input_pdf, output_xml in skipped_files:
                    print(f"  - Input: {input_pdf}")
                    print(f"    Output: {output_xml}")
        return 0

    with ThreadPoolExecutor(max_workers=args.parallel_workers) as executor:
        future_to_pdf = {
            executor.submit(execute_pdf_command, cmd, pdf_path): pdf_path
            for pdf_path, cmd in pending_jobs
        }
        for future in as_completed(future_to_pdf):
            pdf_path = future_to_pdf[future]
            try:
                result_code = future.result()
            except Exception as exc:
                print(f"Failed for {pdf_path}: {exc}", file=sys.stderr)
                return 1
            if result_code != 0:
                return result_code
            processed += 1

    if processed == 0 and skipped > 0:
        print(f"Skipped {skipped} PDF(s) because their outputs already exist. Use --force to overwrite:")
        for input_pdf, output_xml in skipped_files:
            print(f"  - Input: {input_pdf}")
            print(f"    Output: {output_xml}")
    else:
        print(f"Processed {processed} PDF(s).")
        if skipped > 0:
            print(f"Skipped {skipped} PDF(s):")
            for input_pdf, output_xml in skipped_files:
                print(f"  - Input: {input_pdf}")
                print(f"    Output: {output_xml}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
