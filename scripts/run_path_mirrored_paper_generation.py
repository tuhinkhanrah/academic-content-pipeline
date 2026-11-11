#!/usr/bin/env python3
"""Generate exam papers from specs while mirroring the spec folder structure.

Examples:
    python scripts/run_path_mirrored_paper_generation.py \
        --spec-root src/academic_content_pipeline/resources/spec_templates \
        --output-root output/papers \
        --mode agent \
        --num-tests 1

    python scripts/run_path_mirrored_paper_generation.py \
        --spec-root src/academic_content_pipeline/resources/spec_templates \
        --spec wbjee/mathematics.template.yaml \
        --output-root output/papers/wbjee-mathematics \
        --mode agent \
        --resume

Without --spec, batch specs are discovered recursively under --spec-root.
With --spec, one batch or paper spec is generated.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List

import yaml


SPEC_SUFFIXES = {".yaml", ".yml", ".json"}


def normalize_standard(value: str) -> str:
    """Normalize common exam names to the canonical standards used by the CLI."""
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
    return replacements.get(
        value,
        re.sub(r"[^a-z0-9]+", "_", value).strip("_"),
    )


def infer_standard_from_path(
    spec_path: Path,
    spec_root: Path | None = None,
) -> str:
    """Infer an exam standard from the spec's path, matching question generation."""
    path_parts = [part for part in spec_path.parts if part not in (".", "")]
    root_parts = (
        [part for part in spec_root.parts if part not in (".", "")]
        if spec_root is not None
        else []
    )
    inference_parts = root_parts + path_parts
    path_text = "/".join(inference_parts).lower()
    standard_aliases = {
        "jee_main": ("jee main", "jee-main", "jee_main"),
        "jee_advanced": ("jee advanced", "jee-advanced", "jee_advanced"),
        "neet_ug": ("neet", "neet ug", "neet_ug", "neet-ug"),
        "wbjee": ("wbjee", "wb-jee", "wb_jee"),
    }

    for standard, aliases in standard_aliases.items():
        if any(alias in path_text for alias in aliases):
            return standard
    for part in inference_parts:
        normalized = normalize_standard(part)
        if normalized not in {"", "general"} and any(
            token in part.lower() for token in ("neet", "jee", "wbjee")
        ):
            return normalized
    return "general"


def read_spec(spec_path: Path) -> Dict[str, Any]:
    """Load a YAML/JSON spec mapping, raising a useful error when invalid."""
    try:
        raw = spec_path.read_text(encoding="utf-8")
        if spec_path.suffix.casefold() == ".json":
            import json

            payload = json.loads(raw)
        else:
            payload = yaml.safe_load(raw)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ValueError(f"Unable to read paper spec {spec_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Paper spec must contain a mapping: {spec_path}")
    return payload


def is_batch_spec(spec_data: Dict[str, Any]) -> bool:
    """Return whether a spec orchestrates multiple papers or test sets."""
    return isinstance(spec_data.get("papers"), list) or "test_count" in spec_data


def discover_batch_specs(spec_root: Path) -> List[Path]:
    """Find batch specs recursively, excluding their referenced paper specs."""
    specs = []
    for spec_path in sorted(spec_root.rglob("*")):
        if not spec_path.is_file() or spec_path.suffix.casefold() not in SPEC_SUFFIXES:
            continue
        spec_data = read_spec(spec_path)
        if is_batch_spec(spec_data):
            specs.append(spec_path.resolve())
    return specs


def build_output_dir(spec_root: Path, spec_path: Path, output_root: Path) -> Path:
    """Mirror the spec's parent folder below the output root."""
    try:
        relative_parent = spec_path.resolve().parent.relative_to(spec_root.resolve())
    except ValueError as exc:
        raise ValueError(
            f"Spec must be located under --spec-root to mirror its output path: {spec_path}"
        ) from exc
    return output_root / relative_parent


