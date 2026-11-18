#!/usr/bin/env python3
"""Mirror a source PDF vault into an output structure and infer metadata from each path.

This wrapper walks every PDF in a source directory, derives the standard and tags
from the relative path layout, and calls the extraction CLI for each file while
preserving the same folder structure under the output root.

Example:
    python scripts/run_path_mirrored_extraction.py \
        --source-root /data/pdfs \
        --top-category "Previous Year Question Papers" \
        --output-root /workspace/output \
        --mode batch

The inferred metadata may look like:
    standards = NEET UG
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

from mirrored_path_utils import rebuild_mirrored_combined_xml, resolve_subtree


def normalize_standard(value: str) -> str:
    value = value.strip().lower()
    replacements = {
        "jee_main": "jee_main",
        "jee main": "jee_main",
        "jee-main": "jee_main",
        "jee advanced": "jee_advanced",
        "jee_advanced": "jee_advanced",
        "neet": "neet_ug",
        "neet ug": "neet_ug",
        "neet_ug": "neet_ug",
        "neet-ug": "neet_ug",
        "wbjee": "wbjee",
        "wb-jee": "wbjee",
    }
    return replacements.get(value, re.sub(r"[^a-z0-9]+", "_", value).strip("_"))


def infer_metadata_from_path(pdf_path: Path, source_root: Path) -> tuple[str, List[str]]:
    """Infer standard and year tags from the PDF's path relative to the source root."""
    try:
        relative_path = pdf_path.relative_to(source_root)
    except ValueError:
        relative_path = pdf_path
    path_parts = [part for part in relative_path.parts if part not in (".", "")]
    path_text = relative_path.as_posix().lower()

    standard = "general"
    standard_aliases = {
        "jee_main": ["jee main", "jee-main", "jee_main"],
        "jee_advanced": ["jee advanced", "jee_advanced"],
        "neet_ug": [
            "neet",
            "neet ug",
            "neet_ug",
            "neet-ug",
            "ncert-exemplar",
            "ncert_exemplar",
        ],
        "wbjee": ["wbjee", "wb-jee", "wb_jee"],
    }
    for canonical, aliases in standard_aliases.items():
        if any(alias in path_text for alias in aliases):
            standard = canonical
            break

    if standard == "general":
        for part in path_parts:
            normalized = normalize_standard(part)
            if normalized and normalized != "general" and any(token in part.lower() for token in ("neet", "jee", "wbjee")):
                standard = normalized
                break

    years = []
    for match in re.finditer(r"(?<!\d)(\d{4})(?!\d)", relative_path.as_posix()):
        year = match.group(1)
        if year not in years:
            years.append(year)
    tags = [f"year:{year}" for year in years]

    return standard, tags


def should_skip(output_xml: Path, force: bool = False) -> bool:
    if force:
        return False
    return output_xml.exists() and output_xml.stat().st_size > 0


def normalized_languages(languages: str) -> List[str]:
    return list(dict.fromkeys(
        token
        for token in (
            re.sub(r"[^a-zA-Z0-9._-]+", "_", language.strip()).strip("._-").lower()
            for language in languages.split(",")
        )
        if token
    )) or ["english"]


def generated_output_path(
    output_dir: Path,
    source_stem: str,
    standard: str,
    languages: str,
) -> Path:
    """Build the extraction output path for the requested standard and language set."""
    name_parts = [source_stem]
    standard_token = re.sub(r"[^a-zA-Z0-9._-]+", "_", standard).strip("._-").lower()
    if standard_token and standard_token != "general":
        name_parts.append(standard_token)
    name_parts.extend(normalized_languages(languages))
    generated_prefix = "_".join(name_parts)

    return output_dir / f"{generated_prefix}.xml"


def find_existing_output(
    output_dir: Path,
    source_stem: str,
    standard: str,
    languages: str,
) -> Path | None:
    """Find an extraction output for the requested standard and language set."""
    candidates = [generated_output_path(output_dir, source_stem, standard, languages)]
    standard_token = re.sub(r"[^a-zA-Z0-9._-]+", "_", standard).strip("._-").lower()
    language_tokens = normalized_languages(languages)
    if standard_token in {"", "general"} and language_tokens == ["english"]:
        candidates.append(output_dir / f"{source_stem}.xml")
    return next(
        (candidate for candidate in candidates if candidate.is_file() and candidate.stat().st_size > 0),
        None,
    )


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
    instruction_page_model_name: str | None = None,
    ocr_model: str | None = None,
    batch_size: int = 5,
    force: bool = False,
    resume: bool = False,
    replace_pages: list[int] | None = None,
    disable_ocr_cache: bool = False,
    bucket_name: str | None = None,
    instruction_file: Path | None = None,
    top_category: str | None = None,
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
    if top_category is not None:
        cmd.extend(["--top-category", top_category, "--source-root", str(source_root)])
    if model_name:
        cmd.extend(["--model-name", model_name])
    if instruction_page_model_name:
        cmd.extend(["--instruction-page-model-name", instruction_page_model_name])
    if ocr_model:
        cmd.extend(["--ocr-model", ocr_model])
    if batch_size is not None:
        cmd.extend(["--batch-size", str(int(batch_size))])
    if force:
        cmd.append("--force")
    if resume:
        cmd.append("--resume")
    if replace_pages:
        cmd.extend(["--replace-pages", str(replace_pages[0]), str(replace_pages[1])])
    if disable_ocr_cache:
        cmd.append("--disable-ocr-cache")
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


