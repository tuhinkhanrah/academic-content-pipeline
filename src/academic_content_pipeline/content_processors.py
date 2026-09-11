#!/usr/bin/env python3
"""
content_processors.py - Core Content Processing Classes.

Classes:
  1. QuestionPaperExtractor : Extracts questions & diagrams from exam paper PDFs.
  2. QuestionGenerator      : Synthesizes calibrated questions from source PDFs/MDs.
  3. PaperGenerator         : Synthesizes full mock exam papers from syllabi & specs.
"""

import json
import logging
import re
import time
from dataclasses import dataclass

import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from PIL import Image

from .ai_communicators import (
    BaseAICommunicator,
    ImageAttachment,
    MultimodalBatch,
    RemoteSandboxBackend,
)
from .mistral_ocr import MistralOCREngine
from .output_renderers import OutputRenderer
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
class SummaryPromptSpec:
    prompt_path: Path
    intermediate_extension: str


class SummaryPromptResolver:
    """Resolve the summary prompt for a specific engine without any fallback path."""

    def __init__(self, prompt_root: Path):
        self.prompt_root = Path(prompt_root)

    def resolve(self, pdf_engine: str) -> SummaryPromptSpec:
        engine_name = str(pdf_engine).lower().strip()
        prompt_path = self.prompt_root / engine_name / "summary_request.md"
        if not prompt_path.exists():
            raise FileNotFoundError(f"Summary prompt not found for PDF engine '{engine_name}'.")
        return SummaryPromptSpec(
            prompt_path=prompt_path,
            intermediate_extension="tex" if engine_name == "tex" else "html",
        )


class SummarySystemPromptResolver:
    """Resolve the summary system prompt for the selected engine without fallback behavior."""

    def __init__(self, prompt_root: Path):
        self.prompt_root = Path(prompt_root)

    def resolve(self, pdf_engine: str) -> Path:
        engine_name = str(pdf_engine).lower().strip()
        prompt_path = self.prompt_root / engine_name / "summary_system.md"
        if not prompt_path.exists():
            raise FileNotFoundError(f"Summary system prompt not found for PDF engine '{engine_name}'.")
        return prompt_path


def build_summary_system_instruction(pdf_engine: str = "html") -> str:
    """Load the engine-specific summary system instruction from the markdown prompt file."""
    resolver = SummarySystemPromptResolver(PROMPTS_DIR / "generator")
    return resolver.resolve(pdf_engine).read_text(encoding="utf-8").strip()


def build_summary_language_instruction(languages: List[str]) -> str:
    """Build summary-specific language guidance from the markdown rules file."""
    clean_langs = [lang.strip().lower() for lang in languages if lang and lang.strip()]
    if not clean_langs:
        clean_langs = ["english"]

    if len(clean_langs) == 1 and clean_langs[0] != "english":
        target_lang = clean_langs[0].capitalize()
        return (
            f"Write the entire summary in {target_lang} only. Use clear academic {target_lang} terminology, preserve the chapter structure, "
            "and keep all mathematical equations, notation, variables, scientific names, and SVG labels unchanged. "
            "Do not add any English summary text, English section labels, or exam-style answer formatting."
        )

    primary_lang = "english" if "english" in clean_langs else clean_langs[0]
    secondary_langs = [lang for lang in clean_langs if lang != primary_lang]

    if not secondary_langs:
        return (
            "Write the summary in English only. Keep the final document in one language, maintain a clear academic tone, "
            "and do not output questions, answer options, or exam-style formatting."
        )

    summary_rules = (PROMPTS_DIR / "generator" / "summary_language_rules.md").read_text(encoding="utf-8").strip()
    summary_rules = summary_rules.replace("{{languages}}", ", ".join(clean_langs))
    if len(clean_langs) > 1:
        target_lang = secondary_langs[0].capitalize()
        summary_rules = summary_rules.replace("target-language", target_lang)
        summary_rules = summary_rules.replace("requested language", f"{target_lang} language")
    elif clean_langs[0] != "english":
        target_lang = clean_langs[0].capitalize()
        summary_rules = summary_rules.replace("target-language", target_lang)
        summary_rules = summary_rules.replace("requested language", f"{target_lang} language")
    return summary_rules


