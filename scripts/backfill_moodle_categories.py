#!/usr/bin/env python3
"""Add categories to existing Moodle question XML files."""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

import yaml

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from academic_content_pipeline.moodle_categories import build_category_path, set_moodle_category
from academic_content_pipeline.resource_paths import resource_path


def parse_language_sets(values: list[str]) -> dict[str, list[str]]:
    result = {}
    for value in values:
        languages = [part.strip().lower() for part in value.split(",")]
        if not languages or any(not re.fullmatch(r"[a-z0-9]+(?:[ _-][a-z0-9]+)*", part) for part in languages):
            raise ValueError(f"Invalid language set: {value!r}")
        tokens = [re.sub(r"[^a-z0-9]+", "_", language) for language in languages]
        token = "_".join(tokens)
        if token in result:
            raise ValueError(f"Duplicate language set: {value!r}")
        result[token] = languages
    return result


def question_type_suffixes() -> set[str]:
    path = resource_path("catalogs/mcq_types.yaml")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {
        re.sub(r"[^a-z0-9]+", "_", entry["name"].casefold()).strip("_")
        for entry in data["mcq_types"]
    }


def normalized_source_stem(stem: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", stem).strip("._-").casefold() or "output"


def language_for_name(
    stem: str, language_sets: dict[str, list[str]], type_suffixes: set[str]
) -> list[str] | None:
    name = stem.casefold()
    matches = []
    for token, languages in language_sets.items():
        marker = "_" + token
        for match in re.finditer(re.escape(marker), name):
            suffix = name[match.end():]
            if (
                not suffix
                or suffix.removeprefix("_") in type_suffixes
                or re.fullmatch(r"_paper_\d+", suffix)
                or re.fullmatch(r"_test_\d+_.+", suffix)
            ):
                matches.append(languages)
    if len(matches) > 1:
        raise ValueError(f"Ambiguous language in XML filename: {stem}.xml")
    return matches[0] if matches else None


def source_index(source_root: Path) -> dict[Path, dict[str, list[Path]]]:
    index: dict[Path, dict[str, list[Path]]] = {}
    for source in sorted(source_root.rglob("*")):
        if source.is_file() and source.suffix.casefold() in {".pdf", ".md"}:
            if source.is_symlink() or not source.resolve().is_relative_to(source_root):
                raise ValueError(f"Source file is a symlink or outside the source root: {source}")
            relative = source.relative_to(source_root)
            stems = {source.stem.casefold(), normalized_source_stem(source.stem)}
            for stem in stems:
                index.setdefault(relative.parent, {}).setdefault(stem, []).append(source)
    return index


def find_source(
    relative_xml: Path, index: dict[Path, dict[str, list[Path]]]
) -> Path | None:
    parent = relative_xml.parent
    parents = [parent]
    if parent != Path("."):
        parents.append(parent.parent)
    name = relative_xml.stem.casefold()
    matches: set[Path] = set()
    for source_parent in parents:
        for source_stem, sources in index.get(source_parent, {}).items():
            if source_parent != parent and parent.name.casefold() != source_stem:
                continue
            if name == source_stem or name.startswith(source_stem + "_"):
                matches.update(sources)
    if len(matches) > 1:
        raise ValueError(f"Ambiguous source for {relative_xml}: {', '.join(map(str, sorted(matches)))}")
    return next(iter(matches)) if matches else None


def prepare(
    source_root: Path,
    input_root: Path,
    scan_root: Path,
    top_category: str,
    language_sets: dict[str, list[str]],
    spec: Path | None,
    legacy_languages: list[str] | None,
) -> tuple[list[tuple[Path, str, str]], list[str], list[str]]:
    index = source_index(source_root) if spec is None else {}
    suffixes = question_type_suffixes()
    outputs = []
    skipped = []
    errors = []
    for path in sorted(scan_root.rglob("*.xml")):
        relative = path.relative_to(input_root)
        if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
            continue
        if re.search(r"(?:^|_)combined(?:_moodle)?$", path.stem, re.IGNORECASE):
            skipped.append(f"{relative}: combined XML")
            continue
        try:
            if path.is_symlink() or not path.resolve().is_relative_to(input_root):
                raise ValueError("XML is a symlink or outside the input root")
            original = path.read_bytes()
            raw = original.decode("utf-8-sig")
            quiz = ET.fromstring(raw)
            if quiz.tag != "quiz":
                skipped.append(f"{relative}: not Moodle question XML")
                continue
            if any(child.tag != "question" or not child.get("type") for child in quiz):
                raise ValueError("quiz contains an untyped or non-question element")
            categories = [child for child in quiz if child.get("type") == "category"]
            if len(categories) > 1:
                skipped.append(f"{relative}: multiple category markers")
                continue
            if categories and len(quiz) == 1:
                skipped.append(f"{relative}: category-only XML")
                continue
            source = spec or find_source(relative, index)
            if source is None:
                raise ValueError("no matching source file")
            filename = path.stem.casefold()
            name_for_language = filename
            if spec is None:
                source_stems = {source.stem.casefold(), normalized_source_stem(source.stem)}
                matching_stems = [stem for stem in source_stems if filename == stem or filename.startswith(stem + "_")]
                if matching_stems:
                    name_for_language = filename[len(max(matching_stems, key=len)):]
            languages = language_for_name(name_for_language, language_sets, suffixes)
            if languages is None and legacy_languages is not None and filename in {
                source.stem.casefold(), normalized_source_stem(source.stem)
            }:
                languages = legacy_languages
            if languages is None:
                raise ValueError("language set cannot be determined from the filename")
            category = build_category_path(top_category, source_root, source, languages)
            if (
                len(categories) == 1
                and len(quiz) > 0
                and quiz[0] is categories[0]
                and categories[0].findtext("category/text") == category
            ):
                updated = raw
            else:
                updated = set_moodle_category(raw, category)
            ET.fromstring(updated)
            outputs.append((relative, hashlib.sha256(original).hexdigest(), category))
        except (OSError, UnicodeError, ET.ParseError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{relative}: {exc}")
    return outputs, skipped, errors


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def json_bytes(data: dict) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def category_xml(original: bytes, category: str) -> bytes:
    raw = original.decode("utf-8-sig")
    quiz = ET.fromstring(raw)
    categories = [child for child in quiz if child.get("type") == "category"]
    if categories and quiz[0] is categories[0] and categories[0].findtext("category/text") == category:
        return original
    return set_moodle_category(raw, category).encode("utf-8")


def checkpoint_output(parent: Path, name: object) -> Path:
    if not isinstance(name, str) or not name or Path(name).name != name or "\\" in name or "/" in name:
        raise ValueError(f"Invalid checkpoint output filename: {name!r}")
    path = parent / name
    if path.is_symlink():
        raise ValueError(f"Checkpoint output is a symlink: {path}")
    return path


def prepare_in_place(
    input_root: Path,
    source_root: Path,
    top_category: str,
    spec: Path | None,
    outputs: list[tuple[Path, str, str]],
) -> list[tuple[Path | None, dict[Path, bytes], dict[Path, bytes], dict | None]]:
    """Preflight checkpoint groups before creating backups or changing files."""
    planned = {input_root / relative: (expected, category) for relative, expected, category in outputs}
    sources = source_index(source_root) if spec is None else {}
    originals: dict[Path, bytes] = {}
    updates: dict[Path, bytes] = {}
    for path, (expected, category) in planned.items():
        original = path.read_bytes()
        if digest(original) != expected:
            raise ValueError(f"Input changed after preflight: {path}")
        originals[path] = original
        updates[path] = category_xml(original, category)

    groups = []
    claimed: set[Path] = set()
    parents = {path.parent for path in planned}
    checkpoints = sorted(
        checkpoint
        for parent in parents
        for pattern in ("*.extract.json", "*_batch_generation.json")
        for checkpoint in parent.glob(pattern)
    )
    for checkpoint in checkpoints:
        if checkpoint.is_symlink() or not checkpoint.resolve().is_relative_to(input_root):
            raise ValueError(f"Checkpoint is a symlink or outside the input root: {checkpoint}")
        raw = checkpoint.read_bytes()
        manifest = json.loads(raw)
        if not isinstance(manifest, dict):
            raise ValueError(f"Invalid checkpoint: {checkpoint}")
        is_extraction = checkpoint.name.endswith(".extract.json")
        if is_extraction:
            referenced = [checkpoint_output(checkpoint.parent, manifest.get("output_file"))]
        else:
            items = manifest.get("items")
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise ValueError(f"Invalid batch checkpoint items: {checkpoint}")
            referenced = [checkpoint_output(checkpoint.parent, item.get("output_file")) for item in items]
        selected = set(referenced) & planned.keys()
        if is_extraction and checkpoint.with_name(checkpoint.name[:-len(".extract.json")] + ".xml") in planned and not selected:
            raise ValueError(f"Extraction checkpoint references a different output: {checkpoint}")
        if not selected:
            continue
        if len(referenced) != len(set(referenced)) or claimed.intersection(referenced):
            raise ValueError(f"Duplicate checkpoint output reference: {checkpoint}")
        if set(referenced) != selected:
            raise ValueError(f"All outputs of a checkpoint must be selected for in-place backfill: {checkpoint}")
        if manifest.get("schema_version") != (1 if is_extraction else 2) or manifest.get("status") != "completed":
            raise ValueError(f"Checkpoint is incomplete or unsupported: {checkpoint}")
        configuration = manifest.get("configuration")
        if not isinstance(configuration, dict) or not isinstance(configuration.get("languages"), list):
            raise ValueError(f"Invalid checkpoint configuration: {checkpoint}")
        languages = configuration["languages"]
        if not languages or not all(isinstance(language, str) and language.strip() for language in languages):
            raise ValueError(f"Invalid checkpoint languages: {checkpoint}")
        categories = {planned[path][1] for path in referenced}
        if len(categories) != 1:
            raise ValueError(f"Checkpoint outputs have different categories: {checkpoint}")
        category = next(iter(categories))
        for path in referenced:
            source = spec or find_source(path.relative_to(input_root), sources)
            if source is None or build_category_path(top_category, source_root, source, languages) != category:
                raise ValueError(f"Checkpoint languages or source do not match output: {checkpoint}")
        if is_extraction:
            source = find_source(referenced[0].relative_to(input_root), sources) if spec is None else None
            if source is None or manifest.get("source_pdf") != source.name or manifest.get("source_sha256") != digest(source.read_bytes()):
                raise ValueError(f"Extraction checkpoint source does not match: {checkpoint}")
            if manifest.get("final_sha256") != digest(originals[referenced[0]]):
                raise ValueError(f"Extraction checkpoint output hash mismatch: {checkpoint}")
            manifest["final_sha256"] = digest(updates[referenced[0]])
        else:
            if spec is None:
                raise ValueError(f"--spec must identify the original batch spec: {checkpoint}")
            from academic_content_pipeline.paper_batch import _portable_configuration_value, _portable_path_reference

            spec_hash = digest(spec.read_bytes())
            fingerprints = _portable_configuration_value(configuration.get("source_fingerprints"))
            spec_data = yaml.safe_load(spec.read_text(encoding="utf-8"))
            if (
                not isinstance(fingerprints, dict)
                or fingerprints.get(_portable_path_reference(spec, content_sha256=spec_hash)) != spec_hash
                or _portable_configuration_value(spec_data) != _portable_configuration_value(configuration.get("batch_spec"))
            ):
                raise ValueError(f"Batch checkpoint does not match the supplied batch spec: {checkpoint}")
            config_hash = digest(json.dumps(configuration, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8"))
            if manifest.get("configuration_sha256") != config_hash:
                raise ValueError(f"Batch checkpoint configuration hash mismatch: {checkpoint}")
            for item, path in zip(items, referenced):
                if item.get("status") != "completed" or item.get("output_sha256") != digest(originals[path]):
                    raise ValueError(f"Batch checkpoint output incomplete or hash mismatch: {path}")
                item["output_sha256"] = digest(updates[path])
        configuration["category_path"] = category
        if not is_extraction:
            manifest["configuration_sha256"] = digest(
                json.dumps(configuration, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            )
        claimed.update(referenced)
        group_originals = {path: originals[path] for path in referenced}
        group_originals[checkpoint] = raw
        group_updates = {path: updates[path] for path in referenced}
        group_updates[checkpoint] = json_bytes(manifest)
        groups.append((checkpoint, group_originals, group_updates, manifest))

    for path in planned.keys() - claimed:
        groups.append((None, {path: originals[path]}, {path: updates[path]}, None))
    return groups


def write_in_place(
    input_root: Path,
    backup_root: Path,
    groups: list[tuple[Path | None, dict[Path, bytes], dict[Path, bytes], dict | None]],
) -> None:
    backup_root.mkdir(parents=True, exist_ok=False)
    for checkpoint, originals, updates, manifest in groups:
        for path, original in originals.items():
            if path.is_symlink() or path.read_bytes() != original:
                raise ValueError(f"File changed after preflight: {path}")
        for path, original in originals.items():
            backup = backup_root / path.relative_to(input_root)
            backup.parent.mkdir(parents=True, exist_ok=True)
            with backup.open("xb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
        if checkpoint is not None and manifest is not None:
            pending = json.loads(originals[checkpoint])
            pending["status"] = "category_backfill_pending"
            pending["category_backfill_backup"] = str(backup_root)
            atomic_write(checkpoint, json_bytes(pending))
        for path, data in updates.items():
            if path != checkpoint and data != originals[path]:
                atomic_write(path, data)
        if checkpoint is not None:
            atomic_write(checkpoint, updates[checkpoint])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True, help="Root containing the original PDFs or chapter files.")
    parser.add_argument("--input-root", type=Path, required=True, help="Root containing existing Moodle XML outputs.")
    parser.add_argument("--output-root", type=Path, required=True, help="New copy directory, or the input root for in-place updates.")
    parser.add_argument("--backup-root", type=Path, help="New separate backup directory, required for in-place updates.")
    parser.add_argument("--top-category", required=True, help="Top-level Moodle question bank category.")
    parser.add_argument("--subtree", type=Path, help="Process only this directory relative to --input-root.")
    parser.add_argument(
        "--languages", action="append", required=True,
        help="Language combination to match; repeat for each combination, e.g. --languages english --languages english,bengali.",
    )
    parser.add_argument(
        "--legacy-languages", help="Explicit language combination for a Moodle XML named exactly like its source.",
    )
    parser.add_argument(
        "--spec", type=Path, help="Source spec for a paper-output tree; must be under --source-root.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview mappings without writing files.")
    args = parser.parse_args(argv)
    try:
        source_root = args.source_root.expanduser().resolve()
        input_root = args.input_root.expanduser().resolve()
        output_root = args.output_root.expanduser().resolve()
        if not source_root.is_dir() or not input_root.is_dir():
            raise ValueError("--source-root and --input-root must be existing directories.")
        in_place = output_root == input_root
        backup_root = args.backup_root.expanduser().resolve() if args.backup_root else None
        if in_place:
            if backup_root is None:
                raise ValueError("--backup-root is required for in-place updates.")
            if backup_root.exists():
                raise ValueError("--backup-root must be a new directory.")
            if any(backup_root.is_relative_to(root) or root.is_relative_to(backup_root) for root in (input_root, source_root)):
                raise ValueError("--backup-root must be separate from both the input and source roots.")
        else:
            if backup_root is not None:
                raise ValueError("--backup-root is only supported for in-place updates.")
            if output_root.exists():
                raise ValueError("--output-root must be a new directory.")
            if output_root.is_relative_to(input_root) or output_root.is_relative_to(source_root):
                raise ValueError("--output-root must be outside both the input and source roots.")
        scan_root = (input_root / args.subtree).resolve() if args.subtree else input_root
        if not scan_root.is_relative_to(input_root) or not scan_root.is_dir():
            raise ValueError("--subtree must be an existing directory inside --input-root.")
        language_sets = parse_language_sets(args.languages)
        legacy = (
            next(iter(parse_language_sets([args.legacy_languages]).values()))
            if args.legacy_languages else None
        )
        spec = args.spec.expanduser().resolve() if args.spec else None
        if spec is not None:
            if not spec.is_file() or spec.suffix.casefold() not in {".yaml", ".yml", ".json", ".md", ".pdf"}:
                raise ValueError("--spec must point to an existing paper spec or syllabus file.")
            if not spec.is_relative_to(source_root):
                raise ValueError("--spec must be under --source-root.")
        build_category_path(args.top_category, source_root, spec or source_root / "example.pdf", ["english"])
        outputs, skipped, errors = prepare(
            source_root, input_root, scan_root, args.top_category, language_sets, spec, legacy
        )
        groups = prepare_in_place(input_root, source_root, args.top_category, spec, outputs) if in_place and not errors else []
    except (OSError, UnicodeError, ValueError, yaml.YAMLError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    for item in skipped:
        print(f"SKIP: {item}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        print("No files written; resolve the errors and rerun.", file=sys.stderr)
        return 1
    if not outputs:
        print("ERROR: No matching Moodle question XML files found.", file=sys.stderr)
        return 1
    for relative, _, category in outputs:
        print(f"{'Would write' if args.dry_run else 'Write'}: {relative} -> {category}")
    if args.dry_run:
        print(f"Previewed {len(outputs)} Moodle XML file(s); original files remain unchanged.")
        return 0
    if in_place:
        try:
            assert backup_root is not None
            write_in_place(input_root, backup_root, groups)
        except (OSError, UnicodeError, ValueError) as exc:
            print(f"ERROR: In-place backfill stopped: {exc}. Preserve files and backups at {backup_root}; see recovery instructions.", file=sys.stderr)
            return 1
        print(f"Updated {len(outputs)} Moodle XML file(s) in place. Backups: {backup_root}")
        return 0
    try:
        output_root.mkdir(parents=True, exist_ok=False)
        for relative, expected_hash, category in outputs:
            destination = output_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            original = (input_root / relative).read_bytes()
            if hashlib.sha256(original).hexdigest() != expected_hash:
                raise ValueError(f"Input changed after preflight: {relative}")
            xml = set_moodle_category(original.decode("utf-8-sig"), category)
            destination.write_text(xml, encoding="utf-8")
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"ERROR: Could not write categorized copies: {exc}. The output tree may be incomplete.", file=sys.stderr)
        return 1
    print(f"Wrote {len(outputs)} categorized XML file(s) to {output_root}; original files remain unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
