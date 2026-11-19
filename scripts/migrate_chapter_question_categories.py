"""Migrate mirrored chapter Moodle categories without regenerating questions."""

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path
from xml.dom import minidom

from run_path_mirrored_question_generation import build_output_dir, iter_source_files, language_tokens
from academic_content_pipeline.chapter_output_integrity import chapter_output_record
from academic_content_pipeline.moodle_categories import build_category_path, set_moodle_category
from academic_content_pipeline.moodle_combiner import _is_combined, rebuild_combined_xml


SUFFIX = "_question_types.json"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(data: dict) -> bytes:
    return (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(data)
            handle.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _visible_files(root: Path):
    for folder, directories, filenames in os.walk(root):
        directories[:] = sorted(name for name in directories if not name.startswith("."))
        for name in sorted(filenames):
            if not name.startswith("."):
                path = Path(folder) / name
                if path.suffix.casefold() == ".xml" or name.endswith(SUFFIX):
                    if path.is_symlink() or path.resolve() != path:
                        raise ValueError(f"Unexpected linked output: {path}")
                    yield path


def _plan(source_root: Path, output_root: Path, top_category: str, staged: Path | None):
    sources = defaultdict(list)
    for source in iter_source_files(source_root):
        sources[build_output_dir(source_root, source, output_root)].append(source)
    owned: set[Path] = set()
    existing_combined: list[Path] = []
    files = list(_visible_files(output_root))
    changed = {"xml": 0, "checkpoints": 0}
    for path in (path for path in files if path.name.endswith(SUFFIX)):
        candidates = [
            source for source in sources[path.parent]
            if path.name.startswith(source.stem + "_")
        ]
        if len(candidates) != 1:
            raise ValueError(f"Cannot uniquely map checkpoint to its chapter source: {path}")
        source = candidates[0]
        original = path.read_bytes()
        manifest = json.loads(original)
        names, completed = manifest["files"], manifest["completed_files"]
        if (
            manifest["status"] not in {"completed", "in_progress"}
            or len(set(names)) != len(names)
            or not set(completed) <= set(names)
            or (manifest["status"] == "completed" and names != completed)
        ):
            raise ValueError(f"Invalid chapter checkpoint: {path}")
        counts = {
            language_tokens(entry["type"])[0]: entry["count"]
            for entry in manifest["question_types"]
        }
        primary = path.name.removesuffix(SUFFIX)
        languages = manifest["languages"]
        language_suffix = "_" + "_".join(languages)
        if not primary.endswith(language_suffix):
            raise ValueError(f"Checkpoint filename does not match its languages: {path}")
        base = primary.removesuffix(language_suffix)
        variants = {primary: languages}
        if manifest["translate_to"]:
            variants[base + "_english"] = ["english"]
            variants.update({
                f"{base}_english_{target}": ["english", target]
                for target in manifest["translate_to"]
            })
        expected = {
            f"{prefix}_{kind}.xml": (
                build_category_path(top_category, source_root, source, langs), count
            )
            for prefix, langs in variants.items()
            for kind, count in counts.items()
        }
        if set(names) != expected.keys():
            raise ValueError(f"Checkpoint filenames do not match language/type allocations: {path}")
        saved = manifest.get("output_integrity")
        if "output_integrity" in manifest and (
            not isinstance(saved, dict) or set(saved) != set(completed)
        ):
            raise ValueError(f"Invalid checkpoint integrity metadata: {path}")
        records = {}
        for name in names:
            xml = path.parent / name
            if xml in owned or xml.is_symlink():
                raise ValueError(f"Duplicate or linked checkpoint output: {xml}")
            owned.add(xml)
            if not xml.exists() and name not in completed:
                continue
            category, count = expected[name]
            data = xml.read_bytes()
            actual = chapter_output_record(xml, None, count)
            if saved is not None and name in saved:
                if saved[name]["sha256"] != actual["sha256"]:
                    raise ValueError(f"Checkpoint hash mismatch: {xml}")
                chapter_output_record(xml, saved[name]["category_path"], count)
            document = minidom.parseString(data)
            elements = [
                node for node in document.documentElement.childNodes
                if node.nodeType == node.ELEMENT_NODE
            ]
            markers = [node for node in elements if node.getAttribute("type") == "category"]
            if len(markers) > 1:
                raise ValueError(f"Multiple category markers in individual output: {xml}")
            category_text = markers[0].getElementsByTagName("text") if markers else []
            existing = "".join(
                node.data for node in category_text[0].childNodes
                if node.nodeType in (node.TEXT_NODE, node.CDATA_SECTION_NODE)
            ) if category_text else None
            if markers and (existing != category or elements[0] is not markers[0]):
                legacy = category.replace(f"/top/{top_category}/", "/top/", 1)
                if existing not in {category, legacy}:
                    raise ValueError(f"Unexpected existing category {existing!r}: {xml}")
            updated = data if existing == category and elements[0] is markers[0] else (
                set_moodle_category(data.decode("utf-8-sig"), category).encode("utf-8")
            )
            if updated != data:
                new_document = minidom.parseString(updated)
                def questions(doc):
                    return [
                        node.toxml() for node in doc.documentElement.childNodes
                        if node.nodeType == node.ELEMENT_NODE
                        and node.getAttribute("type") != "category"
                    ]
                if questions(document) != questions(new_document):
                    raise ValueError(f"Question payload changed: {xml}")
                changed["xml"] += 1
            if staged is not None:
                destination = staged / xml.relative_to(output_root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(updated)
                chapter_output_record(destination, category, count)
            if name in completed:
                records[name] = {"category_path": category, "sha256": _digest(updated)}
        if saved != records:
            manifest["output_integrity"] = records
            changed["checkpoints"] += 1
        if staged is not None:
            destination = staged / path.relative_to(output_root)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(_json(manifest) if saved != records else original)
    for path in files:
        if path.suffix.casefold() != ".xml" or path in owned:
            continue
        if _is_combined(path):
            existing_combined.append(path)
            if staged is not None:
                destination = staged / path.relative_to(output_root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, destination)
        elif minidom.parse(str(path)).documentElement.tagName == "quiz":
            raise ValueError(f"Unmapped Moodle XML; no checkpoint identifies its source: {path}")
    return changed, existing_combined


def _publish(output_root: Path, backup: Path, journal: dict) -> None:
    if journal["output_root"] != str(output_root):
        raise ValueError("Migration journal belongs to another output directory.")
    for name, (before, after) in journal["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Invalid migration journal path: {name}")
        path = output_root / relative
        if path.is_symlink() or path.resolve() != path:
            raise ValueError(f"Unexpected linked output: {path}")
        current = _digest(path.read_bytes()) if path.exists() else None
        if current == after:
            continue
        if current != before:
            raise ValueError(f"Output changed after staging; refusing to overwrite: {path}")
        if after is None:
            path.unlink()
        else:
            data = (backup / "staged" / output_root.name / relative).read_bytes()
            if _digest(data) != after:
                raise ValueError(f"Staged file hash mismatch: {path}")
            _atomic_write(path, data)


def migrate(source_root: Path, output_root: Path, top_category: str, backup: Path | None):
    source_root, output_root = source_root.resolve(), output_root.resolve()
    if not source_root.is_dir() or not output_root.is_dir():
        raise ValueError("Source and output roots must be existing directories.")
    if backup is None:
        return _plan(source_root, output_root, top_category, None)[0]
    backup = backup.resolve()
    if any(backup.is_relative_to(root) or root.is_relative_to(backup)
           for root in (source_root, output_root)):
        raise ValueError("Backup must be outside and separate from both repositories.")
    journal_path = backup / "journal.json"
    if journal_path.is_file():
        journal = json.loads(journal_path.read_bytes())
        if journal["source_root"] != str(source_root) or journal["category"] != top_category:
            raise ValueError("Migration journal settings differ.")
    else:
        backup.mkdir(parents=True)
        staged = backup / "staged" / output_root.name
        staged.mkdir(parents=True)
        changed, combined = _plan(source_root, output_root, top_category, staged)
        rebuilt = rebuild_combined_xml(staged)
        changed["combined"] = len(rebuilt)
        paths = {path.relative_to(staged) for path in _visible_files(staged)}
        paths.update(path.relative_to(output_root) for path in combined)
        journal = {
            "source_root": str(source_root), "output_root": str(output_root),
            "category": top_category, "counts": changed, "files": {},
        }
        for relative in sorted(paths):
            original = output_root / relative
            replacement = staged / relative
            before = original.read_bytes() if original.exists() else None
            after = replacement.read_bytes() if replacement.exists() else None
            if before == after:
                continue
            if before is not None:
                saved = backup / "original" / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                saved.write_bytes(before)
            journal["files"][relative.as_posix()] = [
                _digest(before) if before is not None else None,
                _digest(after) if after is not None else None,
            ]
        _atomic_write(journal_path, _json(journal))
    _publish(output_root, backup, journal)
    return journal["counts"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--top-category", default="chapterwise-question-bank")
    parser.add_argument("--backup-root", type=Path, help="Apply with a journal and backups here.")
    args = parser.parse_args()
    print(migrate(args.source_root, args.output_root, args.top_category, args.backup_root))


if __name__ == "__main__":
    main()