def command_for_spec(
    spec_path: Path,
    output_dir: Path,
    mode: str,
    *,
    model_name: str | None,
    languages: str,
    standards: str | None,
    tags: str,
    difficulty_mix: str,
    num_tests: int,
    max_paper_attempts: int,
    sample_pdf: Path | None,
    exam_duration_minutes: int | None,
    resume: bool,
    force: bool,
    reference_analysis_mode: str = "batched",
    reference_papers: List[str] | None = None,
    no_reference_papers: bool = False,
) -> List[str]:
    """Build the CLI command for one paper spec."""
    command = [
        sys.executable,
        "-m",
        "academic_content_pipeline.unified_pipeline",
        mode,
        "generate-paper",
        "--spec",
        str(spec_path),
        "--output-dir",
        str(output_dir),
        "--output-format",
        "xml",
        "--languages",
        languages,
        "--tags",
        tags,
        "--difficulty-mix",
        difficulty_mix,
        "--num-tests",
        str(num_tests),
        "--max-paper-attempts",
        str(max_paper_attempts),
    ]
    if model_name:
        command.extend(["--model-name", model_name])
    if standards:
        command.extend(["--standards", standards])
    if sample_pdf or reference_papers:
        for reference in reference_papers or [str(sample_pdf)]:
            command.extend(["--reference-paper", str(reference)])
    if no_reference_papers:
        command.append("--no-reference-paper")
    command.extend(["--reference-analysis-mode", reference_analysis_mode])
    if exam_duration_minutes is not None:
        command.extend(["--exam-duration-minutes", str(exam_duration_minutes)])
    if resume:
        command.append("--resume")
    if force:
        command.append("--force")
    return command


