"""Format-aware translation of generated English assessment artifacts."""

import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from copy import deepcopy
from html.parser import HTMLParser
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr
from typing import Optional

from .ai_communicators import BaseAICommunicator
from .pipeline_utils import (
    LANG_ISO_MAP,
    build_language_instructions,
    load_prompt_template,
    write_prompt_snapshot,
)

logger = logging.getLogger("academic_content_pipeline")

TRANSLATOR_PROMPT_DIR = Path("prompts") / "translator"
XML_QUESTIONS_PER_TRANSLATION = 5
_SVG_PLACEHOLDER_PREFIX = "ACADEMIC_PIPELINE_SVG_PAYLOAD_"
_FILE_PLACEHOLDER_PREFIX = "ACADEMIC_PIPELINE_FILE_PAYLOAD_"


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _visible_html_text(value: str) -> str:
    class TextExtractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.parts: list[str] = []

        def handle_data(self, data: str) -> None:
            self.parts.append(data)

    parser = TextExtractor()
    parser.feed(value)
    parser.close()
    return _normalize_text(" ".join(parser.parts))


def _visible_moodle_field_text(element: ET.Element) -> str:
    html_fragments = [
        node.text or ""
        for node in element.iter()
        if node.tag == "text"
    ]
    return _visible_html_text(" ".join(html_fragments))


def _serialize_moodle_xml(root: ET.Element) -> str:
    def serialize_element(element: ET.Element) -> str:
        attributes = "".join(
            f" {name}={quoteattr(value)}"
            for name, value in element.attrib.items()
        )
        chunks = [f"<{element.tag}{attributes}>"]
        if element.text:
            if element.tag == "text" and not list(element):
                cdata = element.text.replace("]]>", "]]]]><![CDATA[>")
                chunks.append(f"<![CDATA[{cdata}]]>")
            else:
                chunks.append(escape(element.text))
        for child in element:
            chunks.append(serialize_element(child))
            if child.tail:
                chunks.append(escape(child.tail))
        chunks.append(f"</{element.tag}>")
        return "".join(chunks)

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + serialize_element(root)
        + "\n"
    )


