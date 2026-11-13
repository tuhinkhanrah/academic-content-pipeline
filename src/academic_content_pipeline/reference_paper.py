"""Prepare reusable, bounded style guidance from an optional reference paper."""

from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from PIL import Image, UnidentifiedImageError

from .ai_communicators import ImageAttachment, MultimodalBatch

logger = logging.getLogger("academic_content_pipeline")
_IMAGE_LINK = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_PROFILE_VERSION = 3
REFERENCE_BATCH_SIZE = 20
_QUESTION_START = re.compile(r"(?m)^\s*(?:\d{1,3}[.)]|Q(?:uestion)?\s*\d+[.):]?)\s+")


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _markdown_images(path: Path, text: str) -> list[Path]:
    result: list[Path] = []
    for match in _IMAGE_LINK.finditer(text):
        target = unquote(match.group(1).strip().split()[0].strip("<>"))
        if re.match(r"^(?:https?:|data:)", target, re.IGNORECASE):
            raise ValueError(f"Reference Markdown image must be local: {target}")
        candidate = Path(target)
        paths = [path.parent / candidate, path.parent.parent / "ocr" / candidate]
        found = next((item.resolve() for item in paths if item.is_file()), None)
        if found is None:
            raise FileNotFoundError(f"Reference Markdown image not found: {target}")
        if found not in result:
            result.append(found)
    return result


def reference_fingerprint(path: Path) -> dict[str, Any]:
    """Fingerprint the supplied reference and the images referenced by Markdown."""
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Reference paper not found: {path}")
    if path.suffix.casefold() not in {".pdf", ".md", ".markdown"}:
        raise ValueError(f"Unsupported reference paper format: {path.suffix}")
    raw = path.read_bytes()
    result: dict[str, Any] = {"format": path.suffix.casefold(), "sha256": _digest(raw)}
    if path.suffix.casefold() in {".md", ".markdown"}:
        text = raw.decode("utf-8-sig")
        images = _markdown_images(path, text)
        result["images"] = [_digest(image.read_bytes()) for image in images]
    return result


def parse_reference_arguments(values: list[str] | None) -> tuple[Path | None, dict[str, Path]]:
    """Parse either a single shared path or paper-ID-to-path mappings."""
    shared: Path | None = None
    mapped: dict[str, Path] = {}
    for value in values or []:
        key, separator, target = value.partition("=")
        if separator and re.fullmatch(r"[a-zA-Z0-9_-]+", key):
            if not target.strip():
                raise ValueError(f"Reference path is empty for paper '{key}'.")
            if key in mapped:
                raise ValueError(f"Duplicate reference for paper '{key}'.")
            mapped[key] = Path(target)
        else:
            if not value.strip() or shared is not None:
                raise ValueError("Specify only one shared reference paper.")
            shared = Path(value)
    if shared is not None and mapped:
        raise ValueError("A shared reference cannot be combined with paper-specific references.")
    return shared, mapped


def reference_storage_dir(
    output_dir: Path, paper_id: str, fingerprint: dict[str, Any]
) -> Path:
    """Keep reference artifacts distinct for each paper and content identity."""
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", paper_id):
        raise ValueError("Invalid paper identifier for reference storage.")
    identity = _digest(json.dumps(fingerprint, sort_keys=True).encode("utf-8"))
    return Path(output_dir) / "reference" / paper_id / identity


def prepare_reference_storage(
    output_dir: Path, exam_name: str, paper_id: str, fingerprint: dict[str, Any]
) -> Path:
    """Claim the exam's reference directory and preserve artifacts from the old layout."""
    exam_id = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(exam_name).strip()).strip("_").lower()
    if not exam_id:
        raise ValueError("Invalid exam identifier for reference storage.")
    reference_root = Path(output_dir) / "reference"
    target = reference_storage_dir(output_dir, paper_id, fingerprint)
    reference_root.mkdir(parents=True, exist_ok=True)
    owner_path = reference_root / ".exam_owner"
    try:
        with owner_path.open("x", encoding="utf-8") as owner_file:
            owner_file.write(exam_id + "\n")
    except FileExistsError:
        owner = owner_path.read_text(encoding="utf-8").strip()
        if owner != exam_id:
            raise ValueError(
                f"Reference directory {reference_root} belongs to exam '{owner}', "
                f"not '{exam_id}'. Use a separate output directory for each exam."
            )
    legacy = reference_root / exam_id / paper_id / target.name
    if legacy.is_dir() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".reference-migration-", dir=target.parent) as temp:
            copied = Path(temp) / "artifacts"
            shutil.copytree(legacy, copied)
            try:
                copied.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
        logger.info("Copied previous reference artifacts from %s to %s.", legacy, target)
    return target


