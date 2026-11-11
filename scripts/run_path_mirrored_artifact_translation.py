#!/usr/bin/env python3
"""Translate reviewed XML, HTML, and TeX artifacts while mirroring source folders.

Example:
    python scripts/run_path_mirrored_artifact_translation.py \
        --source-root /data/reviewed-english \
        --output-root /data/translated \
        --translate-to bengali \
        --mode agent

    python scripts/run_path_mirrored_artifact_translation.py \
        --source-root /data/reviewed-english \
        --in-place \
        --translate-to bengali \
        --mode agent
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
import xml.etree.ElementTree as ET


SUPPORTED_EXTENSIONS = {".xml", ".html", ".tex"}


def iter_source_files(source_root: Path) -> Iterable[Path]:
    for path in sorted(source_root.rglob("*")):
        if (
            path.is_file()
            and path.suffix.lower() in SUPPORTED_EXTENSIONS
            and path.stem.casefold().endswith("_english")
            and not any(part in {".tmp", "__pycache__"} for part in path.relative_to(source_root).parts)
        ):
            yield path


def build_output_dir(source_root: Path, input_path: Path, output_root: Path) -> Path:
    return output_root / input_path.relative_to(source_root).parent


def build_output_stem(source_stem: str, target_language: str) -> str:
    english_stem = (
        source_stem
        if source_stem.casefold().endswith("_english")
        else f"{source_stem}_english"
    )
    language_token = re.sub(
        r"[^a-z0-9]+", "_", target_language.strip().lower()
    ).strip("_")
    if not language_token:
        raise ValueError(f"Target language has no usable filename characters: {target_language!r}")
    return f"{english_stem}_{language_token}"


def command_for_input(
    input_path: Path,
    output_dir: Path,
    mode: str,
    translate_to: str,
    *,
    model_name: str | None,
    force: bool,
    bucket_name: str | None,
    xml_chunk_size: int,
) -> List[str]:
    command = [
        sys.executable,
        "-m",
        "academic_content_pipeline.unified_pipeline",
        mode,
        "translate-artifacts",
        "--input-file",
        str(input_path),
        "--output-dir",
        str(output_dir),
        "--translate-to",
        translate_to,
        "--xml-chunk-size",
        str(xml_chunk_size),
    ]
    if model_name:
        command.extend(["--model-name", model_name])
    if force:
        command.append("--force")
    if mode == "remote":
        bucket_name = bucket_name or os.environ.get("GCS_BUCKET_NAME")
        if bucket_name:
            command.extend(["--bucket-name", bucket_name])
    return command


def has_translation(output_dir: Path, input_path: Path, target_language: str) -> bool:
    output_path = output_dir / (
        f"{build_output_stem(input_path.stem, target_language)}{input_path.suffix.lower()}"
    )
    if not output_path.is_file() or output_path.stat().st_size == 0:
        return False
    if output_path.suffix.lower() != ".xml":
        return True
    try:
        root = ET.parse(output_path).getroot()
    except ET.ParseError:
        return False
    return root.tag == "quiz" and all(child.tag == "question" for child in root)


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
    result = subprocess.run(
        command,
        cwd=source_root,
        env=environment,
        check=False,
    )
    if result.returncode != 0:
        print(f"Failed for {input_path}: exit code {result.returncode}", file=sys.stderr)
    return result.returncode


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Translate reviewed assessment artifacts while mirroring source folders."
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="Root containing reviewed English XML, HTML, or TeX artifacts.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        help="Root for mirrored bilingual translations (required unless --in-place is used).",
    )
    parser.add_argument(
        "--in-place",
        action="store_true",
        help="Write each bilingual translation beside its English source file.",
    )
    parser.add_argument(
        "--mode",
        choices=["context", "agent", "remote", "batch", "sequential"],
        default="context",
        help="AI communication mode.",
    )
    parser.add_argument(
        "--translate-to",
        required=True,
        help="Comma-separated target languages, for example bengali,hindi.",
    )
    parser.add_argument("--model-name", default=None, help="Gemini model override.")
    parser.add_argument("--bucket-name", default=None, help="GCS bucket for remote mode.")
    parser.add_argument(
        "--xml-chunk-size",
        type=int,
        default=5,
        help="Questions per AI request when translating Moodle XML (default: 5).",
    )
    parser.add_argument(
        "--parallel-workers",
        type=int,
        default=1,
        help="Maximum concurrent source artifacts to translate.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print commands without executing them.")
    parser.add_argument("--force", action="store_true", help="Regenerate existing translations.")
    args = parser.parse_args(argv)

    if args.xml_chunk_size < 1:
        parser.error("--xml-chunk-size must be at least 1.")

    if args.mode == "sequential":
        args.mode = "context"

    target_languages = list(
        dict.fromkeys(
            language.strip()
            for language in args.translate_to.split(",")
            if language.strip()
        )
    )
    if not target_languages:
        parser.error("Provide at least one target language with --translate-to.")
    if any(language.casefold() == "english" for language in target_languages):
        parser.error("--translate-to accepts non-English language names only.")

    seen_targets = set()
    for language in target_languages:
        normalized = language.casefold()
        if normalized in seen_targets:
            parser.error(f"Duplicate target language: {language}")
        seen_targets.add(normalized)
        try:
            build_output_stem("artifact", language)
        except ValueError as exc:
            parser.error(str(exc))

    source_root = args.source_root.expanduser().resolve()
    if not source_root.is_dir():
        parser.error(f"Source root does not exist or is not a directory: {source_root}")
    if args.in_place and args.output_root is not None:
        parser.error("--output-root cannot be combined with --in-place.")
    if not args.in_place and args.output_root is None:
        parser.error("Provide --output-root or use --in-place.")
    output_root = (
        source_root
        if args.in_place
        else args.output_root.expanduser().resolve()
    )
    if not args.in_place and (
        output_root == source_root or source_root in output_root.parents
    ):
        parser.error(
            "--output-root must be outside --source-root so translated files are "
            "not treated as English source artifacts."
        )

    source_files = list(iter_source_files(source_root))
    if not source_files:
        parser.error(
            "No English XML, HTML, or TeX artifacts ending in '_english' found "
            f"under: {source_root}"
        )

    jobs: List[tuple[Path, List[str]]] = []
    skipped = 0
    for input_path in source_files:
        output_dir = build_output_dir(source_root, input_path, output_root)
        missing_targets = [
            language
            for language in target_languages
            if not has_translation(output_dir, input_path, language)
        ]
        if not missing_targets and not args.force:
            print(f"Skipping {input_path}: all requested translations already exist.")
            skipped += 1
            continue

        for language in target_languages:
            if (
                not args.force
                and language not in missing_targets
            ):
                continue
            command = command_for_input(
                input_path,
                output_dir,
                args.mode,
                language,
                model_name=args.model_name,
                force=args.force,
                bucket_name=args.bucket_name,
                xml_chunk_size=args.xml_chunk_size,
            )
            if args.dry_run:
                print(subprocess.list2cmdline(command))
            else:
                jobs.append((input_path, command))

    if args.dry_run:
        print(
            f"Prepared translations for {len(source_files)} source artifact(s); "
            f"skipped {skipped} artifact(s)."
        )
        return 0
    if not jobs:
        print(f"No translations to generate. Skipped {skipped} existing artifact(s).")
        return 0

    failures = 0
    max_workers = max(1, args.parallel_workers)
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

    successful = len(jobs) - failures
    print(
        f"Translated {successful} artifact-language pair(s); "
        f"skipped {skipped} artifact(s); failed {failures} translation(s)."
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
