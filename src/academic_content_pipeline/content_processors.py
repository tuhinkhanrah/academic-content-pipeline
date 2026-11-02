#!/usr/bin/env python3
"""
content_processors.py - Core Content Processing Classes.

Classes:
  1. QuestionPaperExtractor : Extracts questions & diagrams from exam paper PDFs.
  2. QuestionGenerator      : Synthesizes calibrated questions from source PDFs/MDs.
  3. PaperGenerator         : Synthesizes full mock exam papers from syllabi & specs.
"""

import hashlib
import json
import logging
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import yaml
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image

from .ai_communicators import (
    BaseAICommunicator,
    ImageAttachment,
    MultimodalBatch,
    RemoteSandboxBackend,
)
from .artifact_translator import translate_assessment_artifact
from .mistral_ocr import MistralOCREngine
from .output_renderers import OutputRenderer
from .resource_paths import resource_path
from .pipeline_utils import (
    assemble_prompt_files,
    build_language_instructions,
    extract_clean_question_nodes_with_status,
    load_prompt_template,
    load_file_content,
    fix_and_inject_moodle_xml,
    unique_image_items,
    write_prompt_snapshot,
)

logger = logging.getLogger("academic_content_pipeline")

PROMPTS_DIR = Path("prompts")


def load_yaml_or_json_file(path: Path) -> Any:
    """Load a YAML or legacy JSON data file into Python objects."""
    candidate = Path(path)
    raw = candidate.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    try:
        if candidate.suffix.lower() in {".yaml", ".yml"}:
            return yaml.safe_load(raw)
        return json.loads(raw)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Unable to parse config file: {candidate}") from exc


@dataclass(frozen=True)
class TutorialPromptSpec:
    prompt_path: Path
    intermediate_extension: str


class TutorialPromptResolver:
    """Resolve the tutorial prompt for a specific engine without any fallback path."""

    def __init__(self, prompt_root: Path):
        self.prompt_root = Path(prompt_root)

    def resolve(self, pdf_engine: str) -> TutorialPromptSpec:
        engine_name = str(pdf_engine).lower().strip()
        prompt_path = self.prompt_root / engine_name / "tutorial_request.md"
        if not prompt_path.exists():
            raise FileNotFoundError(f"Tutorial prompt not found for PDF engine '{engine_name}'.")
        return TutorialPromptSpec(
            prompt_path=prompt_path,
            intermediate_extension="tex" if engine_name == "tex" else "html",
        )


class TutorialSystemPromptResolver:
    """Resolve the tutorial system prompt for the selected engine without fallback behavior."""

    def __init__(self, prompt_root: Path):
        self.prompt_root = Path(prompt_root)

    def resolve(self, pdf_engine: str) -> Path:
        engine_name = str(pdf_engine).lower().strip()
        prompt_path = self.prompt_root / engine_name / "tutorial_system.md"
        if not prompt_path.exists():
            raise FileNotFoundError(f"Tutorial system prompt not found for PDF engine '{engine_name}'.")
        return prompt_path


def build_tutorial_system_instruction(pdf_engine: str = "html") -> str:
    """Load the engine-specific tutorial system instruction from the markdown prompt file."""
    resolver = TutorialSystemPromptResolver(PROMPTS_DIR / "generator")
    return resolver.resolve(pdf_engine).read_text(encoding="utf-8").strip()


def build_tutorial_language_instruction(languages: List[str]) -> str:
    """Build tutorial-specific language guidance from the markdown rules file."""
    clean_langs = [lang.strip().lower() for lang in languages if lang and lang.strip()]
    if not clean_langs:
        clean_langs = ["english"]

    if len(clean_langs) == 1 and clean_langs[0] != "english":
        target_lang = clean_langs[0].capitalize()
        return (
            f"Write the entire tutorial in {target_lang} only. Use clear academic {target_lang} terminology, preserve the chapter structure, "
            "and keep all mathematical equations, notation, variables, scientific names, and SVG labels unchanged. "
            "Include the requested exam-aligned practice questions, answers, and worked solutions in that language. "
            "Do not add English text or English section labels."
        )

    primary_lang = "english" if "english" in clean_langs else clean_langs[0]
    secondary_langs = [lang for lang in clean_langs if lang != primary_lang]

    if not secondary_langs:
        return (
            "Write the entire tutorial in English only. Keep the document in one language, maintain a clear academic tone, "
            "and include exam-aligned practice questions, answers, and worked solutions."
        )

    tutorial_rules = (PROMPTS_DIR / "generator" / "tutorial_language_rules.md").read_text(encoding="utf-8").strip()
    tutorial_rules = tutorial_rules.replace("{{languages}}", ", ".join(clean_langs))
    if len(clean_langs) > 1:
        target_lang = secondary_langs[0].capitalize()
        tutorial_rules = tutorial_rules.replace("target-language", target_lang)
        tutorial_rules = tutorial_rules.replace("requested language", f"{target_lang} language")
    elif clean_langs[0] != "english":
        target_lang = clean_langs[0].capitalize()
        tutorial_rules = tutorial_rules.replace("target-language", target_lang)
        tutorial_rules = tutorial_rules.replace("requested language", f"{target_lang} language")
    return tutorial_rules


class TutorialGenerator:
    """Generates exam-oriented chapter tutorials from PDFs or Markdown files."""

    def __init__(
        self,
        communicator: BaseAICommunicator,
        ocr_engine: Optional[MistralOCREngine] = None,
        rules_dict: Optional[Dict[str, Any]] = None,
        languages: Optional[List[str]] = None,
        standards: str = "General",
        tags: str = "",
        output_format: str = "pdf",
        pdf_engine: str = "html",
        page_range: Optional[List[int]] = None,
        staging_dir: Path = Path("output"),
        tutorial_title: Optional[str] = None,
        watermark_text: str = "",
        header_text: str = "",
        footer_text: str = "",
    ):
        self.communicator = communicator
        self.ocr_engine = ocr_engine
        self.rules_dict = rules_dict or {}
        self.languages = languages or ["english"]
        self.standards = standards
        self.tags = tags
        self.output_format = output_format.lower()
        if self.output_format not in {"pdf"}:
            raise ValueError("TutorialGenerator currently supports only PDF output mode.")
        self.pdf_engine = pdf_engine.lower()
        self.watermark_text = watermark_text or ""
        self.header_text = header_text or ""
        self.footer_text = footer_text or ""
        self.tutorial_prompt_resolver = TutorialPromptResolver(PROMPTS_DIR / "generator")
        self.tutorial_spec = self.tutorial_prompt_resolver.resolve(self.pdf_engine)
        self.renderer = OutputRenderer(
            self.output_format,
            self.pdf_engine,
            watermark_text=self.watermark_text,
            header_text=self.header_text,
            footer_text=self.footer_text,
        )
        self.page_range = page_range
        self.staging_dir = Path(staging_dir)
        self.tutorial_title = tutorial_title

    def _resolve_tutorial_prompt_path(self) -> Path:
        """Resolve the engine-specific tutorial prompt and fail fast if it is missing."""
        return self.tutorial_prompt_resolver.resolve(self.pdf_engine).prompt_path

    def process_file(self, input_file: Path, output_dir: Path, input_root: Optional[Path] = None) -> Path:
        """Generates a tutorial from a chapter file and writes a final PDF artifact."""
        input_file = Path(input_file)
        if not input_file.exists():
            raise FileNotFoundError(f"Tutorial input file does not exist: {input_file}")

        input_file = input_file.resolve()
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        item_start = time.perf_counter()

        paper_dir, markdown_dir, img_output_dir, temp_dir, prompt_dir = resolve_output_layout(
            input_root=input_root,
            file_path=input_file,
            output_dir=output_dir,
        )
        paper_dir.mkdir(parents=True, exist_ok=True)
        markdown_dir.mkdir(parents=True, exist_ok=True)
        prompt_dir.mkdir(parents=True, exist_ok=True)

        generation_stem = build_generated_output_stem(
            input_file.stem,
            self.languages,
            qualifier=self.standards,
        )
        logger.info(
            f"\n{'='*60}\n📘 [GENERATE-TUTORIAL] Processing: {input_file.name} ({self.output_format.upper()}) -> {generation_stem}\n{'='*60}"
        )

        image_map: Dict[str, str] = {}
        if input_file.suffix.lower() == ".pdf":
            if self.ocr_engine is None:
                self.ocr_engine = MistralOCREngine()
            ocr_result = self.ocr_engine.process_pdf(
                pdf_path=input_file,
                img_output_dir=img_output_dir,
                page_range=self.page_range,
                temp_dir=temp_dir,
            )
            markdown_text = ocr_result.full_markdown
            image_map = ocr_result.all_images
        else:
            markdown_text = load_file_content(input_file)

        system_instruction = build_tutorial_system_instruction(self.pdf_engine)
        lang_instruction, lang_tags = build_language_instructions(
            self.languages,
            output_format=self.output_format,
            pdf_engine=self.pdf_engine,
        )
        tutorial_lang_instruction = build_tutorial_language_instruction(self.languages)
        language_instruction = "\n\n".join(part for part in [lang_instruction, tutorial_lang_instruction] if part and part.strip())
        all_tags = [t.strip() for t in self.tags.split(",") if t.strip()] + lang_tags

        tutorial_title = self.tutorial_title or input_file.stem.replace("_", " ").replace("-", " ").title()
        prompt_path = self._resolve_tutorial_prompt_path()
        prompt_text = load_prompt_template(
            prompt_path,
            title=tutorial_title,
            chapter_content=markdown_text,
            languages=", ".join(self.languages),
            standards=self.standards,
            global_tags=", ".join(all_tags),
            output_format=self.output_format.upper(),
            pdf_engine=self.pdf_engine.upper(),
            language_instruction=language_instruction,
        )

        batch_images = [
            ImageAttachment(reference_id=img_name, source=filepath)
            for img_name, filepath in unique_image_items(image_map)
        ]
        multimodal_batch = MultimodalBatch(
            text=prompt_text,
            images=batch_images,
        )

        output_artifact_name = f"{generation_stem}.{self.tutorial_spec.intermediate_extension}"
        snapshot_path = prompt_dir / f"{generation_stem}_tutorial_prompt.md"
        write_prompt_snapshot(
            snapshot_path,
            system_instruction,
            multimodal_batch,
        )

        raw_output = self.communicator.generate(
            system_instruction=system_instruction,
            contents=multimodal_batch,
            output_filename=output_artifact_name,
            prompt_snapshot_path=snapshot_path,
        )

        final_pdf_path = self.renderer.render(
            raw_output,
            paper_dir,
            generation_stem,
            image_map,
        )
        logger.info(f"✨ Compiled tutorial PDF to: {final_pdf_path}")
        elapsed = time.perf_counter() - item_start
        logger.info("⏱️ [GENERATE-TUTORIAL] %s completed in %.2f seconds (%.2f minutes)", input_file.name, elapsed, elapsed / 60.0)
        return final_pdf_path

    def process_directory(self, input_dir: Path, output_dir: Path) -> List[Path]:
        """Processes all chapter PDFs and MDs in an input directory."""
        input_dir = Path(input_dir).resolve()
        source_files = [
            f for f in input_dir.rglob("*")
            if f.suffix.lower() in [".pdf", ".md"]
            and not f.name.startswith("sliced_")
            and not f.name.startswith("temp_")
        ]
        if not source_files:
            logger.warning(f"No valid chapter files (.pdf, .md) found in {input_dir}")
            return []

        directory_start = time.perf_counter()
        results = []
        for file_path in source_files:
            out_path = self.process_file(file_path, output_dir, input_root=input_dir)
            results.append(out_path)
        directory_elapsed = time.perf_counter() - directory_start
        logger.info("⏱️ [GENERATE-TUTORIAL DIRECTORY] Processed %d file(s) in %.2f seconds (%.2f minutes)", len(source_files), directory_elapsed, directory_elapsed / 60.0)
        self.ocr_engine.log_cache_summary()
        return results