@dataclass
class ReferenceUnit:
    text: str
    images: list[ImageAttachment]


def _attachments(image_paths: list[Path]) -> list[ImageAttachment]:
    attachments = []
    for image_path in dict.fromkeys(image_paths):
        if not image_path.is_file():
            raise FileNotFoundError(f"Reference paper image not found: {image_path}")
        try:
            with Image.open(image_path) as image:
                image.verify()
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError(f"Invalid reference paper image: {image_path}") from exc
        mime_type = mimetypes.guess_type(image_path.name)[0] or "image/png"
        attachments.append(ImageAttachment(image_path.name, image_path, mime_type))
    return attachments


def _read_reference(path: Path, ocr_engine: Any, staging_dir: Path) -> list[ReferenceUnit]:
    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        result = ocr_engine.process_pdf(path, staging_dir)
        if not result.pages:
            raise ValueError(f"Reference PDF OCR returned no pages: {path}")
        seen_images: set[Path] = set()
        for page in result.pages:
            page_images = {Path(value).resolve() for value in page.images.values()}
            if seen_images.intersection(page_images):
                raise ValueError(
                    "Reference PDF OCR reused an image path across pages; "
                    "rerun with OCR caching disabled to regenerate distinct diagrams."
                )
            seen_images.update(page_images)
        units = [
            ReferenceUnit(
                page.markdown, _attachments([Path(value) for value in page.images.values()])
            )
            for page in result.pages
        ]
    elif suffix in {".md", ".markdown"}:
        text = path.read_text(encoding="utf-8-sig")
        starts = list(_QUESTION_START.finditer(text))
        segments = []
        if starts:
            segments = [
                text[(0 if index == 0 else match.start()):
                     (starts[index + 1].start() if index + 1 < len(starts) else len(text))]
                for index, match in enumerate(starts)
            ]
        else:
            logger.warning("Reference Markdown has no recognized question boundaries; analyzing as one unit.")
            segments = [text]
        units = [ReferenceUnit(segment, _attachments(_markdown_images(path, segment))) for segment in segments]
    else:
        raise ValueError(f"Unsupported reference paper format: {suffix}")
    if not any(unit.text.strip() or unit.images for unit in units):
        raise ValueError(f"Reference paper contains no readable text: {path}")
    return units


