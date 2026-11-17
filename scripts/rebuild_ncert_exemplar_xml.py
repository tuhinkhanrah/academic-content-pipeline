#!/usr/bin/env python3
"""Rebuild NCERT Exemplar XML offline from saved extraction responses."""

import argparse
import hashlib
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from academic_content_pipeline.content_processors import (
    is_valid_empty_question_response,
    validate_ncert_exemplar_files,
    validate_ncert_exemplar_questions,
    write_json_atomically,
    write_text_atomically,
)
from academic_content_pipeline.pipeline_utils import (
    extract_clean_question_nodes_with_status,
    fix_and_inject_moodle_xml,
)
from academic_content_pipeline.moodle_categories import set_moodle_category


def local_name(value, label):
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
    ):
        raise ValueError(f"Invalid {label}: {value!r}.")
    return value


def cached_images(manifest, directory):
    source = local_name(manifest.get("source_pdf"), "source PDF name")
    if not source.lower().endswith(".pdf"):
        raise ValueError("Checkpoint source is not a PDF.")
    source_hash = manifest.get("source_sha256")
    if not isinstance(source_hash, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", source_hash):
        raise ValueError("Checkpoint has an invalid source PDF hash.")
    configuration = manifest.get("configuration")
    if not isinstance(configuration, dict):
        raise ValueError("Checkpoint has no extraction configuration.")
    page_range = configuration.get("page_range")
    if page_range is None:
        range_token = "full"
    elif (
        isinstance(page_range, list)
        and len(page_range) == 2
        and all(isinstance(value, int) and not isinstance(value, bool) for value in page_range)
    ):
        start, end = sorted(page_range)
        range_token = f"{start}-{end}"
    else:
        raise ValueError("Checkpoint has an invalid page range.")
    cache_key = f"{source_hash}:{range_token}:ocr-v1"
    cache_name = hashlib.sha256(cache_key.encode("utf-8")).hexdigest() + ".json"
    ocr_dir = directory / Path(source).stem / "ocr"
    cache_path = ocr_dir / "cache" / cache_name
    if not cache_path.is_file():
        raise ValueError(f"Matching OCR cache is missing: {cache_path}.")
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    if cache.get("cache_key") != cache_key or cache.get("pdf_hash") != source_hash:
        raise ValueError(f"OCR cache does not match checkpoint: {cache_path}.")
    images = cache.get("images")
    if not isinstance(images, dict):
        raise ValueError(f"OCR cache has no image map: {cache_path}.")
    resolved = {}
    for key, value in images.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value:
            raise ValueError(f"Invalid image entry in {cache_path}.")
        image_path = (ocr_dir / value).resolve()
        if not image_path.is_relative_to(ocr_dir.resolve()):
            raise ValueError(f"OCR image path is outside its cache directory: {value}.")
        resolved[key] = image_path
    return resolved


def missing_image_references(nodes):
    missing = set()
    for node in nodes:
        question = ET.fromstring(node)
        fields = (
            question.findall("questiontext")
            + question.findall("generalfeedback")
            + question.findall("answer")
            + question.findall("subquestion")
            + question.findall("subquestion/answer")
        )
        for field in fields:
            references = set(
                re.findall(r"""@@PLUGINFILE@@/([^"'\s<>?#]+)""", field.findtext("text") or "")
            )
            embedded = {
                file.get("name") for file in field.findall("file") if (file.text or "").strip()
            }
            missing.update(references - embedded)
    return missing


def image_map_for_nodes(nodes, manifest, directory):
    references = missing_image_references(nodes)
    if not references:
        return {}
    images = cached_images(manifest, directory)
    image_map = {}
    for reference in sorted(references, key=lambda value: (len(value), value)):
        image = images.get(reference)
        if image is None or not image.is_file():
            raise ValueError(f"Cached image is missing for {reference!r}.")
        image_map[reference] = str(image)
    # The shared injector searches by partial image ID; refuse ambiguous mappings.
    for reference in references:
        clean_id = reference.replace(".jpeg", "").replace(".jpg", "")
        selected = next((name for name in image_map if clean_id in name), None)
        if selected is None or image_map[selected] != image_map[reference]:
            raise ValueError(f"Ambiguous cached image ID for {reference!r}.")
    return image_map


def prepare_rebuild(path):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("Unsupported extraction checkpoint.")
    output_name = local_name(manifest.get("output_file"), "output filename")
    if not output_name.lower().endswith(".xml"):
        raise ValueError("Checkpoint output is not an XML file.")
    if manifest.get("status") != "completed":
        raise ValueError("Extraction checkpoint is not completed; no complete rebuild is possible.")
    batches = manifest.get("batches")
    batch_ids = manifest.get("batch_ids")
    if (
        not isinstance(batches, list)
        or not batches
        or not isinstance(batch_ids, list)
        or any(not isinstance(batch, dict) for batch in batches)
        or [batch.get("id") for batch in batches] != batch_ids
        or len(set(batch_ids)) != len(batch_ids)
    ):
        raise ValueError("Checkpoint batch list is invalid.")

    batch_dir = path.parent / ("." + path.name.removesuffix(".extract.json") + ".extract_batches")
    nodes = []
    batches_nodes = []
    for batch in batches:
        batch_id = local_name(batch.get("id"), "batch ID")
        if batch.get("status") != "completed":
            raise ValueError(f"Batch {batch_id} is not completed.")
        response_name = local_name(batch.get("response_file"), "response filename")
        response_path = batch_dir / response_name
        response = response_path.read_text(encoding="utf-8")
        if not response.strip():
            raise ValueError(f"Batch {batch_id} has an empty response file.")
        opened = len(re.findall(r"<question(?=[\s>])", response, re.IGNORECASE))
        closed = len(re.findall(r"</question\s*>", response, re.IGNORECASE))
        if opened != closed:
            raise ValueError(f"Batch {batch_id} has incomplete question XML.")
        batch_nodes, parse_error = extract_clean_question_nodes_with_status(response)
        if parse_error or (not batch_nodes and not is_valid_empty_question_response(response)):
            raise ValueError(f"Batch {batch_id} has invalid XML: {parse_error or 'no valid questions'}.")
        expected = batch.get("question_count")
        if isinstance(expected, bool) or not isinstance(expected, int) or len(batch_nodes) != expected:
            raise ValueError(
                f"Batch {batch_id} response has {len(batch_nodes)} questions; checkpoint expects {expected}."
            )
        nodes.extend(batch_nodes)
        batches_nodes.append(batch_nodes)

    validate_ncert_exemplar_questions(nodes)
    image_map = image_map_for_nodes(nodes, manifest, path.parent)
    xml = fix_and_inject_moodle_xml("<quiz>\n" + "\n".join(nodes) + "\n</quiz>", image_map)
    quiz = ET.fromstring(xml)
    validate_ncert_exemplar_files(quiz)
    category_path = manifest.get("configuration", {}).get("category_path")
    if category_path is not None:
        if not isinstance(category_path, str) or not category_path:
            raise ValueError("Invalid category path in extraction checkpoint.")
        xml = set_moodle_category(xml, category_path)
    return manifest, xml, batches_nodes


def rebuild_manifest(path):
    manifest, xml, batches_nodes = prepare_rebuild(path)
    return manifest["output_file"], xml, sum(len(nodes) for nodes in batches_nodes)


def repair_manifest(path, pdf_path, expected_output, output_root, backup_root, *, dry_run=False):
    manifest, xml, batches_nodes = prepare_rebuild(path)
    if manifest.get("source_pdf") != pdf_path.name:
        raise ValueError(f"Checkpoint source PDF does not match {pdf_path.name}.")
    digest = hashlib.sha256()
    with pdf_path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != manifest.get("source_sha256"):
        raise ValueError(f"Source PDF changed since extraction: {pdf_path}.")
    if manifest["output_file"] != expected_output.name:
        raise ValueError(f"Checkpoint output does not match {expected_output.name}.")

    batch_dir = path.parent / ("." + path.name.removesuffix(".extract.json") + ".extract_batches")
    batch_outputs = []
    seen = set()
    response_names = [
        local_name(batch.get("response_file"), "response filename")
        for batch in manifest["batches"]
    ]
    if len(set(response_names)) != len(response_names):
        raise ValueError("Multiple batches reference the same response file.")
    for batch, nodes in zip(manifest["batches"], batches_nodes):
        question_file = local_name(batch.get("questions_file"), "batch XML filename")
        if not question_file.lower().endswith(".xml"):
            raise ValueError(f"Batch checkpoint is not an XML file: {question_file}.")
        if question_file in seen or question_file in response_names:
            raise ValueError(f"Duplicate or conflicting batch checkpoint filename: {question_file}.")
        seen.add(question_file)
        checkpoint = batch_dir / question_file
        checkpoint_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n<quiz>\n'
            + "\n".join(nodes)
            + "\n</quiz>\n"
        )
        batch_outputs.append((checkpoint, checkpoint_xml))

    output_existed = expected_output.exists()
    targets = [path, expected_output] + [checkpoint for checkpoint, _ in batch_outputs]
    for target in targets:
        if target.is_symlink() or not target.resolve().is_relative_to(output_root):
            raise ValueError(f"Repair target is a symlink or outside the output root: {target}.")
        if target != expected_output and not target.is_file():
            raise ValueError(f"Repair target is missing; no files were changed: {target}.")
        if target == expected_output and output_existed and not target.is_file():
            raise ValueError(f"Repair output is not a file: {target}.")
        if target.with_suffix(target.suffix + ".tmp").exists():
            raise ValueError(f"Temporary file already exists for {target}; inspect it before repair.")
    if dry_run:
        return sum(len(nodes) for nodes in batches_nodes)

    for target in targets:
        if target == expected_output and not output_existed:
            continue
        backup = backup_root / target.relative_to(output_root)
        backup.parent.mkdir(parents=True, exist_ok=True)
        if backup.exists():
            raise ValueError(f"Backup already exists: {backup}.")
        shutil.copy2(target, backup)

    backup_info = backup_root / path.relative_to(output_root)
    backup_info = backup_info.with_name(backup_info.name + ".repair-info.json")
    write_json_atomically(backup_info, {
        "output_file": expected_output.name,
        "output_existed": output_existed,
        "batch_files": [checkpoint.name for checkpoint, _ in batch_outputs],
    })
    pending = dict(manifest)
    pending["status"] = "repair_pending"
    pending["repair_backup_root"] = str(backup_root)
    pending["repair_original_output_missing"] = not output_existed
    write_json_atomically(path, pending)
    try:
        for checkpoint, checkpoint_xml in batch_outputs:
            write_text_atomically(checkpoint, checkpoint_xml)
        write_text_atomically(expected_output, xml)
        for (checkpoint, _), nodes in zip(batch_outputs, batches_nodes):
            root = ET.parse(checkpoint).getroot()
            if root.tag != "quiz" or len(root.findall("question")) != len(nodes):
                raise ValueError(f"Repaired batch checkpoint did not validate: {checkpoint}.")
        quiz = ET.parse(expected_output).getroot()
        if quiz.tag != "quiz":
            raise ValueError(f"Repaired XML root is not quiz: {expected_output}.")
        questions = [question for question in quiz.findall("question") if question.get("type") != "category"]
        if len(questions) != sum(len(nodes) for nodes in batches_nodes):
            raise ValueError(f"Repaired XML has an unexpected question count: {expected_output}.")
        validate_ncert_exemplar_questions(
            [ET.tostring(question, encoding="unicode") for question in questions]
        )
        validate_ncert_exemplar_files(quiz)
        manifest["final_sha256"] = hashlib.sha256(expected_output.read_bytes()).hexdigest()
        write_json_atomically(path, manifest)
    except (OSError, UnicodeError, ET.ParseError, ValueError) as exc:
        raise RuntimeError(
            f"Repair interrupted; checkpoint remains repair_pending. "
            f"Restore this chapter's files from {backup_root} before retrying: {exc}"
        ) from exc
    return sum(len(nodes) for nodes in batches_nodes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True, help="Existing bank or backup containing checkpoints.")
    parser.add_argument("--output-root", type=Path, required=True, help="New, separate staging directory.")
    args = parser.parse_args(argv)
    source = args.source_root.resolve()
    output = args.output_root.resolve()
    if not source.is_dir():
        parser.error(f"Source root is not a directory: {source}")
    if output.is_relative_to(source) or source.is_relative_to(output):
        parser.error("Output root must be separate from the source root.")
    if output.exists():
        parser.error(f"Output root already exists; choose a new directory: {output}")

    manifests = sorted(source.rglob("*.extract.json"))
    if not manifests:
        parser.error(f"No extraction checkpoints found under {source}.")
    output.mkdir(parents=True, exist_ok=False)
    errors = []
    rebuilt = 0
    claimed = set()
    for path in manifests:
        try:
            output_name, xml, count = rebuild_manifest(path)
            relative = path.parent.relative_to(source) / output_name
            if relative in claimed:
                raise ValueError(f"Multiple checkpoints target {relative}.")
            claimed.add(relative)
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomically(destination, xml)
            rebuilt += 1
            print(f"OK: {relative} ({count} questions)")
        except (OSError, UnicodeError, json.JSONDecodeError, ET.ParseError, ValueError, TypeError) as exc:
            errors.append(f"{path.relative_to(source)}: {exc}")

    for path in source.rglob("*.xml"):
        relative = path.relative_to(source)
        if (
            "_neet_ug_" in path.name.casefold()
            and not any(part.startswith(".") for part in relative.parts)
            and relative not in claimed
        ):
            errors.append(f"{relative}: no successfully rebuilt checkpoint.")

    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    print(f"Rebuilt {rebuilt} checkpoint(s); {len(errors)} error(s).")
    print("Review source coverage, excluded sections, answers, translations and images before use.")
    print("Rebuilt XMLs do not include extraction checkpoints and cannot be resumed.")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