def is_valid_moodle_xml_file(path: Path) -> bool:
    """Return whether a file contains a well-formed Moodle quiz document."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return False
    return root.tag == "quiz" and all(child.tag == "question" for child in root)


def _xml_structure_signature(
    element: ET.Element,
    inside_translatable_field: bool = False,
) -> tuple:
    translatable = inside_translatable_field or element.tag in {
        "questiontext",
        "generalfeedback",
    }
    return (
        element.tag,
        tuple(sorted(element.attrib.items())),
        "TRANSLATABLE" if translatable else _normalize_text(element.text or ""),
        tuple(
            _xml_structure_signature(child, translatable)
            for child in element
        ),
        _normalize_text(element.tail or ""),
    )


def _validate_bilingual_xml(source_xml: str, translated_xml: str) -> None:
    try:
        source_root = ET.fromstring(source_xml)
        translated_root = ET.fromstring(translated_xml)
    except ET.ParseError as exc:
        raise ValueError("Translator returned malformed Moodle XML.") from exc

    if _xml_structure_signature(source_root) != _xml_structure_signature(translated_root):
        raise ValueError(
            "Translator changed Moodle XML structure or non-translatable assessment data."
        )

    source_fields = [
        node
        for node in source_root.iter()
        if node.tag in {"questiontext", "generalfeedback"}
    ]
    translated_fields = [
        node
        for node in translated_root.iter()
        if node.tag in {"questiontext", "generalfeedback"}
    ]
    for source_field, translated_field in zip(source_fields, translated_fields):
        original_text = _visible_moodle_field_text(source_field)
        bilingual_text = _visible_moodle_field_text(translated_field)
        if original_text and original_text not in bilingual_text:
            raise ValueError(
                f"Translator did not preserve the English content in <{source_field.tag}>."
            )
        if not original_text and bilingual_text:
            raise ValueError(
                f"Translator added content to an empty <{source_field.tag}>."
            )


def _strip_embedded_payloads(
    xml_fragment: str,
) -> tuple[str, list[tuple[str, str]], list[tuple[str, str]]]:
    """Remove binary and SVG payloads from an AI request while recording exact originals."""
    root = ET.fromstring(xml_fragment)
    binary_payloads: list[tuple[str, str]] = []
    for file_element in root.iter("file"):
        placeholder = f"{_FILE_PLACEHOLDER_PREFIX}{len(binary_payloads)}__"
        binary_payloads.append((placeholder, file_element.text or ""))
        file_element.text = placeholder

    svg_payloads: list[tuple[str, str]] = []
    svg_pattern = re.compile(r"<svg\b.*?</svg\s*>", re.IGNORECASE | re.DOTALL)
    for element in root.iter():
        for attribute in ("text", "tail"):
            value = getattr(element, attribute)
            if not value or "<svg" not in value.lower():
                continue

            def replace_svg(match: re.Match[str]) -> str:
                placeholder = f"{_SVG_PLACEHOLDER_PREFIX}{len(svg_payloads)}__"
                svg_payloads.append((placeholder, match.group(0)))
                return placeholder

            setattr(element, attribute, svg_pattern.sub(replace_svg, value))
    return _serialize_moodle_xml(root), binary_payloads, svg_payloads


def _restore_embedded_payloads(
    source_xml: str,
    translated_xml: str,
    binary_payloads: list[tuple[str, str]],
    svg_payloads: list[tuple[str, str]],
) -> str:
    translated_root = ET.fromstring(translated_xml)
    translated_files = list(translated_root.iter("file"))
    if len(translated_files) != len(binary_payloads):
        raise ValueError("Translator changed the number of embedded Moodle files.")
    for file_element, (placeholder, payload) in zip(
        translated_files, binary_payloads
    ):
        if placeholder not in (file_element.text or ""):
            raise ValueError("Translator changed an embedded Moodle file placeholder.")
        file_element.text = payload

    text_nodes = [
        (element, attribute)
        for element in translated_root.iter()
        for attribute in ("text", "tail")
        if getattr(element, attribute)
    ]
    for placeholder, svg_markup in svg_payloads:
        matching_nodes = [
            (element, attribute)
            for element, attribute in text_nodes
            if placeholder in getattr(element, attribute)
        ]
        if len(matching_nodes) != 1:
            raise ValueError(
                "Translator did not preserve an embedded SVG placeholder exactly once."
            )
        element, attribute = matching_nodes[0]
        setattr(
            element,
            attribute,
            getattr(element, attribute).replace(placeholder, svg_markup, 1),
        )

    restored_xml = _serialize_moodle_xml(translated_root)
    _validate_bilingual_xml(source_xml, restored_xml)
    return restored_xml


def _translate_moodle_xml_in_chunks(
    communicator: BaseAICommunicator,
    source_xml: str,
    prompt_template: Path,
    system_instruction: str,
    target_language: str,
    language_instruction: str,
    *,
    output_filename: str,
    prompt_snapshot_path: Optional[Path],
    force: bool,
    questions_per_chunk: int,
) -> str:
    if questions_per_chunk < 1:
        raise ValueError("Moodle XML translation chunk size must be at least 1.")

    try:
        source_root = ET.fromstring(source_xml)
    except ET.ParseError as exc:
        raise ValueError("English master is malformed Moodle XML.") from exc

    children = list(source_root)
    if not children:
        raise ValueError("English master Moodle XML contains no questions.")

    translated_root = ET.Element(source_root.tag, dict(source_root.attrib))
    translated_root.text = source_root.text
    chunk_count = (
        len(children) + questions_per_chunk - 1
    ) // questions_per_chunk

    for chunk_index, offset in enumerate(
        range(0, len(children), questions_per_chunk),
        start=1,
    ):
        source_chunk_root = ET.Element(source_root.tag, dict(source_root.attrib))
        source_chunk_root.text = source_root.text if offset == 0 else None
        chunk_children = children[offset : offset + questions_per_chunk]
        for child in chunk_children:
            source_chunk_root.append(deepcopy(child))
        source_chunk = ET.tostring(source_chunk_root, encoding="unicode")
        ai_input, binary_payloads, svg_payloads = _strip_embedded_payloads(
            source_chunk
        )
        prompt = load_prompt_template(
            prompt_template,
            target_language=target_language,
            language_instruction=language_instruction,
            source_artifact=ai_input,
        )
        chunk_snapshot = None
        if prompt_snapshot_path is not None:
            chunk_snapshot = prompt_snapshot_path.with_name(
                f"{prompt_snapshot_path.stem}_part_{chunk_index:03d}_of_{chunk_count:03d}.md"
            )
        cache_key_digest = hashlib.sha256()
        cache_key_digest.update(system_instruction.encode("utf-8"))
        cache_key_digest.update(prompt.encode("utf-8"))
        cache_key_digest.update(type(communicator).__qualname__.encode("utf-8"))
        cache_key_digest.update(
            str(getattr(communicator, "model_name", "")).encode("utf-8")
        )
        cache_key = cache_key_digest.hexdigest()
        chunk_cache_path = (
            prompt_snapshot_path.parent
            / "translation_chunks"
            / f"{Path(output_filename).name}.part_{chunk_index:03d}.json"
            if prompt_snapshot_path is not None
            else None
        )
        chunk_output = None
        cache_hit = False
        if chunk_cache_path is not None and chunk_cache_path.is_file() and not force:
            cache_payload = json.loads(chunk_cache_path.read_text(encoding="utf-8"))
            if cache_payload.get("cache_key") == cache_key:
                cached_output = cache_payload.get("translated_chunk")
                if not isinstance(cached_output, str) or not cached_output.strip():
                    raise ValueError(
                        f"Cached Moodle XML translation chunk is invalid: {chunk_cache_path}"
                    )
                chunk_output = cached_output
                cache_hit = True
                logger.info(
                    "Reusing validated Moodle XML translation chunk %s/%s.",
                    chunk_index,
                    chunk_count,
                )

        if chunk_output is None:
            if chunk_snapshot is not None:
                write_prompt_snapshot(chunk_snapshot, system_instruction, prompt)
            chunk_output = communicator.generate(
                system_instruction=system_instruction,
                contents=prompt,
                output_filename=(
                    f"{Path(output_filename).stem}_part_{chunk_index:03d}.xml"
                ),
                prompt_snapshot_path=chunk_snapshot,
                purpose=f"translate Moodle XML to {target_language}, chunk {chunk_index}/{chunk_count} ({output_filename})",
            )
        if not chunk_output or not chunk_output.strip():
            raise ValueError(
                f"Translator returned an empty Moodle XML chunk "
                f"({chunk_index}/{chunk_count})."
            )

        try:
            restored_chunk = _restore_embedded_payloads(
                source_chunk,
                chunk_output,
                binary_payloads,
                svg_payloads,
            )
            restored_root = ET.fromstring(restored_chunk)
        except (ET.ParseError, ValueError) as exc:
            raise ValueError(
                f"Moodle XML translation failed validation for chunk "
                f"{chunk_index}/{chunk_count}: {exc}"
            ) from exc

        for translated_child in restored_root:
            translated_root.append(translated_child)
        if chunk_cache_path is not None and not cache_hit:
            chunk_cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_cache_path = chunk_cache_path.with_suffix(".json.tmp")
            temporary_cache_path.write_text(
                json.dumps(
                    {"cache_key": cache_key, "translated_chunk": chunk_output},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            temporary_cache_path.replace(chunk_cache_path)
        logger.info(
            "Translated Moodle XML chunk %s/%s (%s questions).",
            chunk_index,
            chunk_count,
            len(chunk_children),
        )

    return _serialize_moodle_xml(translated_root)


def _append_moodle_language_tag(xml_content: str, target_language: str) -> str:
    language_code = LANG_ISO_MAP.get(
        target_language.strip().lower(),
        target_language.strip().lower()[:2],
    )
    language_code = re.sub(r"[^a-z0-9-]", "", language_code) or "xx"
    try:
        root = ET.fromstring(xml_content)
    except ET.ParseError as exc:
        raise ValueError("Translated artifact is malformed Moodle XML.") from exc

    for question in root.findall("question"):
        if question.get("type", "").casefold() == "category":
            continue
        tags = question.find("tags")
        if tags is not None and any(
            (tag.findtext("text") or "").strip().casefold()
            == f"lang:{language_code}".casefold()
            for tag in tags.findall("tag")
        ):
            continue
        if tags is None:
            tags = ET.SubElement(question, "tags")
        tag = ET.SubElement(tags, "tag")
        ET.SubElement(tag, "text").text = f"lang:{language_code}"

    return _serialize_moodle_xml(root)


def translate_assessment_artifact(
    communicator: BaseAICommunicator,
    source_artifact: str,
    target_language: str,
    output_format: str,
    pdf_engine: str = "html",
    *,
    output_filename: str,
    prompt_snapshot_path: Optional[Path] = None,
    force: bool = False,
    xml_chunk_size: int = XML_QUESTIONS_PER_TRANSLATION,
) -> str:
    """Translate a generated English artifact while retaining the English content."""
    normalized_format = output_format.lower()
    if normalized_format == "xml":
        prompt_name = "xml.md"
        artifact_format = "Moodle XML"
    elif normalized_format == "pdf" and pdf_engine.lower() in {"html", "tex"}:
        prompt_name = f"{pdf_engine.lower()}.md"
        artifact_format = "HTML5" if pdf_engine.lower() == "html" else "XeLaTeX"
    else:
        raise ValueError(
            f"Translation is unsupported for output format '{output_format}' "
            f"with PDF engine '{pdf_engine}'."
        )

    prompt_path = TRANSLATOR_PROMPT_DIR / prompt_name
    system_path = TRANSLATOR_PROMPT_DIR / "system.md"
    if not prompt_path.is_file() or not system_path.is_file():
        raise FileNotFoundError(f"Translation prompt files are missing for {artifact_format}.")

    system_instruction = system_path.read_text(encoding="utf-8").strip()
    language_instruction, _ = build_language_instructions(
        ["english", target_language],
        output_format=normalized_format,
        pdf_engine=pdf_engine,
    )
    if normalized_format == "xml":
        translated_artifact = _translate_moodle_xml_in_chunks(
            communicator,
            source_artifact,
            prompt_path,
            system_instruction,
            target_language,
            language_instruction,
            output_filename=output_filename,
            prompt_snapshot_path=prompt_snapshot_path,
            force=force,
            questions_per_chunk=xml_chunk_size,
        )
        translated_artifact = _append_moodle_language_tag(
            translated_artifact, target_language
        )
    else:
        prompt = load_prompt_template(
            prompt_path,
            target_language=target_language,
            language_instruction=language_instruction,
            source_artifact=source_artifact,
        )
        if prompt_snapshot_path is not None:
            write_prompt_snapshot(prompt_snapshot_path, system_instruction, prompt)

        translated_artifact = communicator.generate(
            system_instruction=system_instruction,
            contents=prompt,
            output_filename=output_filename,
            prompt_snapshot_path=prompt_snapshot_path,
            purpose=f"translate {artifact_format} artifact to {target_language} ({output_filename})",
        )
        if not translated_artifact or not translated_artifact.strip():
            raise ValueError(f"Translator returned an empty {artifact_format} artifact.")

    if normalized_format == "pdf" and pdf_engine.lower() == "html":
        if not re.search(r"<!doctype\s+html", translated_artifact, re.IGNORECASE):
            raise ValueError("Translator did not return a complete HTML5 document.")
        if not re.search(r"</html\s*>", translated_artifact, re.IGNORECASE):
            raise ValueError("Translator returned incomplete HTML5.")
    elif normalized_format == "pdf" and not re.search(
        r"\\end\s*\{document\}", translated_artifact
    ):
        raise ValueError("Translator did not return a complete XeLaTeX document.")

    logger.info("Translated English assessment to %s.", target_language)
    return translated_artifact