class SummaryGenerator:
    """Generates chapter summaries from PDFs or Markdown files and renders them as HTML/PDF."""

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
        summary_title: Optional[str] = None,
        watermark_text: str = "",
    ):
        self.communicator = communicator
        self.ocr_engine = ocr_engine
        self.rules_dict = rules_dict or {}
        self.languages = languages or ["english"]
        self.standards = standards
        self.tags = tags
        self.output_format = output_format.lower()
        if self.output_format not in {"pdf"}:
            raise ValueError("SummaryGenerator currently supports only PDF output mode.")
        self.pdf_engine = pdf_engine.lower()
        self.watermark_text = watermark_text or ""
        self.summary_prompt_resolver = SummaryPromptResolver(PROMPTS_DIR / "generator")
        self.summary_spec = self.summary_prompt_resolver.resolve(self.pdf_engine)
        self.renderer = OutputRenderer(self.output_format, self.pdf_engine, watermark_text=self.watermark_text)
        self.page_range = page_range
        self.staging_dir = Path(staging_dir)
        self.summary_title = summary_title

    def _resolve_summary_prompt_path(self) -> Path:
        """Resolve the engine-specific summary prompt and fail fast if it is missing."""
        return self.summary_prompt_resolver.resolve(self.pdf_engine).prompt_path

    def process_file(self, input_file: Path, output_dir: Path, input_root: Optional[Path] = None) -> Path:
        """Summarizes a chapter file (.pdf or .md) and writes a final HTML/PDF artifact."""
        input_file = Path(input_file)
        if not input_file.exists():
            raise FileNotFoundError(f"Summary input file does not exist: {input_file}")

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

        generation_stem = build_generated_output_stem(input_file.stem)
        logger.info(
            f"\n{'='*60}\n📘 [GENERATE-SUMMARY] Processing: {input_file.name} ({self.output_format.upper()}) -> {generation_stem}\n{'='*60}"
        )

        image_map: Dict[str, str] = {}
        if input_file.suffix.lower() == ".pdf":
            if self.ocr_engine is None:
                self.ocr_engine = MistralOCREngine()
            markdown_text, image_map = self.ocr_engine.process_pdf(
                pdf_path=input_file,
                img_output_dir=img_output_dir,
                page_range=self.page_range,
                temp_dir=temp_dir,
            )
        else:
            markdown_text = load_file_content(input_file)

        system_instruction = build_summary_system_instruction(self.pdf_engine)
        lang_instruction, lang_tags = build_language_instructions(
            self.languages,
            output_format=self.output_format,
            pdf_engine=self.pdf_engine,
        )
        summary_lang_instruction = build_summary_language_instruction(self.languages)
        language_instruction = "\n\n".join(part for part in [lang_instruction, summary_lang_instruction] if part and part.strip())
        all_tags = [t.strip() for t in self.tags.split(",") if t.strip()] + lang_tags

        summary_title = self.summary_title or input_file.stem.replace("_", " ").replace("-", " ").title()
        prompt_path = self._resolve_summary_prompt_path()
        prompt_text = load_prompt_template(
            prompt_path,
            title=summary_title,
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

        output_artifact_name = f"{generation_stem}.{self.summary_spec.intermediate_extension}"
        snapshot_path = prompt_dir / f"{generation_stem}_summary_prompt.md"
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
        logger.info(f"✨ Compiled summary PDF to: {final_pdf_path}")
        elapsed = time.perf_counter() - item_start
        logger.info("⏱️ [GENERATE-SUMMARY] %s completed in %.2f seconds (%.2f minutes)", input_file.name, elapsed, elapsed / 60.0)
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
        logger.info("⏱️ [GENERATE-SUMMARY DIRECTORY] Processed %d file(s) in %.2f seconds (%.2f minutes)", len(source_files), directory_elapsed, directory_elapsed / 60.0)
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


def find_existing_output(output_dir: Path, stem: str, output_format: str) -> Optional[Path]:
    """Return an existing non-empty generated artifact for the same source file, if any."""
    if output_format == "xml":
        candidates = [
            output_dir / f"{stem}.xml",
        ]
    else:
        candidates = [
            output_dir / f"{stem}.pdf",
        ]

    for candidate in candidates:
        if candidate.exists() and candidate.stat().st_size > 0:
            return candidate
    return None


def build_generated_output_stem(stem: str) -> str:
    """Create a versioned output stem for generation tasks to allow multiple variants."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")[:-3]
    return f"{stem}_{timestamp}"


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
    if mode not in {"exact", "weighted", "automatic"}:
        raise ValueError(f"Unsupported question_types mode: {mode}")

    if mode == "exact":
        total = sum(int(item.get("count", 0)) for _, item in resolved)
        if total != max_questions:
            raise ValueError(
                f"Question type counts ({total}) must equal total_questions ({max_questions})"
            )
    if mode == "weighted":
        weights = [float(item.get("weight", 0)) for _, item in resolved]
        if not weights or abs(sum(weights) - 1.0) > 1e-9:
            raise ValueError("Question type weights must sum to 1.0")

    allocation_lines = []
    for entry, allocation in resolved:
        line = f"- {entry.get('display_name', entry['name'])} ({entry['name']}): {entry['description']}"
        constraints = entry.get("constraints", {})
        if constraints:
            line += f" Constraints: {json.dumps(constraints, sort_keys=True)}."
        if mode == "exact":
            line += f" Requested count: {int(allocation.get('count', 0))}."
        elif mode == "weighted":
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

        existing_output = find_existing_output(paper_dir, pdf_path.stem, self.output_format)
        if existing_output is not None and not self.force_overwrite:
            logger.info(f"⏭️ Skipping {pdf_path.name}: output already exists at {existing_output}")
            return existing_output
        if existing_output is not None and self.force_overwrite:
            logger.info(f"🔄 Reprocessing {pdf_path.name}: force_overwrite enabled; existing output at {existing_output} will be overwritten.")

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

                output_stem = pdf_path.stem
                output_extension = "tex" if self.pdf_engine == "tex" else "html"
                snapshot_path = prompt_dir / f"{output_stem}_extraction_prompt.md"
                write_prompt_snapshot(snapshot_path, system_instruction, multimodal_batch)
                raw_output = self.communicator.generate(
                    system_instruction=system_instruction,
                    contents=multimodal_batch,
                    output_filename=f"{output_stem}_extracted.{output_extension}",
                    prompt_snapshot_path=snapshot_path,
                )
                return OutputRenderer("pdf", self.pdf_engine).render(
                    raw_output,
                    paper_dir,
                    f"{output_stem}_extracted",
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
        output_filepath = paper_dir / f"{pdf_path.stem}.xml"
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
        mcq_types_path: Path = Path("prompts/generator/mcq_types.yaml"),
        watermark_text: str = "",
    ):
        self.communicator = communicator
        self.ocr_engine = ocr_engine or MistralOCREngine()
        self.rules_dict = rules_dict or {}
        self.languages = languages or ["english"]
        self.standards = standards
        self.tags = tags
        self.difficulty_mix = difficulty_mix
        self.num_questions = num_questions
        self.output_format = output_format.lower()
        self.pdf_engine = pdf_engine.lower()
        self.watermark_text = watermark_text or ""
        self.renderer = OutputRenderer(self.output_format, self.pdf_engine, watermark_text=self.watermark_text)
        self.page_range = page_range
        self.staging_dir = Path(staging_dir)
        self.exam_duration_minutes = exam_duration_minutes
        self.spec_path = Path(spec_path).resolve() if spec_path else None
        self.mcq_types_path = Path(mcq_types_path)

    def process_file(self, input_file: Path, output_dir: Path, input_root: Optional[Path] = None) -> Path:
        """Synthesizes questions from a chapter file (.pdf or .md)."""
        input_file = Path(input_file).resolve()
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

        generation_stem = build_generated_output_stem(input_file.stem)
        logger.info(f"\n{'='*60}\n📚 [GENERATE-QUESTIONS] Processing: {input_file.name} ({self.output_format.upper()}) -> {generation_stem}\n{'='*60}")

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
        generation_question_count = (
            int(paper_spec.get("total_questions", self.num_questions))
            if paper_spec
            else self.num_questions
        )
        question_type_contract = build_question_type_contract(
            paper_spec,
            self.mcq_types_path,
            generation_question_count,
        )

        # 1. Read or OCR source content
        image_map: Dict[str, str] = {}
        if input_file.suffix.lower() == ".pdf":
            markdown_text, image_map = self.ocr_engine.process_pdf(
                pdf_path=input_file,
                img_output_dir=img_output_dir,
                page_range=self.page_range,
                temp_dir=temp_dir,
            )
        else:
            markdown_text = load_file_content(input_file)

        # 2. Assemble system prompt rules
        system_instruction = assemble_prompt_files(
            self.rules_dict,
            mode="generate-questions",
            output_format=self.output_format,
            pdf_engine=self.pdf_engine,
        )

        lang_instruction, lang_tags = build_language_instructions(
            self.languages, output_format=self.output_format, pdf_engine=self.pdf_engine
        )
        all_tags = [t.strip() for t in self.tags.split(",") if t.strip()] + lang_tags

        duration_line = ""
        if paper_metadata is None and self.output_format == "pdf" and self.exam_duration_minutes is not None:
            duration_line = f"- Exam Duration: {self.exam_duration_minutes} minutes\n"

        # 3. Build turn content
        paper_header = ""
        if paper_metadata is not None and self.output_format == "pdf":
            paper_header = (
                f"- Paper Title: {paper_spec.get('paper_title', input_file.stem)}\n\n"
                f"{PaperGenerator._format_paper_metadata(paper_metadata)}\n\n"
            )
        prompt_text = load_prompt_template(
            PROMPTS_DIR / "generator" / "generation_request.md",
            chapter_content=markdown_text,
            paper_header=paper_header,
            question_type_contract=question_type_contract,
            question_count=str(generation_question_count),
            difficulty_mix=self.difficulty_mix,
            languages=", ".join(self.languages),
            standards=self.standards,
            global_tags=", ".join(all_tags),
            output_format=self.output_format.upper(),
            pdf_engine=self.pdf_engine.upper(),
            duration_line=duration_line,
            language_instruction=lang_instruction,
        )

        batch_images = [
            ImageAttachment(reference_id=img_name, source=filepath)
            for img_name, filepath in unique_image_items(image_map)
        ]
        multimodal_batch = MultimodalBatch(
            text=prompt_text,
            images=batch_images,
        )

        ext = "pdf" if self.output_format == "pdf" else "xml"
        intermediate_ext = "tex" if self.pdf_engine == "tex" else "html"
        output_artifact_name = f"{generation_stem}.{intermediate_ext if self.output_format == 'pdf' else 'xml'}"

        # 4. Dispatch to communicator
        prompt_dir.mkdir(parents=True, exist_ok=True)
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
                paper_dir,
                generation_stem,
                image_map,
            )
            logger.info(f"✨ Compiled generated PDF to: {final_pdf_path}")
            elapsed = time.perf_counter() - item_start
            logger.info("⏱️ [GENERATE-QUESTIONS] %s completed in %.2f seconds (%.2f minutes)", input_file.name, elapsed, elapsed / 60.0)
            return final_pdf_path
        else:
            final_xml_path = self.renderer.render(
                raw_output,
                paper_dir,
                generation_stem,
                image_map,
            )
            logger.info(f"✅ Saved generated Moodle XML to: {final_xml_path}")
            elapsed = time.perf_counter() - item_start
            logger.info("⏱️ [GENERATE-QUESTIONS] %s completed in %.2f seconds (%.2f minutes)", input_file.name, elapsed, elapsed / 60.0)
            return final_xml_path

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
        mcq_types_path: Path = Path("prompts/generator/mcq_types.yaml"),
        watermark_text: str = "",
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
        self.renderer = OutputRenderer(self.output_format, self.pdf_engine, watermark_text=self.watermark_text)
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

        exam_name = spec_path.stem.upper() if spec_path.suffix.lower() not in {".yaml", ".yml", ".json"} else "MOCK_EXAM"
        if spec_path.suffix.lower() in {".yaml", ".yml", ".json"}:
            try:
                spec_data = load_yaml_or_json_file(spec_path)
                exam_name = spec_data.get("exam_name", exam_name)
            except Exception:
                pass
        output_stem = f"mock_{exam_name.lower()}_bank" if self.output_format == "xml" else f"mock_{exam_name.lower()}_paper"
        generation_stem = build_generated_output_stem(output_stem)

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
                    content, _ = self.ocr_engine.process_pdf(s_file, self.staging_dir / "syllabus" / s_file.stem)
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