def repair_outputs(
    source_root: Path,
    output_root: Path,
    backup_root: Path,
    languages: str,
    dry_run: bool,
    scan_root: Path,
) -> int:
    from rebuild_ncert_exemplar_xml import repair_manifest
    import xml.etree.ElementTree as ET

    candidates = []
    errors = []
    skipped = 0
    for pdf_path in iter_pdfs(scan_root):
        if not any(part.casefold() == "ncert_exemplar" for part in pdf_path.parts):
            errors.append(f"{pdf_path}: repair is supported only for NCERT Exemplar PDFs.")
            continue
        output_dir = build_output_dir(source_root, pdf_path, output_root)
        standard, _ = infer_metadata_from_path(pdf_path, source_root)
        xml_path = generated_output_path(output_dir, pdf_path.stem, standard, languages)
        manifest_path = xml_path.with_suffix(".extract.json")
        if not manifest_path.is_file() and not xml_path.exists():
            skipped += 1
            print(f"Skipped {pdf_path}: no output or checkpoint for {languages}.")
            continue
        try:
            count = repair_manifest(
                manifest_path, pdf_path, xml_path, output_root, backup_root, dry_run=True
            )
            candidates.append((pdf_path, manifest_path, xml_path, count))
        except (OSError, UnicodeError, ET.ParseError, ValueError, TypeError) as exc:
            errors.append(f"{pdf_path}: {exc}")

    if not candidates and not errors:
        errors.append("No matching completed extraction checkpoints were found.")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        print("Repair preflight failed; no files were changed.", file=sys.stderr)
        return 1
    print(f"Preflight passed for {len(candidates)} chapter(s); {skipped} without output skipped.")
    print(f"Backups: {backup_root}")
    if dry_run:
        for _, _, xml_path, count in candidates:
            print(f"Would repair {xml_path} ({count} questions).")
        return 0

    backup_root.mkdir(parents=True, exist_ok=False)
    repaired = 0
    for pdf_path, manifest_path, xml_path, _ in candidates:
        try:
            count = repair_manifest(manifest_path, pdf_path, xml_path, output_root, backup_root)
        except (OSError, UnicodeError, ET.ParseError, ValueError, TypeError, RuntimeError) as exc:
            print(f"ERROR: {pdf_path}: {exc}", file=sys.stderr)
            print(f"Stopped after repairing {repaired} chapter(s). Backups: {backup_root}", file=sys.stderr)
            return 1
        repaired += 1
        print(f"Repaired {xml_path} ({count} questions).")
    print(f"Repaired {repaired} chapter(s). Backups: {backup_root}")
    print("Review recovered questions against the source before use.")
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mirror a PDF vault and infer metadata from source paths.")
    parser.add_argument("--source-root", type=Path, required=True, help="Root directory containing PDFs.")
    parser.add_argument("--subtree", type=Path, help="Only process this directory relative to --source-root.")
    parser.add_argument("--top-category", help="Top-level Moodle question bank category.")
    parser.add_argument("--output-root", type=Path, required=True, help="Root directory where mirrored XML outputs are written.")
    parser.add_argument("--mode", choices=["context", "agent", "remote", "batch", "sequential"], default="context", help="Pipeline communication mode.")
    parser.add_argument("--languages", default="english", help="Target languages for extraction (comma-separated, e.g. english,hindi).")
    parser.add_argument("--model-name", default=None, help="Gemini model override. If omitted, the pipeline default is used.")
    parser.add_argument(
        "--instruction-page-model-name",
        default=None,
        help="Gemini model for instruction-page classification. If omitted, --model-name is used.",
    )
    parser.add_argument("--model", "--ocr-model", dest="ocr_model", default=os.environ.get("MISTRAL_OCR_MODEL", "mistral-ocr-latest"), help="Mistral OCR model name to use.")
    parser.add_argument("--batch-size", type=int, default=5, help="Maximum pages per extraction request (default: 5); 0 processes the entire document in one request.")
    parser.add_argument("--parallel-workers", type=int, default=1, help="Maximum number of PDFs to process concurrently. Use 1 for serial execution.")
    parser.add_argument("--bucket-name", default=None, help="GCS bucket name for remote mode.")
    parser.add_argument("--instruction-file", type=Path, default=None, help="Optional instruction file to pass to extraction.")
    parser.add_argument("--dry-run", action="store_true", help="Only print the commands without executing them.")
    parser.add_argument("--force", action="store_true", help="Re-run extraction even when the target XML already exists.")
    parser.add_argument("--resume", action="store_true", help="Resume extraction from per-PDF batch checkpoints.")
    parser.add_argument("--repair", action="store_true", help="Rebuild existing XML and batch checkpoints offline from saved responses.")
    parser.add_argument("--repair-backup-root", type=Path, default=None, help="New, separate directory for pre-repair backups; required with --repair.")
    parser.add_argument(
        "--replace-pages",
        type=int,
        nargs=2,
        metavar=("START", "END"),
        help="Re-extract and replace saved extraction batches covering this page range; requires a checkpoint.",
    )
    parser.add_argument("--disable-ocr-cache", action="store_true", help="Disable the local OCR cache for the extraction run.")
    parser.add_argument("--verbose", action="store_true", help="Print per-file metadata details.")
    args = parser.parse_args(argv)
    if not args.repair and not args.top_category:
        parser.error("--top-category is required for Moodle XML extraction.")
    if args.repair and (args.force or args.resume or args.replace_pages):
        parser.error("--repair cannot be combined with --force, --resume or --replace-pages.")
    if args.repair and args.repair_backup_root is None:
        parser.error("--repair requires --repair-backup-root.")
    if args.repair_backup_root is not None and not args.repair:
        parser.error("--repair-backup-root requires --repair.")
    if args.repair and args.parallel_workers not in (None, 1):
        parser.error("--repair requires --parallel-workers 1.")
    if args.force and (args.resume or args.replace_pages):
        parser.error(
            "--force cannot be combined with --resume or a replacement option."
        )
    replacement_options = [
        bool(args.replace_pages),
    ]
    if args.resume and any(replacement_options):
        parser.error("--resume cannot be combined with a replacement option.")
    if args.mode == "sequential":
        args.mode = "context"
    max_workers = max(1, int(args.parallel_workers)) if args.parallel_workers is not None else 1
    args.parallel_workers = max_workers

    source_root = args.source_root.expanduser().resolve()
    output_root = args.output_root.expanduser().resolve()
    if not source_root.is_dir():
        raise NotADirectoryError(f"Source root does not exist or is not a directory: {source_root}")
    try:
        scan_root = resolve_subtree(source_root, args.subtree)
    except ValueError as exc:
        parser.error(str(exc))

    if args.repair:
        backup_root = args.repair_backup_root.resolve()
        for root in (source_root, output_root):
            if backup_root.is_relative_to(root) or root.is_relative_to(backup_root):
                parser.error("--repair-backup-root must be separate from source and output roots.")
        if backup_root.exists():
            parser.error("--repair-backup-root must be a new directory.")
        if not source_root.is_dir() or not output_root.is_dir():
            parser.error("--repair requires existing source and output directories.")
        return repair_outputs(source_root, output_root, backup_root, args.languages, args.dry_run, scan_root)

    output_root.mkdir(parents=True, exist_ok=True)

    processed = 0
    skipped = 0
    skipped_files = []
    pending_jobs = []

    for pdf_path in iter_pdfs(scan_root):
        rel = pdf_path.relative_to(source_root)
        output_dir = build_output_dir(source_root, pdf_path, output_root)
        output_dir.mkdir(parents=True, exist_ok=True)

        standard, tags = infer_metadata_from_path(pdf_path, source_root)
        final_xml = find_existing_output(output_dir, pdf_path.stem, standard, args.languages)
        if final_xml is not None and should_skip(
            final_xml,
            force=(
                args.force
                or args.resume
                or bool(args.replace_pages)
            ),
        ):
            if args.verbose:
                print(f"Skipping {pdf_path}: output already exists at {final_xml}")
            skipped += 1
            skipped_files.append((str(pdf_path), str(final_xml)))
            continue
        elif args.force and final_xml is not None:
            print(f"Forcing rerun for {pdf_path}: an updated output will be generated; existing output is {final_xml}.")

        if args.verbose:
            print(f"{rel} -> standard={standard}, tags={tags}")

        cmd = command_for_pdf(
            pdf_path,
            source_root,
            output_root,
            args.mode,
            args.languages,
            model_name=args.model_name,
            instruction_page_model_name=args.instruction_page_model_name,
            ocr_model=args.ocr_model,
            batch_size=args.batch_size,
            force=args.force,
            resume=args.resume,
            replace_pages=args.replace_pages,
            disable_ocr_cache=args.disable_ocr_cache,
            bucket_name=args.bucket_name,
            instruction_file=args.instruction_file,
            top_category=args.top_category,
        )
        if args.dry_run:
            print(" ".join(cmd))
            processed += 1
            continue

        cmd.append("--defer-combined-xml")
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
        rebuild_mirrored_combined_xml(output_root)
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
    rebuild_mirrored_combined_xml(output_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