def resolve_output_layout(
    input_root: Optional[Path],
    file_path: Path,
    output_dir: Path,
) -> tuple[Path, Path, Path, Path, Path]:
    """Resolve the mirrored final-output directory and the per-paper work folders.

    Final XML/PDF artifacts live beside the mirrored source path.
    Persistent OCR outputs live under the per-paper markdown/ and ocr/ folders.
    Temporary prompt snapshots live under a separate .tmp/prompts directory so they
    do not pollute the reusable artifact tree.
    """
    input_root = Path(input_root).resolve() if input_root is not None else None
    file_path = Path(file_path).resolve()
    output_root = Path(output_dir).resolve()

    if input_root is not None and file_path.is_relative_to(input_root):
        paper_dir = output_root / file_path.relative_to(input_root).parent
    else:
        paper_dir = output_root

    work_dir = paper_dir / file_path.stem
    markdown_dir = work_dir / "markdown"
    img_output_dir = work_dir / "ocr"
    temp_dir = img_output_dir / "temp_sliced"
    prompt_dir = work_dir / ".tmp" / "prompts"
    return paper_dir, markdown_dir, img_output_dir, temp_dir, prompt_dir


def find_existing_output(
    output_dir: Path,
    stem: str,
    output_format: str,
    *,
    generated_prefix: Optional[str] = None,
    include_legacy: bool = True,
) -> Optional[Path]:
    """Return an existing non-empty generated artifact for the same source file, if any."""
    if output_format == "xml":
        extension = "xml"
    else:
        extension = "pdf"

    candidates = []
    if include_legacy:
        candidates.append(output_dir / f"{stem}.{extension}")
        if extension == "pdf":
            candidates.append(output_dir / f"{stem}_extracted.pdf")
    if generated_prefix:
        candidates.append(output_dir / f"{generated_prefix}.{extension}")
    for candidate in candidates:
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


def build_generated_output_stem(
    stem: str,
    languages: Optional[List[str]] = None,
    *,
    qualifier: Optional[str] = None,
) -> str:
    """Create a stable output stem with an optional exam/standard before languages."""
    normalized = []
    for language in languages or ["english"]:
        token = str(language).strip().lower()
        token = re.sub(r"[^a-z0-9]+", "_", token).strip("_")
        if token:
            normalized.append(token)

    if not normalized:
        normalized = ["english"]

    safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(stem)).strip("._-")
    if not safe_stem:
        safe_stem = "output"

    qualifier_token = ""
    if qualifier:
        candidate = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(qualifier)).strip("._-").lower()
        if candidate in {"neet", "neet-ug", "neet_ug"}:
            candidate = "neet_ug"
        if candidate and candidate not in {"general", "mock_exam"}:
            qualifier_token = f"_{candidate}"

    language_prefix = "_".join(dict.fromkeys(normalized))
    return f"{safe_stem}{qualifier_token}_{language_prefix}"


def build_paper_metadata(
    spec_data: Dict[str, Any],
    subjects: List[Dict[str, Any]],
    duration_override: Optional[int],
) -> Dict[str, Any]:
    """Validate paper metadata and return the normalized prompt contract."""
    duration_minutes = duration_override
    if duration_minutes is None:
        duration_minutes = spec_data.get("duration_minutes")
    if duration_minutes is not None and duration_minutes <= 0:
        raise ValueError("duration_minutes must be greater than zero")

    if not subjects and spec_data.get("total_questions") is not None:
        subjects = [
            {
                "name": spec_data.get("paper_title", "Question Set"),
                "total_questions": spec_data["total_questions"],
                "marks_per_question": spec_data.get("marks_per_question", 1),
            }
        ]

    section_totals = []
    calculated_total_marks = 0.0
    sections = spec_data.get("sections", [])
    declared_total_marks = spec_data.get("total_marks")
    if sections:
        subject_count = len(subjects)
        for section in sections:
            scoring = section.get("scoring", {})
            question_count = int(section.get("question_count", section.get("total_questions", 0)))
            marks_per_question = float(scoring.get("correct", section.get("marks_per_question", 0)))
            multiplier = subject_count if section.get("subject", "all") == "all" else 1
            section_marks = question_count * marks_per_question * multiplier
            calculated_total_marks += section_marks
            section_totals.append(
                {
                    "name": section.get("name", section.get("id", "Section")),
                    "questions": question_count * multiplier,
                    "marks_per_question": marks_per_question,
                    "section_marks": section_marks,
                    "negative_marks": scoring.get("incorrect"),
                }
            )
    else:
        for subject in subjects:
            question_count = int(subject.get("total_questions", 0))
            marks_per_question = float(
                subject.get("marks_per_question", subject.get("default_grade", 0))
            )
            section_marks = question_count * marks_per_question
            calculated_total_marks += section_marks
            section_totals.append(
                {
                    "name": subject.get("name", "Subject"),
                    "questions": question_count,
                    "marks_per_question": marks_per_question,
                    "section_marks": section_marks,
                    "negative_marks": subject.get("negative_marks", subject.get("penalty")),
                }
            )
    if declared_total_marks is not None and abs(float(declared_total_marks) - calculated_total_marks) > 1e-9:
        raise ValueError(
            f"Spec total_marks ({declared_total_marks}) does not match calculated total "
            f"({calculated_total_marks:g})"
        )

    return {
        "duration_minutes": duration_minutes,
        "total_marks": declared_total_marks if declared_total_marks is not None else calculated_total_marks,
        "instructions": [
            str(item)
            for item in spec_data.get(
                "paper_instructions", spec_data.get("instructions", [])
            )
        ],
        "header_fields": spec_data.get(
            "header_fields",
            {"candidate_name": "", "roll_number": "", "date": ""},
        ),
        "section_totals": section_totals,
    }


def _normalize_subject_key(subject_name: str) -> str:
    """Normalize subject names for matching difficulty config keys."""
    return re.sub(r"[^a-z0-9]+", "_", str(subject_name).lower()).strip("_")


def _parse_difficulty_mix(mix_value: str) -> Dict[str, float]:
    """Parse a string like 'easy:0.2,medium:0.5,hard:0.3' into ratios."""
    parsed: Dict[str, float] = {}
    if not mix_value:
        return parsed
    for part in str(mix_value).split(","):
        if ":" not in part:
            continue
        key, value = part.split(":", 1)
        parsed[key.strip().lower()] = float(value.strip())
    return parsed


def _allocate_subject_difficulty_counts(total_questions: int, ratio: Dict[str, float]) -> Dict[str, int]:
    """Allocate a subject total into easy/medium/hard counts while preserving the total."""
    if total_questions <= 0:
        return {"easy": 0, "medium": 0, "hard": 0}

    labels = ["easy", "medium", "hard"]
    normalized = {label: float(ratio.get(label, 0.0)) for label in labels}
    if sum(normalized.values()) <= 0:
        normalized = {"easy": 0.2, "medium": 0.5, "hard": 0.3}

    raw = {label: total_questions * normalized[label] for label in labels}
    counts = {label: int(raw[label]) for label in labels}
    remainder = total_questions - sum(counts.values())
    if remainder != 0:
        order = sorted(labels, key=lambda label: raw[label] - counts[label], reverse=True)
        for label in order[: abs(remainder)]:
            counts[label] += 1 if remainder > 0 else -1

    return counts


def build_global_difficulty_hint(spec_data: Dict[str, Any]) -> str:
    """Return a non-conflicting difficulty summary with clear precedence rules."""
    fallback_mix = _parse_difficulty_mix(
        spec_data.get("difficulty_mix", "easy:0.2,medium:0.5,hard:0.3")
    )
    if spec_data.get("difficulty_by_subject"):
        return (
            "LEGACY GLOBAL MIX ONLY: ignored when subject-specific difficulty is present; "
            "subject-specific counts are authoritative. "
            f"Fallback values for single-subject papers only: easy:{fallback_mix.get('easy', 0.2)},"
            f"medium:{fallback_mix.get('medium', 0.5)},hard:{fallback_mix.get('hard', 0.3)}"
        )
    return (
        f"easy:{fallback_mix.get('easy', 0.2)},medium:{fallback_mix.get('medium', 0.5)},"
        f"hard:{fallback_mix.get('hard', 0.3)}"
    )


def build_subject_difficulty_contract(
    spec_data: Dict[str, Any],
    subjects: List[Dict[str, Any]],
) -> str:
    """Build a subject-aware difficulty contract for multi-subject exam generation."""
    difficulty_by_subject = spec_data.get("difficulty_by_subject") or {}
    fallback_mix = _parse_difficulty_mix(
        spec_data.get("difficulty_mix", "easy:0.2,medium:0.5,hard:0.3")
    )

    if not difficulty_by_subject:
        return (
            "Subject-specific difficulty allocation is mandatory for multi-subject papers. "
            "Do not rely on a single paper-wide mix when per-subject totals exist. "
            f"Fallback mix: easy:{fallback_mix.get('easy', 0.2)},medium:{fallback_mix.get('medium', 0.5)},hard:{fallback_mix.get('hard', 0.3)}"
        )

    lines = [
        "Subject-specific difficulty allocation is authoritative. "
        "Use the counts below for each subject; ignore the legacy paper-wide mix completely when this contract is present."
    ]
    for subject in subjects:
        subject_name = str(subject.get("name", "Subject"))
        total_questions = int(subject.get("total_questions", 0))
        subject_key = _normalize_subject_key(subject_name)
        config = (
            difficulty_by_subject.get(subject_key)
            or difficulty_by_subject.get(subject_name.lower())
            or difficulty_by_subject.get(subject_name)
            or fallback_mix
        )
        ratio = {
            "easy": float(config.get("easy", config.get("Easy", fallback_mix.get("easy", 0.2)))),
            "medium": float(config.get("medium", config.get("Medium", fallback_mix.get("medium", 0.5)))),
            "hard": float(config.get("hard", config.get("Hard", fallback_mix.get("hard", 0.3)))),
        }
        counts = _allocate_subject_difficulty_counts(total_questions, ratio)
        lines.append(
            f"- {subject_name}: easy: {counts['easy']}, medium: {counts['medium']}, hard: {counts['hard']} "
            f"(target ratios easy:{ratio['easy']},medium:{ratio['medium']},hard:{ratio['hard']})"
        )

    return "\n".join(lines)