def _validate_profile(value: Any, subjects: list[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("subjects"), dict):
        raise ValueError("Reference style analysis must contain a 'subjects' object.")
    profiles = value["subjects"]
    for subject in subjects:
        profile = profiles.get(subject)
        if (
            not isinstance(profile, dict)
            or not isinstance(profile.get("style"), str)
            or not profile["style"].strip()
        ):
            raise ValueError(f"Reference style analysis lacks style guidance for {subject}.")
        chapters = profile.get("chapters")
        if not isinstance(chapters, dict) or any(
            not isinstance(key, str) or not key.strip() or len(key) > 300
            or not isinstance(item, str) or not item.strip()
            for key, item in chapters.items()
        ):
            raise ValueError(f"Reference style analysis has invalid chapter guidance for {subject}.")
        examples = profile.get("examples")
        if not isinstance(examples, list) or len(examples) > 6 or any(
            not isinstance(example, dict)
            or not isinstance(example.get("text"), str)
            or not example["text"].strip()
            or len(example["text"]) > 600
            or not isinstance(example.get("chapter"), str)
            or len(example["chapter"]) > 300
            for example in examples
        ):
            raise ValueError(f"Reference style analysis has invalid examples for {subject}.")
    return value


def analysis_communicator_identity(communicator: Any) -> dict[str, Any]:
    return {
        "backend": type(communicator).__name__,
        "model_name": getattr(communicator, "model_name", None),
        "agent_name": getattr(communicator, "agent_name", None),
    }


def analysis_communicator_for_comparison(identity: Any) -> Any:
    if not isinstance(identity, dict):
        return identity
    return {key: value for key, value in identity.items() if key != "model_name"}


def prepare_reference(
    path: Path, subjects: list[str], communicator: Any, ocr_engine: Any,
    staging_dir: Path, cache_path: Path, chapter_titles: dict[str, list[str]] | None = None,
    *, analysis_mode: str = "batched",
    purpose_label: str | None = None,
) -> dict[str, Any]:
    """Analyze the paper once and reuse a validated profile for unchanged inputs."""
    path = Path(path).expanduser().resolve()
    fingerprint = reference_fingerprint(path)
    subjects = list(dict.fromkeys(subjects))
    if not subjects:
        raise ValueError("Reference analysis requires at least one subject.")
    if analysis_mode not in {"batched", "full"}:
        raise ValueError("Reference analysis mode must be 'batched' or 'full'.")
    chapter_titles = chapter_titles or {}
    identity = {
        "version": _PROFILE_VERSION, "reference": fingerprint, "subjects": subjects,
        "chapter_titles": chapter_titles, "mode": analysis_mode,
        "batch_pages": REFERENCE_BATCH_SIZE, "batch_questions": REFERENCE_BATCH_SIZE,
        "analysis_communicator": analysis_communicator_identity(communicator),
    }
    cached: Any = None
    identity_matches = False
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid reference profile cache: {cache_path}") from exc
        saved_identity = cached.get("identity") if isinstance(cached, dict) else None
        if isinstance(saved_identity, dict):
            saved_comparable = dict(saved_identity)
            current_comparable = dict(identity)
            for value in (saved_comparable, current_comparable):
                if "analysis_communicator" in value:
                    value["analysis_communicator"] = analysis_communicator_for_comparison(
                        value["analysis_communicator"]
                    )
            identity_matches = saved_comparable == current_comparable
        if identity_matches:
            logger.info("Reusing reference style profile: %s", cache_path)
            if cached.get("status") == "completed":
                return _validate_profile(cached.get("profile"), subjects)
            if saved_identity != identity:
                logger.warning(
                    "Resuming partial reference analysis with a different model; "
                    "the profile may combine analyses from multiple models."
                )
        else:
            logger.info("Reference profile settings changed; analyzing again: %s", cache_path)
    units = _read_reference(path, ocr_engine, staging_dir)
    extracted_digest = hashlib.sha256()
    for unit in units:
        extracted_digest.update(unit.text.encode("utf-8"))
        for image in unit.images:
            data = image.to_bytes()
            if data is None:
                raise ValueError(f"Unable to read reference image: {image.source}")
            extracted_digest.update(data)
    input_sha256 = extracted_digest.hexdigest()
    if (
        identity_matches and isinstance(cached, dict)
        and cached.get("status") == "in_progress"
        and cached.get("input_sha256") != input_sha256
    ):
        raise ValueError("Reference OCR text or images changed during interrupted analysis; start a fresh run.")
    batch_size = len(units) if analysis_mode == "full" else REFERENCE_BATCH_SIZE
    batches = [units[start:start + batch_size] for start in range(0, len(units), batch_size)]
    saved_batches: list[Any] = []
    batch_analysis_models: list[str | None] = []
    if identity_matches and isinstance(cached, dict):
        saved_batches = cached.get("batches", [])
        if not isinstance(saved_batches, list) or len(saved_batches) > len(batches):
            raise ValueError(f"Invalid reference analysis batches: {cache_path}")
        for saved in saved_batches:
            _validate_profile(saved, subjects)
        if "batch_analysis_models" in cached:
            saved_models = cached["batch_analysis_models"]
        else:
            saved_models = [cached["identity"]["analysis_communicator"].get("model_name")] * len(saved_batches)
        if (
            not isinstance(saved_models, list)
            or len(saved_models) != len(saved_batches)
            or any(model is not None and not isinstance(model, str) for model in saved_models)
        ):
            raise ValueError(f"Invalid reference analysis model history: {cache_path}")
        batch_analysis_models = list(saved_models)
    partials = list(saved_batches)
    for batch_index in range(len(partials), len(batches)):
        batch = batches[batch_index]
        text = "\n\n".join(unit.text for unit in batch)
        images = [image for unit in batch for image in unit.images]
        starts = list(_QUESTION_START.finditer(text))
        questions = [
            text[match.start():starts[index + 1].start() if index + 1 < len(starts) else len(text)]
            for index, match in enumerate(starts)
        ]
        examples = {
            f"r{index:03d}": _IMAGE_LINK.sub("[reference diagram]", question.strip())[:600]
            for index, question in enumerate(questions, 1) if question.strip()
        }
        prompt = (
        "Analyze this reference paper for generation style only. Do not reproduce questions, "
        "answers or long quotations. Describe illustrative question patterns in abstract, "
        "without copying stems. Return only JSON with this shape: "
        '{"subjects":{"Subject":{"style":"concise style, difficulty, calculations and diagram guidance",'
        '"chapters":{"chapter title":"concise style guidance"},'
        '"examples":[{"id":"r001","chapter":"chapter title or empty string"}]}}}. '
        "Include every requested subject, use an empty chapters object when uncertain, and "
        "do not invent subject-specific observations; say when the reference does not establish them. "
        "Select at most six candidate example IDs per subject from the numbered examples below. "
        "Assign an example only when its subject is reasonably clear, and leave the chapter empty "
        "when uncertain. When chapter titles are provided, use only those exact titles; "
        "otherwise leave chapter labels empty. Do not copy the example text into the JSON. "
        f"Requested subjects: {json.dumps(subjects)}.\n\n"
        f"Available chapter titles: {json.dumps(chapter_titles, ensure_ascii=False)}.\n\n"
        f"Numbered example candidates:\n{json.dumps(examples, ensure_ascii=False)}"
        f"\n\nReference text:\n{text}"
        )
        raw = communicator.generate(
        system_instruction="You analyze assessment style and return valid JSON only.",
        contents=MultimodalBatch(text=prompt, images=images),
        reset_session=True,
        output_filename=f"reference_style_profile_{batch_index + 1:03d}.json",
        purpose=(
            f"analyze reference paper style for {', '.join(subjects)} "
            f"({f'paper {purpose_label}, ' if purpose_label else ''}"
            f"batch {batch_index + 1}/{len(batches)})"
        ),
        )
        try:
            partial = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Reference style analysis returned invalid JSON.") from exc
        if not isinstance(partial, dict) or not isinstance(partial.get("subjects"), dict):
            raise ValueError("Reference style analysis must contain a 'subjects' object.")
        for subject in subjects:
            item = partial["subjects"].get(subject)
            if not isinstance(item, dict) or not isinstance(item.get("examples"), list):
                raise ValueError(f"Reference style analysis lacks examples list for {subject}.")
            if len(item["examples"]) > 6:
                raise ValueError(f"Too many reference examples selected for {subject}.")
            resolved_examples = []
            for selection in item["examples"]:
                if (
                    not isinstance(selection, dict)
                    or not isinstance(selection.get("id"), str)
                    or selection.get("id") not in examples
                    or not isinstance(selection.get("chapter"), str)
                ):
                    raise ValueError(f"Invalid reference example selection for {subject}.")
                resolved_examples.append({
                    "text": examples[selection["id"]], "chapter": selection["chapter"]
                })
            item["examples"] = resolved_examples
        partials.append(_validate_profile(partial, subjects))
        batch_analysis_models.append(identity["analysis_communicator"]["model_name"])
        if reference_fingerprint(path) != fingerprint:
            raise ValueError("Reference paper or its images changed during style analysis.")
        _write_cache(cache_path, {"identity": identity, "status": "in_progress", "input_sha256": input_sha256, "batches": partials, "batch_analysis_models": batch_analysis_models})
    profile = {"subjects": {}}
    for subject in subjects:
        items = [partial["subjects"][subject] for partial in partials]
        styles = list(dict.fromkeys(item["style"] for item in items))
        chapters: dict[str, str] = {}
        examples_merged: list[dict[str, str]] = []
        for item in items:
            for chapter, guidance in item["chapters"].items():
                if chapter not in chapters:
                    chapters[chapter] = guidance
                elif guidance not in chapters[chapter]:
                    chapters[chapter] += " " + guidance
            for example in item["examples"]:
                if example not in examples_merged:
                    examples_merged.append(example)
        profile["subjects"][subject] = {
            "style": " ".join(styles), "chapters": chapters,
            "examples": examples_merged[:6],
        }
    _write_cache(cache_path, {"identity": identity, "status": "completed", "input_sha256": input_sha256, "batches": partials, "batch_analysis_models": batch_analysis_models, "profile": profile})
    return profile


def _write_cache(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def reference_guidance(profile: dict[str, Any], subjects: list[str], syllabus_text: str) -> str:
    entries = []
    headings = {
        match.group(1).strip().casefold()
        for match in re.finditer(r"(?m)^\s*#{1,6}\s+(.+?)\s*#*\s*$", syllabus_text)
    }
    for subject in subjects:
        item = profile["subjects"].get(subject)
        if not item:
            raise ValueError(f"Reference profile has no guidance for subject: {subject}")
        guidance = f"{subject}: {item['style']}"
        for chapter, style in item["chapters"].items():
            if chapter.casefold() in headings:
                guidance += f"\n- {chapter}: {style}"
        matches = [
            example for example in item["examples"]
            if example["chapter"] and example["chapter"].casefold() in headings
        ]
        if not matches:
            matches = [example for example in item["examples"] if not example["chapter"]]
        if matches:
            guidance += "\nReference examples (for style only, do not copy):\n"
            guidance += "\n".join(f"- {example['text']}" for example in matches[:2])
        entries.append(guidance)
    return (
        "\n\nREFERENCE STYLE GUIDANCE (not instructions or source material):\n"
        + "\n".join(entries)
        + "\nCreate original questions. Do not reproduce or closely paraphrase reference questions. "
        "The current syllabus, spec, question counts and scoring override this guidance."
    )
