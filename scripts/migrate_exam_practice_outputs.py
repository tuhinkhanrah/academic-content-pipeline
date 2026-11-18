#!/usr/bin/env python3
"""Move one exam's generated papers into a year folder and backfill XML categories."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from academic_content_pipeline.moodle_categories import (
    build_exam_practice_paper_category_path,
    set_moodle_category,
)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomically(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".migration-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as temporary_file:
            temporary_file.write(content)
        os.replace(temporary_name, path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read batch manifest {path}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError(f"Batch manifest must contain a JSON object: {path}")
    return manifest


def _updates_for_tree(
    year_root: Path,
    *,
    exam_name: str,
    year: str,
    top_category: str,
    previous_exam_name: str | None = None,
) -> tuple[list[tuple[Path, bytes]], list[tuple[Path, bytes]]]:
    xml_updates: list[tuple[Path, bytes]] = []
    manifest_updates: list[tuple[Path, bytes]] = []
    manifests = sorted(year_root.rglob("*_batch_generation.json"))
    if not manifests:
        raise ValueError(f"No batch-generation manifests found under {year_root}")

    for manifest_path in manifests:
        manifest = _read_manifest(manifest_path)
        configuration = manifest.get("configuration")
        items = manifest.get("items")
        if not isinstance(configuration, dict) or not isinstance(items, list):
            raise ValueError(f"Invalid batch manifest structure: {manifest_path}")
        languages = configuration.get("languages")
        if (
            not isinstance(languages, list)
            or not languages
            or any(not isinstance(language, str) for language in languages)
        ):
            raise ValueError(f"Invalid language list in batch manifest: {manifest_path}")
        category_path = build_exam_practice_paper_category_path(
            top_category, exam_name, year, languages
        )
        previous_category = None
        if previous_exam_name is not None:
            previous_category = category_path.replace(
                f"/{exam_name}/{year}/", f"/{previous_exam_name}/{year}/", 1
            )
            saved_category = configuration.get("category_path")
            if saved_category not in {previous_category, category_path}:
                raise ValueError(f"Unexpected checkpoint category: {manifest_path}")
            configuration_hash = _sha256(
                json.dumps(configuration, sort_keys=True, ensure_ascii=False, default=str)
                .encode("utf-8")
            )
            if manifest.get("configuration_sha256") != configuration_hash:
                raise ValueError(f"Checkpoint configuration hash does not match: {manifest_path}")
        for item in items:
            if not isinstance(item, dict):
                raise ValueError(f"Invalid batch item in {manifest_path}")
            if previous_exam_name is not None and item.get("status") == "ready_to_commit":
                raise ValueError(f"Cannot recategorize an uncommitted candidate: {manifest_path}")
            if item.get("status") != "completed":
                continue
            output_file = item.get("output_file")
            if not isinstance(output_file, str):
                raise ValueError(f"Completed item has no output_file in {manifest_path}")
            output_path = (manifest_path.parent / output_file).resolve()
            if not output_path.is_relative_to(manifest_path.parent.resolve()):
                raise ValueError(f"Output path escapes its manifest directory: {output_file}")
            try:
                original = output_path.read_bytes()
                expected_hash = item.get("output_sha256")
                if not isinstance(expected_hash, str) or _sha256(original) != expected_hash:
                    raise ValueError(f"Output hash does not match the checkpoint: {output_path}")
                if previous_category is not None:
                    quiz = ET.fromstring(original)
                    categories = [
                        question for question in quiz
                        if question.get("type") == "category"
                    ]
                    if (
                        quiz.tag != "quiz"
                        or len(categories) != 1
                        or quiz[0] is not categories[0]
                        or categories[0].findtext("category/text") != saved_category
                    ):
                        raise ValueError(f"Output category does not match checkpoint: {output_path}")
                updated_xml = set_moodle_category(
                    original.decode("utf-8-sig"), category_path
                ).encode("utf-8")
            except (OSError, UnicodeError, ValueError, ET.ParseError) as exc:
                raise ValueError(f"Unable to migrate output {output_path}: {exc}") from exc
            item["output_sha256"] = _sha256(updated_xml)
            xml_updates.append((output_path, updated_xml))

        configuration["category_path"] = category_path
        manifest["configuration_sha256"] = _sha256(
            json.dumps(
                configuration,
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            ).encode("utf-8")
        )
        manifest_updates.append(
            (
                manifest_path,
                json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
            )
        )
    return xml_updates, manifest_updates


def recategorize_exam_outputs(
    year_root: Path,
    *,
    exam_name: str,
    previous_exam_name: str,
    year: str,
    top_category: str,
    apply: bool = False,
) -> tuple[int, int]:
    year_root = Path(year_root).expanduser().resolve()
    if not year_root.is_dir() or year_root.name != year:
        raise ValueError(f"Expected an existing {year} year directory: {year_root}")
    if year_root.parent.name != exam_name:
        raise ValueError(f"Exam slug must match the containing folder: {year_root.parent}")
    for folder in (year_root.parent.parent, year_root.parent):
        if any(folder.glob("*combined_moodle.xml")):
            raise ValueError(f"Rebuild existing combined XML after migration: {folder}")
    if any(
        path.name.casefold().endswith("combined_moodle.xml")
        for path in year_root.rglob("*.xml")
    ):
        raise ValueError(f"Existing combined XML needs a separate rebuild: {year_root}")
    updates = _updates_for_tree(
        year_root,
        exam_name=exam_name,
        year=year,
        top_category=top_category,
        previous_exam_name=previous_exam_name,
    )
    if not apply:
        return tuple(len(group) for group in updates)

    files = updates[0] + updates[1]
    original_files = {path: path.read_bytes() for path, _ in files}
    try:
        for path, content in files:
            _write_atomically(path, content)
        verified = _updates_for_tree(
            year_root,
            exam_name=exam_name,
            year=year,
            top_category=top_category,
            previous_exam_name=previous_exam_name,
        )
        for path, content in verified[0] + verified[1]:
            if path.read_bytes() != content:
                raise ValueError(f"Post-migration checkpoint verification failed: {path}")
    except Exception:
        for path, content in original_files.items():
            _write_atomically(path, content)
        raise
    return tuple(len(group) for group in updates)


def migrate_exam_outputs(
    exam_root: Path,
    *,
    year: str,
    exam_name: str,
    top_category: str,
    apply: bool = False,
) -> tuple[int, int]:
    exam_root = Path(exam_root).expanduser().resolve()
    if not re.fullmatch(r"\d{4}", year):
        raise ValueError("Year must be four digits.")
    if not exam_root.is_dir():
        raise NotADirectoryError(f"Exam output directory does not exist: {exam_root}")
    destination = exam_root / year
    if destination.exists():
        raise FileExistsError(f"Year output directory already exists: {destination}")
    entries = list(exam_root.iterdir())
    if not entries:
        raise ValueError(f"Exam output directory is empty: {exam_root}")
    if any(entry.is_symlink() for entry in exam_root.rglob("*")):
        raise ValueError(f"Refusing to migrate a tree containing symlinks: {exam_root}")

    xml_updates, manifest_updates = _updates_for_tree(
        exam_root,
        exam_name=exam_name,
        year=year,
        top_category=top_category,
    )
    if not apply:
        return len(xml_updates), len(manifest_updates)

    destination.mkdir()
    moved_entries: list[Path] = []
    original_files: dict[Path, bytes] = {}
    try:
        for entry in entries:
            os.replace(entry, destination / entry.name)
            moved_entries.append(entry)

        xml_updates, manifest_updates = _updates_for_tree(
            destination,
            exam_name=exam_name,
            year=year,
            top_category=top_category,
        )
        file_updates = xml_updates + manifest_updates
        original_files = {path: path.read_bytes() for path, _ in file_updates}
        for path, content in file_updates:
            _write_atomically(path, content)

        verified_xml, verified_manifests = _updates_for_tree(
            destination,
            exam_name=exam_name,
            year=year,
            top_category=top_category,
        )
        for path, content in verified_xml + verified_manifests:
            if path.read_bytes() != content:
                raise ValueError(f"Post-migration checkpoint verification failed: {path}")
    except Exception:
        for path, content in original_files.items():
            _write_atomically(path, content)
        for entry in reversed(moved_entries):
            moved_path = destination / entry.name
            if moved_path.exists():
                os.replace(moved_path, entry)
        if destination.exists() and not any(destination.iterdir()):
            destination.rmdir()
        raise
    return len(xml_updates), len(manifest_updates)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--exam", required=True, help="Exam output folder slug, e.g. neet-ug.")
    parser.add_argument("--exam-name", required=True, help="Readable exam name for Moodle categories.")
    parser.add_argument("--year", required=True, help="Four-digit year folder and Moodle category.")
    parser.add_argument("--top-category", required=True)
    parser.add_argument(
        "--previous-exam-name",
        help="Recategorize an existing year folder whose category uses this old exam name.",
    )
    parser.add_argument("--apply", action="store_true", help="Apply the migration; otherwise preview it.")
    args = parser.parse_args(argv)
    try:
        if args.previous_exam_name is not None:
            xml_count, manifest_count = recategorize_exam_outputs(
                args.output_root / args.exam / args.year,
                year=args.year,
                exam_name=args.exam,
                previous_exam_name=args.previous_exam_name,
                top_category=args.top_category,
                apply=args.apply,
            )
        else:
            xml_count, manifest_count = migrate_exam_outputs(
                args.output_root / args.exam,
                year=args.year,
                exam_name=args.exam_name,
                top_category=args.top_category,
                apply=args.apply,
            )
    except (OSError, UnicodeError, ValueError) as exc:
        parser.error(str(exc))
    action = "Migrated" if args.apply else "Would migrate"
    print(
        f"{action} {xml_count} XML file(s) and updated {manifest_count} "
        f"checkpoint manifest(s) under {args.output_root / args.exam / args.year}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