def build_question_type_contract(
    spec_data: Dict[str, Any],
    registry_path: Path,
    max_questions: int,
) -> str:
    """Resolve question-type configuration into an explicit AI contract."""
    registry = load_yaml_or_json_file(Path(registry_path))
    entries = registry.get("mcq_types", [])
    by_key = {entry["id"]: entry for entry in entries}
    by_key.update({entry["name"]: entry for entry in entries})
    config = spec_data.get("question_types")
    if config is None:
        if spec_data.get("sections"):
            allocations: Dict[str, int] = {}
            for section in spec_data["sections"]:
                multiplier = max_questions // sum(
                    int(item.get("question_count", item.get("total_questions", 0)))
                    for item in spec_data["sections"]
                ) if section.get("subject", "all") == "all" else 1
                for item in section.get("question_types", []):
                    type_key = item.get("type") if isinstance(item, dict) else item
                    allocations[type_key] = allocations.get(type_key, 0) + int(item.get("count", 0)) * multiplier
            config = {
                "mode": "exact",
                "types": [{"type": key, "count": count} for key, count in allocations.items()],
            }
        else:
            config = {"mode": "automatic", "allowed": [entry["name"] for entry in entries]}
    elif isinstance(config, list):
        config = {"mode": "exact", "types": config}

    forbidden = set(config.get("forbidden", []))
    requested = config.get("types", config.get("allowed", []))
    if isinstance(requested, dict):
        requested = list(requested)
    if not requested:
        requested = [entry["name"] for entry in entries]

    resolved = []
    for item in requested:
        type_key = item.get("type") if isinstance(item, dict) else item
        if type_key in forbidden:
            continue
        if type_key not in by_key:
            raise ValueError(f"Unknown MCQ type: {type_key}")
        entry = by_key[type_key]
        if entry["name"] in forbidden or entry["id"] in forbidden:
            continue
        resolved.append((entry, item if isinstance(item, dict) else {}))

    mode = config.get("mode", "automatic").lower()
    if mode not in {"exact", "weighted", "adaptive", "automatic"}:
        raise ValueError(f"Unsupported question_types mode: {mode}")

    if mode == "exact":
        total = sum(int(item.get("count", 0)) for _, item in resolved)
        if total != max_questions:
            raise ValueError(
                f"Question type counts ({total}) must equal total_questions ({max_questions})"
            )
    if mode in {"weighted", "adaptive"}:
        weights = [float(item.get("weight", 0)) for _, item in resolved]
        if (
            not weights
            or any(not math.isfinite(weight) or weight <= 0 for weight in weights)
            or abs(sum(weights) - 1.0) > 1e-9
        ):
            raise ValueError("Question type weights must be positive and sum to 1.0.")

    allocation_lines = []
    for entry, allocation in resolved:
        line = f"- {entry.get('display_name', entry['name'])} ({entry['name']}): {entry['description']}"
        constraints = entry.get("constraints", {})
        if constraints:
            line += f" Constraints: {json.dumps(constraints, sort_keys=True)}."
        if mode == "exact":
            line += f" Requested count: {int(allocation.get('count', 0))}."
        elif mode in {"weighted", "adaptive"}:
            line += f" Weight: {float(allocation.get('weight', 0)):g}."
        allocation_lines.append(line)

    minimum = config.get("minimum", {})
    minimum_counts = (
        f"Minimum counts: {json.dumps(minimum, sort_keys=True)}."
        if minimum
        else ""
    )
    return load_prompt_template(
        PROMPTS_DIR / "generator" / "question_type_contract.md",
        max_questions=str(max_questions),
        allocation_mode=mode,
        allocations="\n".join(allocation_lines),
        minimum_counts=minimum_counts,
    )


def resolve_separate_question_type_allocations(
    spec_data: Dict[str, Any],
    registry_path: Path,
    total_questions: int,
) -> List[tuple[Dict[str, Any], int]]:
    """Resolve exact per-type counts for separate Moodle XML generation."""
    config = spec_data.get("question_types")
    if isinstance(config, list):
        config = {"mode": "exact", "types": config}
    mode = config.get("mode", "automatic") if isinstance(config, dict) else None
    if not isinstance(mode, str) or mode.casefold() != "exact":
        raise ValueError(
            "Separate question-type output requires --spec with "
            "question_types.mode: exact and explicit per-type counts."
        )

    requested = config.get("types")
    if not isinstance(requested, list) or not requested:
        raise ValueError(
            "Separate question-type output requires question_types.types "
            "to be a non-empty list of {type, count} entries."
        )

    registry = load_yaml_or_json_file(Path(registry_path))
    entries = registry.get("mcq_types", [])
    by_key = {entry["id"]: entry for entry in entries}
    by_key.update({entry["name"]: entry for entry in entries})

    allocations: List[tuple[Dict[str, Any], int]] = []
    seen_types: set[str] = set()
    for allocation in requested:
        if not isinstance(allocation, dict) or "type" not in allocation:
            raise ValueError(
                "Each question_types.types entry must include both 'type' and 'count'."
            )
        type_key = allocation["type"]
        if not isinstance(type_key, str):
            raise ValueError("Question type names and IDs must be strings.")
        if type_key not in by_key:
            raise ValueError(f"Unknown MCQ type: {type_key}")
        entry = by_key[type_key]
        type_name = entry["name"]
        if type_name in seen_types:
            raise ValueError(f"Question type is allocated more than once: {type_name}")
        seen_types.add(type_name)
        count_value = allocation.get("count")
        if isinstance(count_value, bool) or not isinstance(count_value, int):
            raise ValueError(
                f"Question type '{type_name}' must have an integer count."
            )
        count = count_value
        if count < 0:
            raise ValueError(f"Question type '{type_name}' cannot have a negative count.")
        if count:
            allocations.append((entry, count))

    allocated_total = sum(count for _, count in allocations)
    if allocated_total != total_questions:
        raise ValueError(
            f"Separate question-type counts ({allocated_total}) must equal "
            f"total_questions ({total_questions})."
        )
    return allocations


def resolve_adaptive_question_type_profile(
    spec_data: Dict[str, Any],
    registry_path: Path,
) -> List[tuple[Dict[str, Any], float]]:
    """Resolve weighted type preferences used to plan a chapter-specific mix."""
    config = spec_data.get("question_types")
    if not isinstance(config, dict) or config.get("mode") != "adaptive":
        raise ValueError(
            "Adaptive question-type output requires question_types.mode: adaptive."
        )
    requested = config.get("types")
    if not isinstance(requested, list) or not requested:
        raise ValueError(
            "Adaptive question_types.types must be a non-empty list of "
            "{type, weight} entries."
        )

    registry = load_yaml_or_json_file(Path(registry_path))
    entries = registry.get("mcq_types", [])
    by_key = {entry["id"]: entry for entry in entries}
    by_key.update({entry["name"]: entry for entry in entries})

    profile: List[tuple[Dict[str, Any], float]] = []
    seen_types: set[str] = set()
    for allocation in requested:
        if not isinstance(allocation, dict) or not isinstance(
            allocation.get("type"), str
        ):
            raise ValueError("Each adaptive question type must include a string 'type'.")
        type_key = allocation["type"]
        if type_key not in by_key:
            raise ValueError(f"Unknown MCQ type: {type_key}")
        entry = by_key[type_key]
        type_name = entry["name"]
        if type_name in seen_types:
            raise ValueError(f"Question type is listed more than once: {type_name}")
        seen_types.add(type_name)
        weight_value = allocation.get("weight")
        if isinstance(weight_value, bool) or not isinstance(
            weight_value, (int, float)
        ):
            raise ValueError(
                f"Adaptive question type '{type_name}' must have a numeric weight."
            )
        weight = float(weight_value)
        if not math.isfinite(weight) or weight <= 0:
            raise ValueError(
                f"Adaptive question type '{type_name}' weight must be positive."
            )
        profile.append((entry, weight))

    if abs(sum(weight for _, weight in profile) - 1.0) > 1e-9:
        raise ValueError("Adaptive question-type weights must sum to 1.0.")
    return profile


def parse_adaptive_question_type_plan(
    raw_plan: str,
    profile: List[tuple[Dict[str, Any], float]],
    target_questions: int,
) -> List[tuple[Dict[str, Any], int]]:
    """Validate a model-produced per-type plan against the configured profile."""
    try:
        plan = json.loads(BaseAICommunicator.strip_code_fences(raw_plan))
    except json.JSONDecodeError as exc:
        raise ValueError("AI returned an invalid JSON question-type allocation plan.") from exc
    if not isinstance(plan, dict) or not isinstance(plan.get("allocations"), list):
        raise ValueError("Question-type allocation plan must contain an 'allocations' list.")

    profile_by_name = {entry["name"]: entry for entry, _ in profile}
    profile_by_id = {entry["id"]: entry for entry, _ in profile}
    allocations: Dict[str, int] = {}
    for item in plan["allocations"]:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise ValueError("Each planned allocation must contain a string 'type'.")
        type_key = item["type"]
        entry = profile_by_name.get(type_key) or profile_by_id.get(type_key)
        if entry is None:
            raise ValueError(
                f"AI allocated a type not enabled by the profile: {type_key}"
            )
        count = item.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(
                f"AI allocation for '{entry['name']}' must be a non-negative integer."
            )
        if entry["name"] in allocations:
            raise ValueError(
                f"AI allocated question type more than once: {entry['name']}"
            )
        allocations[entry["name"]] = count

    resolved = [
        (entry, allocations.get(entry["name"], 0))
        for entry, _ in profile
        if allocations.get(entry["name"], 0) > 0
    ]
    allocated_total = sum(count for _, count in resolved)
    if allocated_total < 1:
        raise ValueError("AI found no suitable questions to allocate for this chapter.")
    if allocated_total > target_questions:
        raise ValueError(
            f"AI allocated {allocated_total} questions, exceeding the target "
            f"of {target_questions}."
        )
    return resolved