def execute_command(command: List[str], spec_path: Path) -> int:
    """Run one CLI command with this checkout's src directory importable."""
    environment = os.environ.copy()
    repository_root = Path(__file__).resolve().parents[1]
    source_path = str(repository_root / "src")
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not existing_pythonpath
        else source_path + os.pathsep + existing_pythonpath
    )
    result = subprocess.run(command, env=environment, check=False)
    if result.returncode:
        print(
            f"Failed for {spec_path}: exit code {result.returncode}",
            file=sys.stderr,
        )
    return result.returncode


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate exam papers from specs while mirroring spec folders."
    )
    parser.add_argument(
        "--spec-root",
        type=Path,
        help="Root containing paper/batch specs; required for discovery.",
    )
    parser.add_argument(
        "--spec",
        type=Path,
        help="Generate one YAML/JSON batch or paper spec instead of discovering batch specs.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        required=True,
        help="Root for outputs; each spec's relative parent folder is mirrored beneath it.",
    )
    parser.add_argument(
        "--mode",
        choices=["context", "agent", "remote", "batch"],
        default="context",
        help="AI communication mode.",
    )
    parser.add_argument("--model-name", default=None, help="Optional Gemini model override.")
    parser.add_argument("--languages", default="english", help="Comma-separated target languages.")
    parser.add_argument("--standards", default=None, help="Optional exam-standard override.")
    parser.add_argument("--tags", default="", help="Comma-separated global tags.")
    parser.add_argument(
        "--difficulty-mix",
        default="easy:0.2,medium:0.5,hard:0.3",
        help="Difficulty ratios passed to the paper generator.",
    )
    parser.add_argument(
        "--num-tests",
        type=int,
        default=1,
        help="Number of test sets per spec (default: 1).",
    )
    parser.add_argument(
        "--max-paper-attempts",
        type=int,
        default=3,
        help="Maximum generation attempts per paper in this invocation.",
    )
    reference_options = parser.add_mutually_exclusive_group()
    reference_options.add_argument("--reference-paper", action="append", default=None, metavar="[PAPER_ID=]PATH", help="Reference PDF or OCR Markdown; repeat with paper IDs for batch-specific references.")
    reference_options.add_argument("--sample-pdf", type=Path, default=None, help="Deprecated alias for --reference-paper.")
    reference_options.add_argument("--no-reference-paper", action="store_true", help="Ignore reference papers configured in batch templates.")
    parser.add_argument("--reference-analysis-mode", choices=["batched", "full"], default="batched")
    parser.add_argument(
        "--exam-duration-minutes",
        type=int,
        default=None,
        help="Optional exam duration for PDF rendering; retained for CLI compatibility.",
    )
    parser.add_argument(
        "--parallel-workers",
        type=int,
        default=1,
        help="Maximum concurrent spec runs; defaults to 1 to limit API load.",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume an existing checkpoint, or start a new run if none exists.",
    )
    parser.add_argument("--force", action="store_true", help="Start a fresh run in an existing output directory.")
    parser.add_argument("--dry-run", action="store_true", help="Print commands without running them.")
    args = parser.parse_args(argv)
    if args.sample_pdf is not None:
        print("Warning: --sample-pdf is deprecated; use --reference-paper.", file=sys.stderr)

    if args.resume and args.force:
        parser.error("--resume and --force cannot be combined.")
    if args.num_tests < 1:
        parser.error("--num-tests must be at least 1.")
    if args.max_paper_attempts < 1:
        parser.error("--max-paper-attempts must be at least 1.")
    if args.parallel_workers < 1:
        parser.error("--parallel-workers must be at least 1.")

    output_root = args.output_root.expanduser().resolve()
    if args.spec_root is not None:
        spec_root = args.spec_root.expanduser().resolve()
        if not spec_root.is_dir():
            raise NotADirectoryError(f"Spec root does not exist: {spec_root}")
    elif args.spec is not None:
        spec_root = args.spec.expanduser().resolve().parent
    else:
        parser.error("Provide --spec-root for discovery or --spec for one paper.")

    if args.spec is not None:
        spec_path = args.spec.expanduser()
        if not spec_path.is_absolute() and args.spec_root is not None:
            rooted_path = spec_root / spec_path
            if rooted_path.is_file():
                spec_path = rooted_path
        spec_paths = [spec_path.resolve()]
        if not spec_paths[0].is_file():
            raise FileNotFoundError(f"Paper spec does not exist: {spec_paths[0]}")
        read_spec(spec_paths[0])
    else:
        spec_paths = discover_batch_specs(spec_root)
        if not spec_paths:
            parser.error(f"No batch specs found under: {spec_root}")

    if not args.dry_run:
        output_root.mkdir(parents=True, exist_ok=True)
    jobs = []
    output_dirs = set()
    for spec_path in spec_paths:
        output_dir = build_output_dir(spec_root, spec_path, output_root)
        if output_dir in output_dirs:
            raise ValueError(
                f"Multiple specs resolve to the same output directory: {output_dir}"
            )
        output_dirs.add(output_dir)
        command = command_for_spec(
            spec_path,
            output_dir,
            args.mode,
            model_name=args.model_name,
            languages=args.languages,
            standards=args.standards or infer_standard_from_path(spec_path, spec_root),
            tags=args.tags,
            difficulty_mix=args.difficulty_mix,
            num_tests=args.num_tests,
            max_paper_attempts=args.max_paper_attempts,
            sample_pdf=args.sample_pdf,
            exam_duration_minutes=args.exam_duration_minutes,
            resume=args.resume,
            force=args.force,
            reference_analysis_mode=args.reference_analysis_mode,
            reference_papers=args.reference_paper,
            no_reference_papers=args.no_reference_paper,
        )
        jobs.append((spec_path, command))
        if args.dry_run:
            print(subprocess.list2cmdline(command))

    if args.dry_run:
        print(f"Prepared {len(jobs)} spec(s); outputs under {output_root}.")
        return 0

    failures = 0
    with ThreadPoolExecutor(max_workers=args.parallel_workers) as executor:
        futures = {
            executor.submit(execute_command, command, spec_path): spec_path
            for spec_path, command in jobs
        }
        for future in as_completed(futures):
            try:
                if future.result() != 0:
                    failures += 1
            except OSError as exc:
                print(f"Failed for {futures[future]}: {exc}", file=sys.stderr)
                failures += 1

    print(f"Processed {len(jobs) - failures} spec(s); failed {failures} spec(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