def write_json_atomically(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON through a temporary sibling so readers never see partial data."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def is_valid_moodle_xml_question_file(
    path: Path,
    expected_question_count: int,
) -> bool:
    """Return whether an existing file is Moodle XML with the expected count."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return False
    actual_question_count = sum(
        node.tag == "question" and node.get("type") != "category"
        for node in root
    )
    return root.tag == "quiz" and actual_question_count == expected_question_count


# =======================================================================
# 1. Question Paper Extractor Class
# =======================================================================

class QuestionPaperExtractor:
    """Extracts questions from PDF exam papers using Mistral OCR and AI Communicator."""

    def __init__(
        self,
        communicator: BaseAICommunicator,
        ocr_engine: Optional[MistralOCREngine] = None,
        rules_dict: Optional[Dict[str, Any]] = None,
        languages: Optional[List[str]] = None,
        standards: str = "General",
        tags: str = "",
        page_range: Optional[List[int]] = None,
        no_instruction_page: bool = False,
        instruction_page: int = 1,
        verify_online: bool = False,
        rate_limit_delay: float = 4.0,
        output_format: str = "xml",
        pdf_engine: str = "html",
        staging_dir: Path = Path("output"),
        batch_size: int = 0,
        force_overwrite: bool = False,
        page_check_communicator: Optional[BaseAICommunicator] = None,
        header_text: str = "",
        footer_text: str = "",
    ):
        self.communicator = communicator
        self.page_check_communicator = page_check_communicator or communicator
        self.ocr_engine = ocr_engine or MistralOCREngine()
        self.rules_dict = rules_dict or {}
        self.languages = languages or ["english"]
        self.standards = standards
        self.tags = tags
        self.page_range = page_range
        self.no_instruction_page = no_instruction_page
        self.instruction_page = instruction_page
        self.verify_online = verify_online
        self.rate_limit_delay = rate_limit_delay
        self.output_format = output_format.lower()
        self.pdf_engine = pdf_engine.lower()
        self.staging_dir = Path(staging_dir)
        self.batch_size = int(batch_size)
        self.force_overwrite = bool(force_overwrite)
        self.header_text = header_text or ""
        self.footer_text = footer_text or ""

    @staticmethod
    def parse_instruction_page_summary(response: Optional[str]) -> Optional[str]:
        """Parse the instruction-page JSON response into a summary string."""
        if response is None:
            return None

        cleaned = BaseAICommunicator.strip_code_fences(response).strip()
        if not cleaned:
            return None

        try:
            payload = json.loads(cleaned)
            if isinstance(payload, dict):
                is_instruction = bool(payload.get("is_instruction_page"))
                if not is_instruction:
                    return None
                summary = payload.get("summary")
                summary_text = str(summary or "").strip()
                if summary_text:
                    return summary_text
                return ""
        except json.JSONDecodeError:
            pass

        normalized = cleaned.replace("**", "").replace("`", "").strip()
        if re.search(r"(?is)^\s*(?:NOT_INSTRUCTION_PAGE|NO_INSTRUCTION_PAGE)\b", normalized):
            return None

        if re.search(r"(?is)^\s*(?:INSTRUCTION_PAGE|INSTRUCTION\s+PAGE)\b", normalized):
            return normalized

        return normalized

    def _detect_instruction_page_summary(
        self,
        page_data: Any,
        markdown_dir: Path,
        pdf_path: Path,
    ) -> Optional[str]:
        if page_data is None:
            return None

        prompt_text = (
            "Determine whether this page is an exam instruction page.\n"
            "Return valid JSON only with exactly these fields:\n"
            "{\"is_instruction_page\": true|false, \"summary\": \"<markdown summary only if it is an instruction page, otherwise empty string>\"}\n"
            "If the page is not an instruction page, set is_instruction_page to false and summary to an empty string.\n"
            "If it is an instruction page, provide a concise Markdown summary of the header and key instructions relevant to answering the paper.\n\n"
            f"{page_data.markdown}"
        )

        batch_images = [
            ImageAttachment(reference_id=img_name, source=filepath)
            for img_name, filepath in unique_image_items(page_data.images)
        ]
        multimodal_batch = MultimodalBatch(
            text=prompt_text,
            images=batch_images,
            page_range=(page_data.page_num, page_data.page_num),
        )

        snapshot_path = markdown_dir / f"{pdf_path.stem}_instruction_page_check_prompt.md"
        raw_response = self.page_check_communicator.generate(
            system_instruction=(
                "You are a strict exam-paper classifier. Determine whether the supplied page is an instruction page. "
                "Return valid JSON only with the fields is_instruction_page and summary. "
                "When the page is not an instruction page, set is_instruction_page to false and summary to an empty string."
            ),
            contents=multimodal_batch,
            output_filename=f"{pdf_path.stem}_instruction_page_check.json",
            prompt_snapshot_path=snapshot_path,
        )
        parsed_summary = self.parse_instruction_page_summary(raw_response)
        try:
            payload = json.loads(BaseAICommunicator.strip_code_fences(raw_response or "")) if raw_response else None
            is_instruction_page = bool(payload.get("is_instruction_page")) if isinstance(payload, dict) else False
        except Exception:
            is_instruction_page = parsed_summary is not None

        if is_instruction_page:
            logger.info(
                "📄 AI classified page %s as an instruction page. Summary: %s",
                page_data.page_num,
                (parsed_summary or "").strip()[:300],
            )
        else:
            logger.info("📄 AI classified page %s as NOT an instruction page.", page_data.page_num)

        return parsed_summary

    @staticmethod
    def _page_batches(pages: List[Any], batch_size: int) -> List[List[Any]]:
        page_batch_size = len(pages) if batch_size <= 0 else max(1, int(batch_size))
        return [pages[i : i + page_batch_size] for i in range(0, len(pages), page_batch_size)]

    def process_file(self, pdf_path: Path, output_dir: Path, input_root: Optional[Path] = None) -> Path:
        """Extracts questions from a single PDF and writes Moodle XML."""
        pdf_path = Path(pdf_path).resolve()
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        item_start = time.perf_counter()

        paper_dir, markdown_dir, img_output_dir, temp_dir, prompt_dir = resolve_output_layout(
            input_root=input_root,
            file_path=pdf_path,
            output_dir=output_dir,
        )
        paper_dir.mkdir(parents=True, exist_ok=True)
        markdown_dir.mkdir(parents=True, exist_ok=True)
        prompt_dir.mkdir(parents=True, exist_ok=True)

        output_stem = build_generated_output_stem(
            pdf_path.stem if self.output_format == "xml" else f"{pdf_path.stem}_extracted",
            self.languages,
            qualifier=self.standards,
        )
        output_prefix = output_stem
        is_default_naming = (
            self.standards.strip().lower() == "general"
            and [language.strip().lower() for language in self.languages] == ["english"]
        )
        existing_output = find_existing_output(
            paper_dir,
            pdf_path.stem,
            self.output_format,
            generated_prefix=output_prefix,
            include_legacy=is_default_naming,
        )
        if existing_output is not None and not self.force_overwrite:
            logger.info(f"⏭️ Skipping {pdf_path.name}: output already exists at {existing_output}")
            return existing_output
        if existing_output is not None and self.force_overwrite:
            logger.info(f"🔄 Reprocessing {pdf_path.name}: force_overwrite enabled; the stable output will be replaced (existing output: {existing_output}).")

        logger.info(f"\n{'='*60}\n📄 [EXTRACT] Processing: {pdf_path.name}\n{'='*60}")

        # 1. Convert PDF to Markdown & extract isolated diagrams via Mistral OCR
        ocr_result = self.ocr_engine.process_pdf(
            pdf_path=pdf_path,
            img_output_dir=img_output_dir,
            page_range=self.page_range,
            temp_dir=temp_dir,
        )

        instruction_page_summary = None
        if getattr(ocr_result, "pages", None):
            target_page_num = self.instruction_page
            instruction_page = next(
                (page for page in ocr_result.pages if getattr(page, "page_num", None) == target_page_num),
                ocr_result.pages[0],
            )
            instruction_page_summary = self._detect_instruction_page_summary(instruction_page, markdown_dir, pdf_path)

        # 2. Assemble system prompt rules
        system_instruction = assemble_prompt_files(
            self.rules_dict,
            mode="extract",
            output_format="xml" if self.output_format == "xml" else "pdf",
            pdf_engine=self.pdf_engine,
            verify_online=self.verify_online,
            instruction_page_summary=instruction_page_summary,
        )

        lang_instruction, lang_tags = build_language_instructions(
            self.languages,
            output_format="xml" if self.output_format == "xml" else "pdf",
            pdf_engine=self.pdf_engine,
        )
        all_tags = [t.strip() for t in self.tags.split(",") if t.strip()] + lang_tags
        if self.output_format == "xml":
            system_instruction += "\n\n" + load_prompt_template(
                PROMPTS_DIR / "core" / "extraction_contract.md",
                language_instruction=lang_instruction,
                standards=self.standards,
                global_tags="\n".join(f"  <tag><text>{tag}</text></tag>" for tag in all_tags),
            )

        all_questions_xml: List[str] = []
        pages_to_process = ocr_result.pages if hasattr(ocr_result, "pages") and ocr_result.pages else []

        if self.output_format == "xml" and pages_to_process:
            if self.batch_size <= 0:
                logger.info(
                    "Processing all %s page(s) in a single batch for %s mode.",
                    len(pages_to_process),
                    type(self.communicator).__name__,
                )
            else:
                logger.info(
                    "Processing %s page(s) in batches of %s for %s mode.",
                    len(pages_to_process),
                    self.batch_size,
                    type(self.communicator).__name__,
                )
            for batch_index, batch_pages in enumerate(self._page_batches(pages_to_process, self.batch_size), start=1):
                filtered_batch = []
                for page_data in batch_pages:
                    p_num = page_data.page_num
                    if self.no_instruction_page and p_num == self.instruction_page:
                        logger.info(f"⏩ Skipping instruction page {p_num}...")
                        continue
                    filtered_batch.append(page_data)

                if not filtered_batch:
                    continue

                batch_start = filtered_batch[0].page_num
                batch_end = filtered_batch[-1].page_num
                logger.info(f"\n--- 📄 Processing Pages {batch_start}-{batch_end} (batch {batch_index}) ---")

                batch_prompt_parts = []
                batch_images: List[ImageAttachment] = []

                for page_data in filtered_batch:
                    p_num = page_data.page_num
                    page_prompt = load_prompt_template(
                        PROMPTS_DIR / "core" / "extraction_page_turn.md",
                        page_number=str(p_num),
                        image_ids=str(list(page_data.images.keys())),
                        ocr_markdown=page_data.markdown,
                    )
                    batch_prompt_parts.append(page_prompt)

                    for img_name, filepath in unique_image_items(page_data.images):
                        batch_images.append(ImageAttachment(reference_id=img_name, source=filepath))

                combined_prompt = "\n\n".join(batch_prompt_parts)
                multimodal_batch = MultimodalBatch(
                    text=combined_prompt,
                    images=batch_images,
                    page_range=(batch_start, batch_end),
                )

                snapshot_name = f"{pdf_path.stem}_pages_{batch_start}_{batch_end}_prompt.md"
                write_prompt_snapshot(prompt_dir / snapshot_name, system_instruction, multimodal_batch)
                raw_batch_output = self.communicator.generate(
                    system_instruction=system_instruction,
                    contents=multimodal_batch,
                    output_filename=f"pages_{batch_start}_{batch_end}.xml",
                    prompt_snapshot_path=prompt_dir / snapshot_name,
                )

                valid_nodes, _ = extract_clean_question_nodes_with_status(raw_batch_output)
                if valid_nodes:
                    logger.info(f"  ✓ Pages {batch_start}-{batch_end}: Extracted {len(valid_nodes)} question(s).")
                    all_questions_xml.extend(valid_nodes)
                else:
                    logger.info(f"  ℹ Pages {batch_start}-{batch_end}: No complete questions concluded in this batch.")

                if self.rate_limit_delay > 0:
                    time.sleep(self.rate_limit_delay)
        else:
            if self.output_format == "pdf":
                prompt_text = load_prompt_template(
                    PROMPTS_DIR / "extractor" / "extraction_request.md",
                    ocr_content=ocr_result.full_markdown,
                    languages=", ".join(self.languages),
                    standards=self.standards,
                    global_tags=", ".join(all_tags),
                    image_ids=str(list(ocr_result.all_images.keys())),
                    language_instruction=lang_instruction,
                )
                batch_images = [
                    ImageAttachment(reference_id=img_name, source=filepath)
                    for img_name, filepath in unique_image_items(ocr_result.all_images)
                ]
                multimodal_batch = MultimodalBatch(
                    text=prompt_text,
                    images=batch_images,
                )

                output_extension = "tex" if self.pdf_engine == "tex" else "html"
                snapshot_path = prompt_dir / f"{output_stem}_extraction_prompt.md"
                write_prompt_snapshot(snapshot_path, system_instruction, multimodal_batch)
                raw_output = self.communicator.generate(
                    system_instruction=system_instruction,
                    contents=multimodal_batch,
                    output_filename=f"{output_stem}.{output_extension}",
                    prompt_snapshot_path=snapshot_path,
                )
                return OutputRenderer(
                    "pdf",
                    self.pdf_engine,
                    header_text=self.header_text,
                    footer_text=self.footer_text,
                ).render(
                    raw_output,
                    paper_dir,
                    output_stem,
                    ocr_result.all_images,
                )

            if self.batch_size <= 0:
                logger.info("Sending full document OCR Markdown in a single batch for XML extraction...")
            else:
                logger.info("Sending full document OCR Markdown in batches of %s for XML extraction...", self.batch_size)
            for batch_index, batch_pages in enumerate(self._page_batches(pages_to_process, self.batch_size), start=1):
                if not batch_pages:
                    continue
                batch_start = batch_pages[0].page_num
                batch_end = batch_pages[-1].page_num
                batch_markdown = "\n\n".join(page_data.markdown for page_data in batch_pages)
                prompt_text = (
                    f"### Exam Paper OCR Markdown (Pages {batch_start}-{batch_end}):\n\n{batch_markdown}\n\n"
                    f"### Extraction Parameters:\n"
                    f"- Target Languages: {', '.join(self.languages)}\n"
                    f"- Target Standards: {self.standards}\n"
                    f"- Global Tags: {', '.join(all_tags)}\n"
                    f"- Attached Diagram Reference IDs: {list(ocr_result.all_images.keys())}\n\n"
                    f"{lang_instruction}\n"
                    f"Extract all complete questions from these pages into valid Moodle XML format."
                )
                batch_images = []
                for page_data in batch_pages:
                    for img_name, filepath in unique_image_items(page_data.images):
                        batch_images.append(ImageAttachment(reference_id=img_name, source=filepath))

                multimodal_batch = MultimodalBatch(
                    text=prompt_text,
                    images=batch_images,
                    page_range=(batch_start, batch_end),
                )

                snapshot_name = f"{pdf_path.stem}_batch_{batch_start}_{batch_end}_prompt.md"
                write_prompt_snapshot(prompt_dir / snapshot_name, system_instruction, multimodal_batch)
                raw_output = self.communicator.generate(
                    system_instruction=system_instruction,
                    contents=multimodal_batch,
                    output_filename=f"{pdf_path.stem}_pages_{batch_start}_{batch_end}.xml",
                    prompt_snapshot_path=prompt_dir / snapshot_name,
                )
                valid_nodes, _ = extract_clean_question_nodes_with_status(raw_output)
                logger.info(f"  ✓ Batch {batch_start}-{batch_end}: Extracted {len(valid_nodes)} question(s).")
                all_questions_xml.extend(valid_nodes)

        combined_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<quiz>\n'
            + '\n'.join(all_questions_xml) +
            '\n</quiz>'
        )

        final_xml = fix_and_inject_moodle_xml(combined_xml, ocr_result.all_images)
        output_filepath = paper_dir / f"{output_stem}.xml"
        output_filepath.write_text(final_xml, encoding="utf-8")

        logger.info(f"✅ Extracted {len(all_questions_xml)} total question(s) -> {output_filepath}")
        elapsed = time.perf_counter() - item_start
        logger.info("⏱️ [EXTRACT] %s completed in %.2f seconds (%.2f minutes)", pdf_path.name, elapsed, elapsed / 60.0)
        return output_filepath

    def process_directory(self, input_dir: Path, output_dir: Path) -> List[Path]:
        """Processes all PDFs in an input directory."""
        input_dir = Path(input_dir).resolve()
        pdf_files = [
            f for f in input_dir.rglob("*.pdf")
            if not f.name.startswith("sliced_") and not f.name.startswith("temp_")
        ]
        if not pdf_files:
            logger.warning(f"No PDF files found in {input_dir}")
            return []

        directory_start = time.perf_counter()
        results = []
        for pdf_path in pdf_files:
            out_file = self.process_file(pdf_path, output_dir, input_root=input_dir)
            results.append(out_file)
        directory_elapsed = time.perf_counter() - directory_start
        logger.info("⏱️ [EXTRACT DIRECTORY] Processed %d PDF(s) in %.2f seconds (%.2f minutes)", len(pdf_files), directory_elapsed, directory_elapsed / 60.0)
        self.ocr_engine.log_cache_summary()
        return results


# =======================================================================
# 2. Chapter Question Generator Class
# =======================================================================

class QuestionGenerator:
    """Synthesizes questions from source PDFs or Markdown files."""

    @staticmethod
    def split_xml_papers(raw_output: str, question_count: int, num_papers: int) -> List[str]:
        if num_papers < 1 or question_count < num_papers:
            raise ValueError("Number of papers must be positive and cannot exceed the requested question count")
        quiz = ET.fromstring(fix_and_inject_moodle_xml(raw_output, {}))
        questions = [node for node in quiz if node.tag == "question" and node.get("type") != "category"]
        if len(questions) < question_count:
            raise ValueError(f"Requested {question_count} questions, but AI returned only {len(questions)} parseable questions")
        papers = []
        offset = 0
        base_size, extra = divmod(question_count, num_papers)
        for index in range(num_papers):
            size = base_size + (index < extra)
            paper = ET.Element("quiz")
            paper.extend(questions[offset:offset + size])
            papers.append(ET.tostring(paper, encoding="unicode"))
            offset += size
        return papers

    def __init__(
        self,
        communicator: BaseAICommunicator,
        ocr_engine: Optional[MistralOCREngine] = None,
        rules_dict: Optional[Dict[str, Any]] = None,
        languages: Optional[List[str]] = None,
        standards: str = "General",
        tags: str = "",
        difficulty_mix: str = "easy:0.2,medium:0.5,hard:0.3",
        num_questions: int = 5,
        output_format: str = "xml",
        pdf_engine: str = "html",
        page_range: Optional[List[int]] = None,
        staging_dir: Path = Path("output"),
        exam_duration_minutes: Optional[int] = None,
        spec_path: Optional[Path] = None,
        mcq_types_path: Path = resource_path("catalogs/mcq_types.yaml"),
        watermark_text: str = "",
        num_papers: int = 1,
        header_text: str = "",
        footer_text: str = "",
        translate_to: Optional[List[str]] = None,
        force_overwrite: bool = False,
        separate_by_question_type: bool = False,
    ):
        self.communicator = communicator
        self.ocr_engine = ocr_engine or MistralOCREngine()
        self.rules_dict = rules_dict or {}
        self.languages = languages or ["english"]
        unique_targets = {}
        for language in translate_to or []:
            target = language.strip()
            if target:
                unique_targets.setdefault(target.casefold(), target)
        self.translate_to = list(unique_targets.values())
        self.force_overwrite = bool(force_overwrite)
        if any(language.lower() == "english" for language in self.translate_to):
            raise ValueError("--translate-to must contain non-English languages only.")
        requested_languages = {language.lower() for language in self.languages}
        duplicate_targets = [
            language
            for language in self.translate_to
            if language.lower() in requested_languages and language.lower() != "english"
        ]
        if duplicate_targets:
            raise ValueError(
                "Do not request the same non-English language in both --languages "
                f"and --translate-to: {', '.join(duplicate_targets)}"
            )
        self.standards = standards
        self.tags = tags
        self.difficulty_mix = difficulty_mix
        self.num_questions = num_questions
        self.num_papers = num_papers
        self.separate_by_question_type = bool(separate_by_question_type)
        self.output_format = output_format.lower()
        if self.num_papers < 1:
            raise ValueError("--num-papers must be at least 1")
        if self.num_papers > 1 and self.output_format != "xml":
            raise ValueError("--num-papers greater than 1 currently requires --output-format xml")
        if self.separate_by_question_type and self.output_format != "xml":
            raise ValueError("--separate-by-question-type requires --output-format xml.")
        if self.separate_by_question_type and self.num_papers != 1:
            raise ValueError(
                "--separate-by-question-type cannot be combined with --num-papers."
            )
        self.pdf_engine = pdf_engine.lower()
        self.watermark_text = watermark_text or ""
        self.header_text = header_text or ""
        self.footer_text = footer_text or ""
        self.renderer = OutputRenderer(
            self.output_format,
            self.pdf_engine,
            watermark_text=self.watermark_text,
            header_text=self.header_text,
            footer_text=self.footer_text,
        )
        self.page_range = page_range
        self.staging_dir = Path(staging_dir)
        self.exam_duration_minutes = exam_duration_minutes
        self.spec_path = Path(spec_path).resolve() if spec_path else None
        self.mcq_types_path = Path(mcq_types_path)

    def process_file(self, input_file: Path, output_dir: Path, input_root: Optional[Path] = None) -> Path | List[Path]:
        """Synthesizes questions from a chapter file (.pdf or .md)."""
        input_file = Path(input_file).resolve()
        if not input_file.is_file():
            raise FileNotFoundError(f"Chapter input file not found: {input_file}")
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        item_start = time.perf_counter()

        paper_dir, markdown_dir, img_output_dir, temp_dir, prompt_dir = resolve_output_layout(
            input_root=input_root,
            file_path=input_file,
            output_dir=output_dir,
        )
        paper_dir.mkdir(parents=True, exist_ok=True)
        markdown_dir.mkdir(parents=True, exist_ok=True)
        prompt_dir.mkdir(parents=True, exist_ok=True)

        paper_spec: Dict[str, Any] = {}
        if self.spec_path is not None:
            if not self.spec_path.exists():
                raise FileNotFoundError(f"Paper spec file not found: {self.spec_path}")
            if self.spec_path.suffix.lower() not in {".yaml", ".yml", ".json"}:
                raise ValueError("generate-questions --spec must point to a YAML paper spec")
            paper_spec = load_yaml_or_json_file(self.spec_path)
            spec_subjects = paper_spec.get("subjects") or [
                {
                    "name": paper_spec.get("paper_title", input_file.stem),
                    "total_questions": paper_spec.get("total_questions", self.num_questions),
                    "marks_per_question": paper_spec.get("marks_per_question", 1),
                }
            ]
            paper_metadata = build_paper_metadata(
                paper_spec,
                spec_subjects,
                self.exam_duration_minutes,
            )
            if paper_spec.get("instruction_profile"):
                self.rules_dict["instruction_profile"] = paper_spec["instruction_profile"]
                self.rules_dict["instruction_file"] = None
        else:
            paper_metadata = None
        generation_stem = build_generated_output_stem(
            input_file.stem,
            self.languages,
            qualifier=(
                paper_spec.get("exam_name")
                if str(paper_spec.get("exam_name", "")).strip().lower() not in {"", "mock_exam"}
                else self.standards
            ),
        )
        logger.info(f"\n{'='*60}\n📚 [GENERATE-QUESTIONS] Processing: {input_file.name} ({self.output_format.upper()}) -> {generation_stem}\n{'='*60}")
        generation_question_count = (
            int(paper_spec.get("total_questions", self.num_questions))
            if paper_spec
            else self.num_questions
        )
        if self.num_papers > 1 and generation_question_count < self.num_papers:
            raise ValueError("Number of papers cannot exceed the requested question count")
        question_count_contract = ""
        difficulty_count_contract = ""
        if self.num_papers > 1:
            question_count_contract = (
                f"- Exact total question count (required): {generation_question_count}\n"
            )
            difficulty_counts = _allocate_subject_difficulty_counts(
                generation_question_count,
                _parse_difficulty_mix(self.difficulty_mix),
            )
            difficulty_count_contract = (
                f"- Required difficulty counts (out of {generation_question_count}): "
                f"easy: {difficulty_counts['easy']}, medium: {difficulty_counts['medium']}, "
                f"hard: {difficulty_counts['hard']}\n"
                "- Match these difficulty counts exactly.\n"
            )
        question_type_contract = build_question_type_contract(
            paper_spec,
            self.mcq_types_path,
            generation_question_count,
        )
        question_type_config = paper_spec.get("question_types")
        question_type_mode = (
            question_type_config.get("mode", "exact")
            if isinstance(question_type_config, dict)
            else "exact" if isinstance(question_type_config, list) else None
        )
        adaptive_type_profile = (
            resolve_adaptive_question_type_profile(
                paper_spec,
                self.mcq_types_path,
            )
            if self.separate_by_question_type
            and question_type_mode == "adaptive"
            else []
        )
        separate_type_allocations = (
            resolve_separate_question_type_allocations(
                paper_spec,
                self.mcq_types_path,
                generation_question_count,
            )
            if self.separate_by_question_type
            and question_type_mode != "adaptive"
            else []
        )

        # 1. Read or OCR source content
        image_map: Dict[str, str] = {}
        if input_file.suffix.lower() == ".pdf":
            ocr_result = self.ocr_engine.process_pdf(
                pdf_path=input_file,
                img_output_dir=img_output_dir,
                page_range=self.page_range,
                temp_dir=temp_dir,
            )
            markdown_text = ocr_result.full_markdown
            image_map = ocr_result.all_images
        else:
            markdown_text = load_file_content(input_file)
        if not markdown_text.strip():
            raise ValueError(f"Chapter input has no readable content: {input_file}")

        # 2. Assemble system prompt rules
        system_instruction = assemble_prompt_files(
            self.rules_dict,
            mode="generate-questions",
            output_format=self.output_format,
            pdf_engine=self.pdf_engine,
        )

        duration_line = ""
        if paper_metadata is None and self.output_format == "pdf" and self.exam_duration_minutes is not None:
            duration_line = f"- Exam Duration: {self.exam_duration_minutes} minutes\n"

        paper_header = ""
        if paper_metadata is not None and self.output_format == "pdf":
            paper_header = (
                f"- Paper Title: {paper_spec.get('paper_title', input_file.stem)}\n\n"
                f"{PaperGenerator._format_paper_metadata(paper_metadata)}\n\n"
            )
        batch_images = [
            ImageAttachment(reference_id=img_name, source=filepath)
            for img_name, filepath in unique_image_items(image_map)
        ]
        intermediate_ext = "tex" if self.pdf_engine == "tex" else "html"
        prompt_dir.mkdir(parents=True, exist_ok=True)
        master_cache_dir = prompt_dir.parent / "masters"
        master_cache_key: Optional[str] = None

        def generate_for_languages(
            languages: List[str],
            output_stem: str,
            label: str,
            *,
            request_question_count: int | None = None,
            request_question_type_contract: str | None = None,
        ) -> str:
            nonlocal master_cache_key
            requested_count = (
                generation_question_count
                if request_question_count is None
                else request_question_count
            )
            lang_instruction, lang_tags = build_language_instructions(
                languages, output_format=self.output_format, pdf_engine=self.pdf_engine
            )
            all_tags = [tag.strip() for tag in self.tags.split(",") if tag.strip()] + lang_tags
            prompt_text = load_prompt_template(
                PROMPTS_DIR / "generator" / "generation_request.md",
                chapter_content=markdown_text,
                paper_header=paper_header,
                question_type_contract=(
                    question_type_contract
                    if request_question_type_contract is None
                    else request_question_type_contract
                ),
                question_count=str(requested_count),
                question_count_contract=(
                    question_count_contract
                    if request_question_count is None
                    else f"- Exact total question count (required): {requested_count}\n"
                ),
                difficulty_mix=self.difficulty_mix,
                difficulty_count_contract=difficulty_count_contract,
                languages=", ".join(languages),
                standards=self.standards,
                global_tags=", ".join(all_tags),
                output_format=self.output_format.upper(),
                pdf_engine=self.pdf_engine.upper(),
                duration_line=duration_line,
                language_instruction=lang_instruction,
            )
            normalized_languages = [
                language.strip().lower()
                for language in languages
                if language and language.strip()
            ]
            is_english_master = bool(self.translate_to) and normalized_languages == [
                "english"
            ]
            cache_path: Optional[Path] = None
            if is_english_master:
                cache_digest = hashlib.sha256()
                cache_digest.update(system_instruction.encode("utf-8"))
                cache_digest.update(prompt_text.encode("utf-8"))
                cache_digest.update(
                    type(self.communicator).__qualname__.encode("utf-8")
                )
                cache_digest.update(
                    str(getattr(self.communicator, "model_name", "")).encode("utf-8")
                )
                for image in batch_images:
                    cache_digest.update(image.reference_id.encode("utf-8"))
                    image_bytes = image.to_bytes()
                    if image_bytes is not None:
                        cache_digest.update(image_bytes)
                    else:
                        cache_digest.update(str(image.source).encode("utf-8"))
                master_cache_key = cache_digest.hexdigest()
                cache_path = master_cache_dir / f"{output_stem}.json"
                if cache_path.is_file() and not self.force_overwrite:
                    cache_payload = json.loads(cache_path.read_text(encoding="utf-8"))
                    if cache_payload.get("cache_key") == master_cache_key:
                        cached_artifact = cache_payload.get("artifact")
                        if not isinstance(cached_artifact, str) or not cached_artifact.strip():
                            raise ValueError(
                                f"English master cache is invalid: {cache_path}"
                            )
                        logger.info("Reusing unchanged English master: %s", cache_path)
                        return cached_artifact

            multimodal_batch = MultimodalBatch(text=prompt_text, images=batch_images)
            extension = intermediate_ext if self.output_format == "pdf" else "xml"
            snapshot_path = prompt_dir / f"{output_stem}_{label}_prompt.md"
            write_prompt_snapshot(snapshot_path, system_instruction, multimodal_batch)
            generated = self.communicator.generate(
                system_instruction=system_instruction,
                contents=multimodal_batch,
                output_filename=f"{output_stem}.{extension}",
                prompt_snapshot_path=snapshot_path,
            )
            if not generated or not generated.strip():
                raise ValueError(
                    f"AI returned an empty response for chapter input: {input_file}"
                )
            if cache_path is not None and master_cache_key is not None:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                temporary_cache_path = cache_path.with_suffix(".json.tmp")
                temporary_cache_path.write_text(
                    json.dumps(
                        {"cache_key": master_cache_key, "artifact": generated},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                temporary_cache_path.replace(cache_path)
            return generated

        configured_languages = [
            language.strip().lower()
            for language in self.languages
            if language and language.strip()
        ] or ["english"]
        english_master_stem = build_generated_output_stem(
            input_file.stem,
            ["english"],
            qualifier=(
                paper_spec.get("exam_name")
                if str(paper_spec.get("exam_name", "")).strip().lower() not in {"", "mock_exam"}
                else self.standards
            ),
        )
        separate_english_master = bool(
            self.translate_to and configured_languages != ["english"]
        )
        if self.separate_by_question_type:
            bilingual_stem = build_generated_output_stem(
                input_file.stem,
                ["english"],
                qualifier=(
                    paper_spec.get("exam_name")
                    if str(paper_spec.get("exam_name", "")).strip().lower()
                    not in {"", "mock_exam"}
                    else self.standards
                ),
            )
            all_output_paths: List[Path] = []
            adaptive_manifest_path: Optional[Path] = (
                paper_dir / f"{generation_stem}_question_types.json"
                if adaptive_type_profile
                else None
            )
            reused_adaptive_manifest = False
            if (
                adaptive_type_profile
                and adaptive_manifest_path is not None
                and adaptive_manifest_path.is_file()
                and not self.force_overwrite
            ):
                try:
                    saved_manifest = json.loads(
                        adaptive_manifest_path.read_text(encoding="utf-8")
                    )
                except json.JSONDecodeError:
                    saved_manifest = None
                    logger.warning(
                        "Ignoring invalid adaptive manifest: %s",
                        adaptive_manifest_path,
                    )
                if isinstance(saved_manifest, dict):
                    source_digest = hashlib.sha256(
                        markdown_text.encode("utf-8")
                    ).hexdigest()
                    saved_source_digest = saved_manifest.get("source_sha256")
                    manifest_matches = (
                        saved_manifest.get("status")
                        in {"in_progress", "completed"}
                        and saved_manifest.get("target_questions")
                        == generation_question_count
                        and saved_manifest.get("spec_sha256")
                        == hashlib.sha256(
                            self.spec_path.read_bytes()
                        ).hexdigest()
                        if self.spec_path is not None
                        else False
                    )
                    manifest_matches = (
                        manifest_matches
                        and saved_manifest.get("languages")
                        == [
                            language.strip().casefold()
                            for language in self.languages
                            if language.strip()
                        ]
                        and saved_manifest.get("translate_to")
                        == [
                            language.strip().casefold()
                            for language in self.translate_to
                        ]
                        and (
                            saved_source_digest is None
                            or saved_source_digest == source_digest
                        )
                    )
                    if manifest_matches:
                        try:
                            separate_type_allocations = (
                                parse_adaptive_question_type_plan(
                                    json.dumps(
                                        {
                                            "allocations": saved_manifest.get(
                                                "question_types"
                                            )
                                        }
                                    ),
                                    adaptive_type_profile,
                                    generation_question_count,
                                )
                            )
                        except ValueError as exc:
                            logger.warning(
                                "Ignoring invalid adaptive allocation in %s: %s",
                                adaptive_manifest_path,
                                exc,
                            )
                        else:
                            reused_adaptive_manifest = True
                            logger.info(
                                "Reusing saved adaptive question-type allocation "
                                "from %s.",
                                adaptive_manifest_path,
                            )
            if adaptive_type_profile:
                allocation_prompt = load_prompt_template(
                    PROMPTS_DIR
                    / "generator"
                    / "question_type_allocation.md",
                    chapter_content=markdown_text,
                    source_filename=input_file.name,
                    target_questions=str(generation_question_count),
                    standards=self.standards,
                    type_profile=json.dumps(
                        [
                            {
                                "type": entry["name"],
                                "id": entry["id"],
                                "description": entry["description"],
                                "weight": weight,
                            }
                            for entry, weight in adaptive_type_profile
                        ],
                        ensure_ascii=False,
                        indent=2,
                    ),
                )
                allocation_snapshot = prompt_dir / (
                    f"{generation_stem}_question_type_allocation_prompt.md"
                )
                allocation_batch = MultimodalBatch(
                    text=allocation_prompt,
                    images=batch_images,
                )
                allocation_system_instruction = (
                    "You are a chapter-specific assessment planner. "
                    "Your only task is to return a valid JSON question-type "
                    "allocation as requested. Do not generate questions or "
                    "Moodle XML."
                )
                write_prompt_snapshot(
                    allocation_snapshot,
                    allocation_system_instruction,
                    allocation_batch,
                )
                if not reused_adaptive_manifest:
                    raw_plan = self.communicator.generate(
                        system_instruction=allocation_system_instruction,
                        contents=allocation_batch,
                        output_filename=(
                            f"{generation_stem}_question_type_allocation.json"
                        ),
                        prompt_snapshot_path=allocation_snapshot,
                    )
                    separate_type_allocations = (
                        parse_adaptive_question_type_plan(
                            raw_plan,
                            adaptive_type_profile,
                            generation_question_count,
                        )
                    )
                allocated_count = sum(
                    count for _, count in separate_type_allocations
                )
                if allocated_count < generation_question_count:
                    logger.info(
                        "Adaptive question-type allocation selected %s of %s "
                        "target questions for this chapter.",
                        allocated_count,
                        generation_question_count,
                    )

            adaptive_manifest: Optional[Dict[str, Any]] = None
            if adaptive_type_profile:
                assert adaptive_manifest_path is not None
                planned_output_names: List[str] = []
                planned_output_counts: Dict[str, int] = {}
                for entry, _ in separate_type_allocations:
                    suffix = re.sub(
                        r"[^a-z0-9]+", "_", entry["name"].casefold()
                    ).strip("_")
                    if not suffix:
                        raise ValueError(
                            "Question type has no usable filename characters: "
                            f"{entry['name']!r}"
                        )
                    expected_type_count = next(
                        count
                        for allocated_entry, count in separate_type_allocations
                        if allocated_entry["name"] == entry["name"]
                    )
                    primary_name = f"{generation_stem}_{suffix}.xml"
                    planned_output_names.append(primary_name)
                    planned_output_counts[primary_name] = expected_type_count
                    if separate_english_master:
                        english_name = f"{english_master_stem}_{suffix}.xml"
                        planned_output_names.append(english_name)
                        planned_output_counts[english_name] = expected_type_count
                    for target_language in self.translate_to:
                        target_suffix = re.sub(
                            r"[^a-z0-9]+",
                            "_",
                            target_language.strip().casefold(),
                        ).strip("_")
                        translated_name = (
                            f"{bilingual_stem}_{target_suffix}_{suffix}.xml"
                        )
                        planned_output_names.append(translated_name)
                        planned_output_counts[translated_name] = expected_type_count
                existing_completed_names = [
                    name
                    for name, count in planned_output_counts.items()
                    if is_valid_moodle_xml_question_file(
                        paper_dir / name,
                        count,
                    )
                ]
                adaptive_manifest = {
                    "status": "in_progress",
                    "target_questions": generation_question_count,
                    "allocated_questions": sum(
                        count for _, count in separate_type_allocations
                    ),
                    "question_types": [
                        {"type": entry["name"], "count": count}
                        for entry, count in separate_type_allocations
                    ],
                    "files": planned_output_names,
                    "completed_files": existing_completed_names,
                    "languages": [
                        language.strip().casefold()
                        for language in self.languages
                        if language.strip()
                    ],
                    "translate_to": [
                        language.strip().casefold()
                        for language in self.translate_to
                    ],
                    "spec_sha256": hashlib.sha256(
                        self.spec_path.read_bytes()
                    ).hexdigest()
                    if self.spec_path is not None
                    else None,
                    "source_sha256": hashlib.sha256(
                        markdown_text.encode("utf-8")
                    ).hexdigest(),
                }
                write_json_atomically(
                    adaptive_manifest_path,
                    adaptive_manifest,
                )

            def persist_adaptive_progress() -> None:
                if adaptive_manifest is None or adaptive_manifest_path is None:
                    return
                adaptive_manifest["completed_files"] = [
                    name
                    for name, count in planned_output_counts.items()
                    if is_valid_moodle_xml_question_file(
                        paper_dir / name,
                        count,
                    )
                ]
                write_json_atomically(
                    adaptive_manifest_path,
                    adaptive_manifest,
                )

            for type_entry, type_question_count in separate_type_allocations:
                type_name = type_entry["name"]
                type_suffix = re.sub(
                    r"[^a-z0-9]+", "_", type_name.casefold()
                ).strip("_")
                if not type_suffix:
                    raise ValueError(
                        f"Question type has no usable filename characters: {type_name!r}"
                    )
                type_contract = build_question_type_contract(
                    {
                        "question_types": {
                            "mode": "exact",
                            "types": [
                                {"type": type_name, "count": type_question_count}
                            ],
                        }
                    },
                    self.mcq_types_path,
                    type_question_count,
                )
                type_generation_stem = f"{generation_stem}_{type_suffix}"
                type_english_stem = f"{english_master_stem}_{type_suffix}"
                primary_path = paper_dir / f"{type_generation_stem}.xml"
                english_master_path = paper_dir / f"{type_english_stem}.xml"

                def output_is_valid(path: Path) -> bool:
                    return (
                        not self.force_overwrite
                        and is_valid_moodle_xml_question_file(
                            path,
                            type_question_count,
                        )
                    )

                primary_is_valid = output_is_valid(primary_path)
                english_master_is_valid = (
                    output_is_valid(english_master_path)
                    if separate_english_master
                    else False
                )

                def require_type_count(artifact: str, label: str) -> None:
                    try:
                        root = ET.fromstring(
                            fix_and_inject_moodle_xml(artifact, image_map)
                        )
                    except ET.ParseError as exc:
                        raise ValueError(
                            f"Generated {label} for question type '{type_name}' "
                            "is not valid Moodle XML."
                        ) from exc
                    actual_count = sum(
                        node.tag == "question" and node.get("type") != "category"
                        for node in root
                    )
                    if actual_count != type_question_count:
                        raise ValueError(
                            f"Generated {actual_count} questions for type "
                            f"'{type_name}', expected exactly {type_question_count}."
                        )

                translation_outputs = [
                    (
                        target_language,
                        paper_dir
                        / (
                            f"{bilingual_stem}_"
                            f"{re.sub('[^a-z0-9]+', '_', target_language.strip().casefold()).strip('_')}_"
                            f"{type_suffix}.xml"
                        ),
                    )
                    for target_language in self.translate_to
                ]
                needs_translation = any(
                    not output_is_valid(path)
                    for _, path in translation_outputs
                )
                type_english_master = (
                    generate_for_languages(
                        ["english"],
                        type_english_stem,
                        "english_master",
                        request_question_count=type_question_count,
                        request_question_type_contract=type_contract,
                    )
                    if separate_english_master
                    and (not english_master_is_valid or needs_translation)
                    else None
                )
                if primary_is_valid:
                    type_raw_output = None
                    all_output_paths.append(primary_path)
                else:
                    type_raw_output = generate_for_languages(
                        self.languages,
                        type_generation_stem,
                        "generation",
                        request_question_count=type_question_count,
                        request_question_type_contract=type_contract,
                    )
                    require_type_count(type_raw_output, "generation output")
                    primary_path = self.renderer.render(
                        type_raw_output,
                        paper_dir,
                        type_generation_stem,
                        image_map,
                    )
                    all_output_paths.append(primary_path)

                if type_english_master is None and english_master_is_valid:
                    all_output_paths.append(english_master_path)

                if type_english_master is not None:
                    require_type_count(type_english_master, "English master")
                    english_master_path = self.renderer.render(
                        type_english_master,
                        paper_dir,
                        type_english_stem,
                        image_map,
                    )
                    all_output_paths.append(english_master_path)

                if separate_english_master and (type_english_master is not None or english_master_is_valid):
                    translation_source = (
                        type_english_master
                        if type_english_master is not None
                        else english_master_path.read_text(encoding="utf-8")
                    )
                elif self.translate_to:
                    translation_source = (
                        type_raw_output
                        if type_raw_output is not None
                        else primary_path.read_text(encoding="utf-8")
                    )
                else:
                    translation_source = ""

                if (
                    self.translate_to
                    and not separate_english_master
                    and configured_languages != ["english"]
                ):
                    raise ValueError(
                        "Separate-by-type translations require English in --languages "
                        "or a separate English-master generation."
                    )

                for target_language in self.translate_to:
                    target_suffix = re.sub(
                        r"[^a-z0-9]+",
                        "_",
                        target_language.strip().casefold(),
                    ).strip("_")
                    translated_stem = (
                        f"{bilingual_stem}_{target_suffix}"
                        f"_{type_suffix}"
                    )
                    translated_path = paper_dir / f"{translated_stem}.xml"
                    metadata_path = (
                        master_cache_dir
                        / f"{translated_stem}.translation.json"
                    )
                    if output_is_valid(translated_path):
                        all_output_paths.append(translated_path)
                        continue
                    cached_translation = False
                    if (
                        not self.force_overwrite
                        and master_cache_key is not None
                        and translated_path.is_file()
                        and translated_path.stat().st_size > 0
                        and metadata_path.is_file()
                    ):
                        metadata = json.loads(
                            metadata_path.read_text(encoding="utf-8")
                        )
                        if (
                            metadata.get("master_cache_key") == master_cache_key
                            and metadata.get("target_language")
                            == target_language.casefold()
                        ):
                            try:
                                translated_root = ET.parse(translated_path).getroot()
                                cached_count = sum(
                                    node.tag == "question"
                                    and node.get("type") != "category"
                                    for node in translated_root
                                )
                                cached_translation = (
                                    translated_root.tag == "quiz"
                                    and cached_count == type_question_count
                                )
                            except ET.ParseError:
                                cached_translation = False
                    if cached_translation:
                        all_output_paths.append(translated_path)
                        continue

                    translated = translate_assessment_artifact(
                        self.communicator,
                        translation_source,
                        target_language,
                        "xml",
                        output_filename=translated_path.name,
                        prompt_snapshot_path=prompt_dir
                        / f"{translated_stem}_translation_prompt.md",
                        force=self.force_overwrite,
                    )
                    require_type_count(translated, "translated output")
                    all_output_paths.append(
                        self.renderer.render(
                            translated,
                            paper_dir,
                            translated_stem,
                            image_map,
                        )
                    )
                    if master_cache_key is not None:
                        metadata_path.parent.mkdir(parents=True, exist_ok=True)
                        temporary_metadata_path = metadata_path.with_suffix(
                            ".json.tmp"
                        )
                        temporary_metadata_path.write_text(
                            json.dumps(
                                {
                                    "master_cache_key": master_cache_key,
                                    "target_language": target_language.casefold(),
                                },
                                ensure_ascii=False,
                            ),
                            encoding="utf-8",
                        )
                        temporary_metadata_path.replace(metadata_path)

                persist_adaptive_progress()

                logger.info(
                    "Saved separate question-type output for %s (%s questions).",
                    type_name,
                    type_question_count,
                )

            if adaptive_manifest is not None and adaptive_manifest_path is not None:
                persist_adaptive_progress()
                if adaptive_manifest["completed_files"] != planned_output_names:
                    missing_outputs = sorted(
                        set(planned_output_names)
                        - set(adaptive_manifest["completed_files"])
                    )
                    raise ValueError(
                        "Adaptive generation did not produce all valid outputs: "
                        + ", ".join(missing_outputs)
                    )
                adaptive_manifest["status"] = "completed"
                write_json_atomically(
                    adaptive_manifest_path,
                    adaptive_manifest,
                )
                all_output_paths.append(adaptive_manifest_path)

            elapsed = time.perf_counter() - item_start
            logger.info(
                "⏱️ [GENERATE-QUESTIONS] %s completed in %.2f seconds "
                "(%.2f minutes)",
                input_file.name,
                elapsed,
                elapsed / 60.0,
            )
            return all_output_paths

        english_master = (
            generate_for_languages(["english"], english_master_stem, "english_master")
            if separate_english_master
            else None
        )
        raw_output = generate_for_languages(
            self.languages, generation_stem, "generation"
        )
        if english_master is None and self.translate_to:
            english_master_stem = generation_stem
            english_master = raw_output

        def render_artifact(artifact: str, output_stem: str) -> List[Path]:
            if self.output_format == "pdf":
                return [
                    self.renderer.render(
                        artifact,
                        paper_dir,
                        output_stem,
                        image_map,
                    )
                ]
            if self.num_papers > 1:
                split_artifacts = self.split_xml_papers(
                    artifact, generation_question_count, self.num_papers
                )
                return [
                    self.renderer.render(
                        xml,
                        paper_dir,
                        f"{output_stem}_paper_{index:02d}",
                        image_map,
                    )
                    for index, xml in enumerate(split_artifacts, start=1)
                ]
            return [
                self.renderer.render(
                    artifact,
                    paper_dir,
                    output_stem,
                    image_map,
                )
            ]

        primary_paths = render_artifact(raw_output, generation_stem)
        all_output_paths = list(primary_paths)
        if separate_english_master and english_master is not None:
            english_paths = render_artifact(english_master, english_master_stem)
            all_output_paths.extend(english_paths)
            logger.info("Saved canonical English question artifact(s): %s", english_paths)

        for target_language in self.translate_to:
            translated_stem = build_generated_output_stem(
                input_file.stem,
                ["english", target_language],
                qualifier=(
                    paper_spec.get("exam_name")
                    if str(paper_spec.get("exam_name", "")).strip().lower() not in {"", "mock_exam"}
                    else self.standards
                ),
            )
            translated_extension = (
                intermediate_ext if self.output_format == "pdf" else "xml"
            )
            translated_final_extension = (
                "pdf" if self.output_format == "pdf" else "xml"
            )
            expected_translated_paths = (
                [
                    paper_dir / f"{translated_stem}_paper_{index:02d}.xml"
                    for index in range(1, self.num_papers + 1)
                ]
                if self.output_format == "xml" and self.num_papers > 1
                else [
                    paper_dir / f"{translated_stem}.{translated_final_extension}"
                ]
            )
            translation_metadata_path = (
                master_cache_dir / f"{translated_stem}.translation.json"
            )
            cached_translation = False
            if (
                self.translate_to
                and not self.force_overwrite
                and master_cache_key is not None
                and translation_metadata_path.is_file()
                and all(
                    path.is_file() and path.stat().st_size > 0
                    for path in expected_translated_paths
                )
            ):
                translation_metadata = json.loads(
                    translation_metadata_path.read_text(encoding="utf-8")
                )
                cached_translation = (
                    translation_metadata.get("master_cache_key") == master_cache_key
                    and translation_metadata.get("target_language")
                    == target_language.casefold()
                )
            if cached_translation:
                translated_paths = expected_translated_paths
                logger.info(
                    "Reusing %s translation generated from the current English master.",
                    target_language,
                )
            else:
                translated = translate_assessment_artifact(
                    self.communicator,
                    english_master or raw_output,
                    target_language,
                    self.output_format,
                    self.pdf_engine,
                    output_filename=f"{translated_stem}.{translated_extension}",
                    prompt_snapshot_path=prompt_dir
                    / f"{translated_stem}_translation_prompt.md",
                    force=self.force_overwrite,
                )
                translated_paths = render_artifact(translated, translated_stem)
                if master_cache_key is not None:
                    master_cache_dir.mkdir(parents=True, exist_ok=True)
                    temporary_metadata_path = translation_metadata_path.with_suffix(
                        ".json.tmp"
                    )
                    temporary_metadata_path.write_text(
                        json.dumps(
                            {
                                "master_cache_key": master_cache_key,
                                "target_language": target_language.casefold(),
                            },
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                    temporary_metadata_path.replace(translation_metadata_path)
            all_output_paths.extend(translated_paths)
            logger.info(
                "Saved English + %s question artifact(s): %s",
                target_language,
                translated_paths,
            )

        elapsed = time.perf_counter() - item_start
        logger.info(
            "⏱️ [GENERATE-QUESTIONS] %s completed in %.2f seconds (%.2f minutes)",
            input_file.name,
            elapsed,
            elapsed / 60.0,
        )
        if self.translate_to:
            return all_output_paths
        if self.output_format == "pdf":
            return primary_paths[0]
        if self.num_papers > 1:
            return primary_paths
        return primary_paths[0]

    def process_directory(self, input_dir: Path, output_dir: Path) -> List[Path]:
        """Processes all chapter PDFs and MDs in an input directory."""
        input_dir = Path(input_dir).resolve()
        source_files = [
            f for f in input_dir.rglob("*")
            if f.suffix.lower() in [".pdf", ".md"]
            and not f.name.startswith("sliced_")
            and not f.name.startswith("temp_")
        ]
        if not source_files:
            logger.warning(f"No valid chapter files (.pdf, .md) found in {input_dir}")
            return []

        directory_start = time.perf_counter()
        results = []
        for file_path in source_files:
            out_path = self.process_file(file_path, output_dir, input_root=input_dir)
            if isinstance(out_path, list):
                results.extend(out_path)
            else:
                results.append(out_path)
        directory_elapsed = time.perf_counter() - directory_start
        logger.info("⏱️ [GENERATE-QUESTIONS DIRECTORY] Processed %d file(s) in %.2f seconds (%.2f minutes)", len(source_files), directory_elapsed, directory_elapsed / 60.0)
        self.ocr_engine.log_cache_summary()
        return results


# =======================================================================
# 3. Syllabus & Mock Exam Question Generator Class
# =======================================================================

class PaperGenerator:
    """Synthesizes complete mock exam papers or question banks from syllabi & specs."""

    @staticmethod
    def _format_paper_metadata(metadata: Dict[str, Any]) -> str:
        """Render normalized metadata using the shared paper contract template."""
        time_allowed = (
            f"{metadata['duration_minutes']} minutes"
            if metadata["duration_minutes"] is not None
            else "Not specified"
        )
        instructions = metadata["instructions"] or [
            "Answer each question according to the instructions printed in the paper.",
            "Select the best answer for every multiple-choice question.",
        ]
        instruction_lines = "\n".join(
            f"{index}. {instruction}" for index, instruction in enumerate(instructions, 1)
        )
        marking_lines = []
        for section in metadata["section_totals"]:
            line = (
                f"- {section['name']}: {section['questions']} questions x "
                f"{section['marks_per_question']:g} marks = {section['section_marks']:g} marks"
            )
            if section["negative_marks"] is not None:
                line += f"; negative marking: {section['negative_marks']}"
            marking_lines.append(line)
        return load_prompt_template(
            PROMPTS_DIR / "generator" / "paper_metadata_contract.md",
            time_allowed=time_allowed,
            total_marks=f"{metadata['total_marks']:g}",
            candidate_fields=", ".join(metadata["header_fields"].keys()),
            instructions=instruction_lines,
            marking_scheme="\n".join(marking_lines),
        )

    def __init__(
        self,
        communicator: BaseAICommunicator,
        ocr_engine: Optional[MistralOCREngine] = None,
        rules_dict: Optional[Dict[str, Any]] = None,
        languages: Optional[List[str]] = None,
        standards: str = "General",
        tags: str = "",
        difficulty_mix: str = "easy:0.2,medium:0.5,hard:0.3",
        output_format: str = "xml",
        pdf_engine: str = "html",
        sample_pdf: Optional[Path] = None,
        staging_dir: Path = Path("output"),
        exam_duration_minutes: Optional[int] = None,
        mcq_types_path: Path = resource_path("catalogs/mcq_types.yaml"),
        watermark_text: str = "",
        header_text: str = "",
        footer_text: str = "",
    ):
        self.communicator = communicator
        self.ocr_engine = ocr_engine or MistralOCREngine()
        self.rules_dict = rules_dict or {}
        self.languages = languages or ["english"]
        self.standards = standards
        self.tags = tags
        self.difficulty_mix = difficulty_mix
        self.output_format = output_format.lower()
        self.pdf_engine = pdf_engine.lower()
        self.watermark_text = watermark_text or ""
        self.header_text = header_text or ""
        self.footer_text = footer_text or ""
        self.renderer = OutputRenderer(
            self.output_format,
            self.pdf_engine,
            watermark_text=self.watermark_text,
            header_text=self.header_text,
            footer_text=self.footer_text,
        )
        self.sample_pdf = sample_pdf
        self.staging_dir = Path(staging_dir)
        self.exam_duration_minutes = exam_duration_minutes
        self.mcq_types_path = Path(mcq_types_path)

    def process_spec(self, spec_path: Path, output_dir: Path) -> Path:
        """Synthesizes a full calibrated mock exam from a spec JSON or syllabus."""
        spec_path = Path(spec_path).resolve()
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        item_start = time.perf_counter()

        if not spec_path.exists():
            raise FileNotFoundError(f"Spec file not found: {spec_path}")

        spec_data = {}
        if spec_path.suffix.lower() in {".yaml", ".yml", ".json"}:
            spec_data = load_yaml_or_json_file(spec_path)
        else:
            spec_data = {
                "exam_name": spec_path.stem.upper(),
                "subjects": [{"name": spec_path.stem, "syllabus_file": str(spec_path), "total_questions": 10}],
            }

        exam_name = spec_data.get("exam_name", "MOCK_EXAM")
        output_stem = "mock_bank" if self.output_format == "xml" else "mock_paper"
        generation_stem = build_generated_output_stem(
            output_stem,
            self.languages,
            qualifier=(
                exam_name
                if str(exam_name).strip().lower() not in {"", "mock_exam"}
                else self.standards
            ),
        )
        subjects = spec_data.get("subjects", [])
        metadata = build_paper_metadata(
            spec_data,
            subjects,
            self.exam_duration_minutes,
        )
        if spec_data.get("instruction_profile"):
            self.rules_dict["instruction_profile"] = spec_data["instruction_profile"]
            self.rules_dict["instruction_file"] = None
        question_type_contract = build_question_type_contract(
            spec_data,
            self.mcq_types_path,
            sum(int(subject.get("total_questions", 0)) for subject in subjects),
        )
        difficulty_contract = build_subject_difficulty_contract(spec_data, subjects)
        global_difficulty_hint = build_global_difficulty_hint(spec_data)

        logger.info(f"\n{'='*60}\n🎓 [GENERATE-PAPER] Exam: {exam_name} ({self.output_format.upper()})\n{'='*60}")

        markdown_dir = output_dir / "markdown"
        prompt_dir = output_dir / ".tmp" / "prompts"
        markdown_dir.mkdir(parents=True, exist_ok=True)
        prompt_dir.mkdir(parents=True, exist_ok=True)

        # 1. Aggregate syllabi
        syllabus_blocks = []
        for subj in subjects:
            subj_name = subj.get("name", "Subject")
            total_qs = subj.get("total_questions", 0)
            s_file = Path(subj.get("syllabus_file", ""))
            syllabus_start = time.perf_counter()

            content = ""
            if s_file.exists():
                if s_file.suffix.lower() == ".pdf":
                    ocr_result = self.ocr_engine.process_pdf(
                        s_file,
                        self.staging_dir / "syllabus" / s_file.stem,
                    )
                    content = ocr_result.full_markdown
                else:
                    content = load_file_content(s_file)
                logger.info(f"  ✓ Loaded syllabus for {subj_name} ({s_file.name})")
            else:
                logger.warning(f"  ⚠️ Syllabus file '{s_file}' not found.")

            syllabus_blocks.append(
                load_prompt_template(
                    PROMPTS_DIR / "generator" / "subject_scope.md",
                    subject_name=subj_name.upper(),
                    question_count=str(total_qs),
                    syllabus_content=content,
                )
            )
            syllabus_elapsed = time.perf_counter() - syllabus_start
            logger.info("⏱️ [GENERATE-PAPER SYLLABUS] %s completed in %.2f seconds (%.2f minutes)", s_file.name or subj_name, syllabus_elapsed, syllabus_elapsed / 60.0)

        all_syllabi_text = ("\n" + "=" * 40 + "\n").join(syllabus_blocks)

        # 2. Assemble system prompt rules
        system_instruction = assemble_prompt_files(
            self.rules_dict,
            mode="generate-paper",
            output_format=self.output_format,
            pdf_engine=self.pdf_engine,
        )

        lang_instruction, lang_tags = build_language_instructions(
            self.languages, output_format=self.output_format, pdf_engine=self.pdf_engine
        )
        all_tags = [t.strip() for t in self.tags.split(",") if t.strip()] + lang_tags

        # 3. Build turn content
        paper_header = ""
        if self.output_format == "pdf":
            paper_header = f"- Paper Title: {spec_data.get('paper_title', exam_name)}\n\n{self._format_paper_metadata(metadata)}\n\n"
        prompt_text = load_prompt_template(
            PROMPTS_DIR / "generator" / "paper_request.md",
            syllabi_text=all_syllabi_text,
            paper_header=paper_header,
            question_type_contract=question_type_contract,
            exam_name=exam_name,
            standards=self.standards,
            languages=", ".join(self.languages),
            difficulty_mix=global_difficulty_hint,
            difficulty_contract=difficulty_contract,
            global_tags=", ".join(all_tags),
            output_format=self.output_format.upper(),
            pdf_engine=self.pdf_engine.upper(),
            language_instruction=lang_instruction,
        )

        multimodal_batch = MultimodalBatch(text=prompt_text)

        intermediate_ext = "tex" if self.pdf_engine == "tex" else "html"
        output_artifact_name = f"{generation_stem}.{intermediate_ext if self.output_format == 'pdf' else 'xml'}"

        # 4. Dispatch to communicator
        snapshot_path = prompt_dir / f"{generation_stem}_prompt.md"
        write_prompt_snapshot(
            snapshot_path,
            system_instruction,
            multimodal_batch,
        )
        raw_output = self.communicator.generate(
            system_instruction=system_instruction,
            contents=multimodal_batch,
            output_filename=output_artifact_name,
            prompt_snapshot_path=snapshot_path,
        )

        # 5. Render the generated artifact
        if self.output_format == "pdf":
            final_pdf_path = self.renderer.render(
                raw_output,
                output_dir,
                generation_stem,
            )
            logger.info(f"✨ Compiled Mock Exam PDF to: {final_pdf_path}")
            elapsed = time.perf_counter() - item_start
            logger.info("⏱️ [GENERATE-PAPER] %s completed in %.2f seconds (%.2f minutes)", spec_path.name, elapsed, elapsed / 60.0)
            return final_pdf_path
        else:
            final_xml_path = self.renderer.render(
                raw_output,
                output_dir,
                generation_stem,
            )
            logger.info(f"✅ Saved Mock Exam Moodle XML to: {final_xml_path}")
            elapsed = time.perf_counter() - item_start
            logger.info("⏱️ [GENERATE-PAPER] %s completed in %.2f seconds (%.2f minutes)", spec_path.name, elapsed, elapsed / 60.0)
            return final_xml_path
