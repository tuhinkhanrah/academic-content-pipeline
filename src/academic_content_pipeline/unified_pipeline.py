#!/usr/bin/env python3
"""
unified_pipeline.py - Master Unified Academic Content Pipeline CLI.

Modes of Communication:
  1. context : Rolling chat session with memory span
  2. agent   : Managed agent session with environment ID
  3. remote  : Remote sandbox execution with GCS staging

Functionalities:
  1. extract               : Extract questions from PDF question papers via Mistral OCR
  2. generate-questions     : Synthesize questions from chapter PDFs/MDs
  3. generate-paper         : Synthesize full mock exams from YAML specs / syllabi
  4. generate-tutorial      : Generate exam-oriented tutorials with practice questions
  5. translate-artifacts    : Translate reviewed English XML, HTML, and TeX artifacts

Review-first translation example:
  python -m academic_content_pipeline.unified_pipeline agent translate-artifacts
      --input-dir ./reviewed-english --output-dir ./translated --translate-to bengali
"""

import argparse
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    src_root = Path(__file__).resolve().parents[1]
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

from google import genai

from academic_content_pipeline.ai_communicators import (
    AgentSessionBackend,
    BaseAICommunicator,
    BatchAPIBackend,
    ContextChatBackend,
    RemoteSandboxBackend,
    SingleShotBackend,
)
from academic_content_pipeline.content_processors import (
    PaperGenerator,
    QuestionGenerator,
    QuestionPaperExtractor,
    TutorialGenerator,
)
from academic_content_pipeline.paper_batch import PaperBatchRunner, is_batch_paper_spec
from academic_content_pipeline.artifact_translator import (
    is_valid_moodle_xml_file,
    translate_assessment_artifact,
)
from academic_content_pipeline.mistral_ocr import MistralOCREngine
from academic_content_pipeline.pipeline_utils import compile_html_to_pdf, setup_logger
from academic_content_pipeline.resource_paths import resource_path
from academic_content_pipeline.template_tools import (
    DEFAULT_MANIFEST_PATH,
    DEFAULT_TEMPLATE_DIR,
    TemplateValidationError,
    format_validation_errors,
    generate_exam_templates,
    validate_exam_templates,
)

logger = logging.getLogger("academic_content_pipeline")


class GeneratorPromptResolver:
    """Resolve built-in generator prompts for a single engine without fallback logic."""

    def __init__(self, prompt_root: Path):
        self.prompt_root = Path(prompt_root)

    def resolve(self, template_name: str, *, output_format: str, pdf_engine: str) -> Path:
        engine_dir = "xml" if output_format == "xml" else pdf_engine
        prompt_path = self.prompt_root / engine_dir / f"{template_name}.md"
        if not prompt_path.exists():
            raise FileNotFoundError(f"Generator prompt '{template_name}' not found for engine '{engine_dir}'.")
        return prompt_path


def resolve_default_extractor_prompt(standards: str, output_format: str, pdf_engine: str) -> Path:
    """Return the correct built-in extractor prompt for the selected standard and output format."""
    standard_name = str(standards or "general").strip().lower().replace("_", " ")
    if output_format != "xml":
        return Path("prompts/extractor") / pdf_engine / "extractor.md"

    prompt_map = {
        "neet": Path("prompts/extractor/neet_ug.md"),
        "neet ug": Path("prompts/extractor/neet_ug.md"),
        "neet_ug": Path("prompts/extractor/neet_ug.md"),
        "wbjee": Path("prompts/extractor/wbjee.md"),
        "jee main": Path("prompts/extractor/jee_main.md"),
        "jee advanced": Path("prompts/extractor/jee_advanced.md"),
    }

    candidates = [standard_name, standard_name.replace(" ", "_")]
    for candidate in candidates:
        if candidate in prompt_map:
            return prompt_map[candidate]

    for key, path in prompt_map.items():
        if key in standard_name or standard_name in key:
            return path

    return Path("prompts/extractor/neet_ug.md")


def collect_translation_inputs(
    parser: argparse.ArgumentParser,
    input_file: Path | None,
    input_dir: Path | None,
    output_dir: Path,
) -> tuple[list[Path], Path | None]:
    """Validate translation inputs and return files plus their optional root."""
    if bool(input_file) == bool(input_dir):
        parser.error("Specify exactly one of --input-file or --input-dir.")

    resolved_output_dir = output_dir.expanduser().resolve()
    if input_file is not None:
        source_file = input_file.expanduser().resolve()
        if not source_file.is_file():
            parser.error(f"Input artifact does not exist: {source_file}")
        if source_file.suffix.lower() not in {".xml", ".html", ".tex"}:
            parser.error("Translation input must be an .xml, .html, or .tex file.")
        return [source_file], None

    source_dir = input_dir.expanduser().resolve()
    if not source_dir.is_dir():
        parser.error(f"Input directory does not exist: {source_dir}")
    if resolved_output_dir == source_dir or source_dir in resolved_output_dir.parents:
        parser.error(
            "--output-dir must be outside --input-dir to prevent translated files "
            "from being picked up as English inputs."
        )

    artifact_files = sorted(
        path
        for path in source_dir.rglob("*")
        if path.is_file()
        and path.suffix.lower() in {".xml", ".html", ".tex"}
        and not any(part in {".tmp", "__pycache__"} for part in path.relative_to(source_dir).parts)
    )
    if not artifact_files:
        parser.error(f"No XML, HTML, or TeX artifacts found under: {source_dir}")
    return artifact_files, source_dir


def build_translation_output_stem(source_stem: str, target_language: str) -> str:
    """Keep the English-master marker and append a normalized target-language token."""
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


def add_common_options(parser: argparse.ArgumentParser) -> None:
    """Adds shared options across all subcommands."""
    parser.add_argument("--languages", default="english", help="Target languages (e.g. english,bengali,hindi).")
    parser.add_argument("--standards", default="general", help="Target standards (e.g. neet_ug, jee_main).")
    parser.add_argument("--tags", default="", help="Global tags (e.g. year:2026,source:allen).")
    parser.add_argument("--output-dir", default="./output", type=Path, help="Directory for generated outputs.")
    parser.add_argument("--output-format", choices=["xml", "pdf"], default="xml", help="Output format: xml or pdf.")
    parser.add_argument("--pdf-engine", choices=["html", "tex"], default="html", help="PDF engine: html (Chrome) or tex (XeLaTeX).")
    parser.add_argument("--log-file", type=Path, default=None, help="Path to write log outputs.")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose debug logs.")

    # Core system prompt files
    parser.add_argument("--prompt", type=Path, default=None, help="Path to the main prompt/exam profile markdown.")
    parser.add_argument("--instruction-file", type=Path, default=None, help="Path to exam instruction file.")
    parser.add_argument("--xml-rules", type=Path, default="prompts/core/moodle_xml_rules.md")
    parser.add_argument("--xml-extraction-rules", type=Path, default="prompts/core/moodle_xml_extraction_rules.md")
    parser.add_argument("--tags-rules", type=Path, default="prompts/core/naming_and_tags_rules.md")
    parser.add_argument("--templates", type=Path, default="prompts/core/moodle_xml_templates.md")
    parser.add_argument("--pdf-rules", type=Path, default=None)
    parser.add_argument("--pdf-rules-html", type=Path, default="prompts/core/pdf_html_rules.md")
    parser.add_argument("--pdf-rules-tex", type=Path, default="prompts/core/pdf_tex_rules.md")
    parser.add_argument(
        "--reasoning-rules",
        type=Path,
        default="prompts/core/reasoning_rules.md",
        help="Shared step-by-step reasoning and solution rules.",
    )
    parser.add_argument("--mcq-types", type=Path, default=resource_path("catalogs/mcq_types.yaml"), help="MCQ type registry YAML file.")

    # Communicator specific tuning
    parser.add_argument("--model-name", default="gemini-flash-latest", help="Gemini model name.")
    parser.add_argument("--ocr-model", "--model", dest="ocr_model", default=os.environ.get("MISTRAL_OCR_MODEL", "mistral-ocr-latest"), help="Mistral OCR model name to use.")
    parser.add_argument("--agent-name", default="antigravity-preview-09-2026", help="Agent identifier.")
    parser.add_argument("--memory-span", type=int, default=3, help="Rolling turn history memory span for chat context.")
    parser.add_argument("--rate-limit-delay", type=float, default=6.0, help="Delay in seconds between page requests to stay under TPM limits.")
    parser.add_argument("--retry-limit", type=int, default=5, help="Max retry attempts per page on API/quota error.")
    parser.add_argument("--batch-size", type=int, default=5, help="Maximum pages per extraction request (default: 5); 0 processes the entire document in one batch.")
    parser.add_argument("--force", action="store_true", help="Force regeneration of existing or cached outputs.")
    parser.add_argument("--disable-ocr-cache", action="store_true", help="Disable the local OCR cache and force a fresh Mistral OCR run.")
    parser.add_argument("--bucket-name", default=None, help="GCS bucket name for remote sandbox staging.")
    parser.add_argument("--watermark-text", default="", help="Optional text overlay added to generated PDF pages.")
    parser.add_argument("--header-text", default="", help="Optional repeating header text for HTML-rendered PDFs.")
    parser.add_argument("--footer-text", default="", help="Optional repeating footer text for HTML-rendered PDFs.")


def add_task_subparsers(subparser_dest: Any) -> None:
    """Attach extraction, generation, translation, and tutorial task subparsers."""

    # 1. extract
    p_ext = subparser_dest.add_parser("extract", help="Extract questions from exam paper PDFs via Mistral OCR.")
    add_common_options(p_ext)
    p_ext.add_argument("--input-dir", type=Path, default=None, help="Directory containing input PDFs.")
    p_ext.add_argument("--input-file", type=Path, default=None, help="Single input PDF file.")
    p_ext.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"), help="Page range to extract (1-based).")
    p_ext.add_argument(
        "--replace-pages",
        type=int,
        nargs=2,
        metavar=("START", "END"),
        help="Re-extract and replace saved extraction batches covering this page range; requires a checkpoint.",
    )
    p_ext.add_argument("--no-instruction-page", action="store_true", help="Skip front instruction page.")
    p_ext.add_argument("--instruction-page", type=int, default=1, help="Index of front instruction page (1-based).")
    p_ext.add_argument(
        "--resume",
        action="store_true",
        help="Resume extraction from validated per-batch checkpoints.",
    )
    p_ext.add_argument(
        "--instruction-page-model-name",
        default=None,
        help="Gemini model for instruction-page classification (defaults to --model-name).",
    )
    p_ext.add_argument("--verify-online", action="store_true", help="Verify answers online.")

    # 2. generate-questions
    p_chap = subparser_dest.add_parser("generate-questions", help="Synthesize practice questions from chapter PDFs/MDs.")
    add_common_options(p_chap)
    p_chap.add_argument("--input-dir", type=Path, default=None, help="Directory containing chapter PDFs or MDs.")
    p_chap.add_argument("--input-file", type=Path, default=None, help="Single chapter PDF or MD file.")
    p_chap.add_argument("--spec", type=Path, default=None, help="Optional YAML paper spec for a formal PDF header.")
    p_chap.add_argument("--exam-duration-minutes", type=int, default=None, help="Exam duration in minutes for PDF output only.")
    p_chap.add_argument("--num-questions", type=int, default=5, help="Number of questions to synthesize.")
    p_chap.add_argument("--num-papers", type=int, default=1, help="Split the generated XML questions across this many papers.")
    p_chap.add_argument(
        "--separate-by-question-type",
        action="store_true",
        help="Generate each exact or adaptively planned question type into its own Moodle XML file.",
    )
    p_chap.add_argument("--difficulty-mix", default="easy:0.2,medium:0.5,hard:0.3", help="Difficulty ratio breakdown for generated questions.")
    p_chap.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"), help="Page range for chapter PDF.")
    p_chap.add_argument(
        "--translate-to",
        default="",
        help="After normal generation, create English-master bilingual output(s) for comma-separated languages.",
    )

    # 3. translate reviewed artifacts
    p_translate = subparser_dest.add_parser(
        "translate-artifacts",
        help="Translate reviewed English XML, HTML, or TeX artifacts without regenerating them.",
    )
    add_common_options(p_translate)
    p_translate.add_argument("--input-dir", type=Path, default=None, help="Directory containing reviewed English artifacts.")
    p_translate.add_argument("--input-file", type=Path, default=None, help="Single reviewed English XML, HTML, or TeX artifact.")
    p_translate.add_argument(
        "--translate-to",
        required=True,
        help="Comma-separated target languages, for example bengali,hindi.",
    )
    p_translate.add_argument(
        "--xml-chunk-size",
        type=int,
        default=5,
        help="Questions per AI request when translating Moodle XML (default: 5).",
    )

    # 4. generate-tutorial
    p_tutorial = subparser_dest.add_parser(
        "generate-tutorial",
        help="Generate an exam-oriented chapter tutorial with practice questions and solutions.",
    )
    add_common_options(p_tutorial)
    p_tutorial.add_argument("--input-dir", type=Path, default=None, help="Directory containing chapter PDFs or MDs.")
    p_tutorial.add_argument("--input-file", type=Path, default=None, help="Single chapter PDF or MD file.")
    p_tutorial.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"), help="Page range for chapter PDF.")
    p_tutorial.add_argument("--title", default=None, help="Optional title for the generated tutorial document.")
    p_tutorial.add_argument(
        "--render-pdf",
        action="store_true",
        help="Render existing HTML input to PDF without running AI generation; save the PDF beside the HTML.",
    )

    # 5. generate-paper
    p_syl = subparser_dest.add_parser("generate-paper", help="Synthesize mock exams from specs / syllabi.")
    add_common_options(p_syl)
    p_syl.add_argument("--spec", type=Path, default=None, help="Path to YAML spec or syllabus markdown/pdf.")
    p_syl.add_argument("--sample-pdf", type=Path, default=None, help="Optional sample exam PDF for pattern matching.")
    p_syl.add_argument("--exam-duration-minutes", type=int, default=None, help="Exam duration in minutes for PDF output only.")
    p_syl.add_argument("--difficulty-mix", default="easy:0.2,medium:0.5,hard:0.3", help="Difficulty ratio breakdown.")
    p_syl.add_argument("--num-tests", type=int, default=None, help="Generate this many test sets from the spec.")
    p_syl.add_argument("--resume", action="store_true", help="Resume a checkpointed batch-paper run.")
    p_syl.add_argument("--max-paper-attempts", type=int, default=3, help="Maximum generation attempts per paper in this run.")


def build_communicator(mode: str, args: argparse.Namespace) -> BaseAICommunicator:
    """Builds the AI communication backend matching the selected mode."""
    genai_client = None
    if os.environ.get("GEMINI_API_KEY"):
        genai_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    else:
        logger.warning(
            "GEMINI_API_KEY is not set. The communicator object is created without a client; "
            "real generation calls will fail until the key is configured."
        )

    if mode == "context":
        return ContextChatBackend(
            client=genai_client,
            model_name=args.model_name,
            memory_span=args.memory_span,
            attempt_limit=args.retry_limit,
            verbose=args.verbose,
        )
    elif mode == "agent":
        return AgentSessionBackend(
            client=genai_client,
            agent_name=args.agent_name,
            model_name=args.model_name,
            attempt_limit=args.retry_limit,
            verbose=args.verbose,
        )
    elif mode == "remote":
        bucket_name = args.bucket_name or os.environ.get("GCS_BUCKET_NAME")
        if not bucket_name:
            logger.error(
                "GCS bucket name must be supplied via --bucket-name or "
                "GCS_BUCKET_NAME environment variable for remote mode."
            )
            sys.exit(1)
        return RemoteSandboxBackend(
            client=genai_client,
            bucket_name=bucket_name,
            agent_name=args.agent_name,
            attempt_limit=args.retry_limit,
            verbose=args.verbose,
        )
    elif mode == "batch":
        return BatchAPIBackend(
            client=genai_client,
            model_name=args.model_name,
            attempt_limit=args.retry_limit,
            verbose=args.verbose,
        )
    else:
        raise ValueError(f"Unknown mode: {mode}")


def main():
    parser = argparse.ArgumentParser(
        description="Unified Academic Content Pipeline CLI - Modular Multi-mode Question Processing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Allow specifying communication mode as top-level command or via --mode flag
    top_subparsers = parser.add_subparsers(dest="top_command", required=True)

    # 1. Mode-first syntax: `pipeline.py context <task>`, `pipeline.py agent <task>`, `pipeline.py remote <task>`
    p_context = top_subparsers.add_parser("context", help="Execute using Multi-turn Chat Context (rolling memory_span).")
    ctx_sub = p_context.add_subparsers(dest="task", required=True)
    add_task_subparsers(ctx_sub)

    p_agent = top_subparsers.add_parser("agent", help="Execute using Managed Agent Session (with environment ID).")
    agent_sub = p_agent.add_subparsers(dest="task", required=True)
    add_task_subparsers(agent_sub)

    p_remote = top_subparsers.add_parser("remote", help="Execute using Remote Agent Sandbox (with GCS Staging).")
    remote_sub = p_remote.add_subparsers(dest="task", required=True)
    add_task_subparsers(remote_sub)

    p_batch = top_subparsers.add_parser("batch", help="Execute using Gemini Batch API requests.")
    batch_sub = p_batch.add_subparsers(dest="task", required=True)
    add_task_subparsers(batch_sub)

    p_generate_templates = top_subparsers.add_parser(
        "generate-templates",
        help="Generate paper-batch and subject chapter-bank templates from exam specs.",
    )
    p_generate_templates.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="Structured exam manifest whose paper specs drive derived templates.",
    )
    p_generate_templates.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_TEMPLATE_DIR,
        help="Directory to write paper, batch, and chapter-bank templates.",
    )
    p_generate_templates.add_argument(
        "--force",
        action="store_true",
        help="Replace existing generated template files.",
    )

    p_validate_templates = top_subparsers.add_parser(
        "validate-templates",
        help="Validate exam paper specs and generated batch/chapter-bank templates.",
    )
    p_validate_templates.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="Structured exam manifest to validate against.",
    )
    p_validate_templates.add_argument(
        "--template-dir",
        type=Path,
        default=DEFAULT_TEMPLATE_DIR,
        help="Directory containing paper, batch, and chapter-bank templates.",
    )

    # 2. Task-first direct syntax: `pipeline.py extract --mode ...`
    p_direct_ext = top_subparsers.add_parser("extract", help="Extract questions from PDF question papers.")
    p_direct_ext.add_argument("--mode", choices=["context", "agent", "remote", "batch"], default="context")
    add_common_options(p_direct_ext)
    p_direct_ext.add_argument("--input-dir", type=Path, default=None)
    p_direct_ext.add_argument("--input-file", type=Path, default=None)
    p_direct_ext.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"))
    p_direct_ext.add_argument(
        "--replace-pages",
        type=int,
        nargs=2,
        metavar=("START", "END"),
        help="Re-extract and replace saved extraction batches covering this page range; requires a checkpoint.",
    )
    p_direct_ext.add_argument("--no-instruction-page", action="store_true")
    p_direct_ext.add_argument("--instruction-page", type=int, default=1)
    p_direct_ext.add_argument(
        "--resume",
        action="store_true",
        help="Resume extraction from validated per-batch checkpoints.",
    )
    p_direct_ext.add_argument(
        "--instruction-page-model-name",
        default=None,
        help="Gemini model for instruction-page classification (defaults to --model-name).",
    )
    p_direct_ext.add_argument("--verify-online", action="store_true")

    p_direct_chap = top_subparsers.add_parser("generate-questions", help="Synthesize questions from chapter documents.")
    p_direct_chap.add_argument("--mode", choices=["context", "agent", "remote", "batch"], default="context")
    add_common_options(p_direct_chap)
    p_direct_chap.add_argument("--input-dir", type=Path, default=None)
    p_direct_chap.add_argument("--input-file", type=Path, default=None)
    p_direct_chap.add_argument("--spec", type=Path, default=None, help="Optional YAML paper spec for a formal PDF header.")
    p_direct_chap.add_argument("--exam-duration-minutes", type=int, default=None)
    p_direct_chap.add_argument("--num-questions", type=int, default=5)
    p_direct_chap.add_argument("--num-papers", type=int, default=1, help="Split the generated XML questions across this many papers.")
    p_direct_chap.add_argument(
        "--separate-by-question-type",
        action="store_true",
        help="Generate each exact or adaptively planned question type into its own Moodle XML file.",
    )
    p_direct_chap.add_argument("--difficulty-mix", default="easy:0.2,medium:0.5,hard:0.3")
    p_direct_chap.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"))
    p_direct_chap.add_argument(
        "--translate-to",
        default="",
        help="After generation, create bilingual output(s) immediately for comma-separated languages.",
    )

    p_direct_translate = top_subparsers.add_parser(
        "translate-artifacts",
        help="Translate reviewed English XML, HTML, or TeX artifacts.",
    )
    p_direct_translate.add_argument(
        "--mode", choices=["context", "agent", "remote", "batch"], default="context"
    )
    add_common_options(p_direct_translate)
    p_direct_translate.add_argument("--input-dir", type=Path, default=None)
    p_direct_translate.add_argument("--input-file", type=Path, default=None)
    p_direct_translate.add_argument(
        "--translate-to",
        required=True,
        help="Comma-separated target languages, for example bengali,hindi.",
    )
    p_direct_translate.add_argument(
        "--xml-chunk-size",
        type=int,
        default=5,
        help="Questions per AI request when translating Moodle XML (default: 5).",
    )

    p_direct_tutorial = top_subparsers.add_parser(
        "generate-tutorial",
        help="Generate an exam-oriented chapter tutorial with practice questions and solutions.",
    )
    p_direct_tutorial.add_argument("--mode", choices=["context", "agent", "remote", "batch"], default="context")
    add_common_options(p_direct_tutorial)
    p_direct_tutorial.add_argument("--input-dir", type=Path, default=None)
    p_direct_tutorial.add_argument("--input-file", type=Path, default=None)
    p_direct_tutorial.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"))
    p_direct_tutorial.add_argument("--title", default=None, help="Optional title for the generated tutorial document.")
    p_direct_tutorial.add_argument(
        "--render-pdf",
        action="store_true",
        help="Render existing HTML input to PDF without running AI generation; save the PDF beside the HTML.",
    )

    p_direct_syl = top_subparsers.add_parser("generate-paper", help="Synthesize mock exams.")
    p_direct_syl.add_argument("--mode", choices=["context", "agent", "remote", "batch"], default="context")
    add_common_options(p_direct_syl)
    p_direct_syl.add_argument("--spec", type=Path, default=None)
    p_direct_syl.add_argument("--sample-pdf", type=Path, default=None)
    p_direct_syl.add_argument("--exam-duration-minutes", type=int, default=None)
    p_direct_syl.add_argument("--difficulty-mix", default="easy:0.2,medium:0.5,hard:0.3")
    p_direct_syl.add_argument("--num-tests", type=int, default=None, help="Generate this many test sets from the spec.")
    p_direct_syl.add_argument("--resume", action="store_true", help="Resume a checkpointed batch-paper run.")
    p_direct_syl.add_argument("--max-paper-attempts", type=int, default=3, help="Maximum generation attempts per paper in this run.")

    args = parser.parse_args()

    if args.top_command == "generate-templates":
        try:
            generated_paths = generate_exam_templates(
                manifest_path=args.manifest,
                output_dir=args.output_dir,
                force=args.force,
            )
        except (FileExistsError, FileNotFoundError, TemplateValidationError) as exc:
            parser.error(str(exc))
        print(f"Generated {len(generated_paths)} exam templates in {args.output_dir.resolve()}")
        return

    if args.top_command == "validate-templates":
        try:
            validation_errors = validate_exam_templates(
                manifest_path=args.manifest,
                template_dir=args.template_dir,
            )
        except (FileNotFoundError, TemplateValidationError) as exc:
            parser.error(str(exc))
        if validation_errors:
            print("Template validation failed:")
            print(format_validation_errors(validation_errors))
            sys.exit(1)
        print(f"Exam templates are valid: {args.template_dir.resolve()}")
        return

    # Determine mode and task based on syntax used
    if args.top_command in ["context", "agent", "remote", "batch"]:
        mode = args.top_command
        task = args.task
    else:
        mode = getattr(args, "mode", "context")
        task = args.top_command

    translate_to = [
        language.strip()
        for language in getattr(args, "translate_to", "").split(",")
        if language.strip()
    ]
    if task == "generate-questions" and translate_to:
        if any(language.casefold() == "english" for language in translate_to):
            parser.error("--translate-to accepts non-English language names only.")
        configured_languages = {
            language.strip().casefold()
            for language in args.languages.split(",")
            if language.strip()
        }
        conflicting_targets = [
            language for language in translate_to
            if language.casefold() in configured_languages
            and language.casefold() != "english"
        ]
        if conflicting_targets:
            parser.error(
                "Do not request the same non-English language in both --languages "
                f"and --translate-to: {', '.join(conflicting_targets)}."
            )
    if task == "generate-questions" and args.separate_by_question_type:
        if args.spec is None:
            parser.error(
                "--separate-by-question-type requires --spec with exact counts "
                "or an adaptive question-type profile."
            )
        if args.output_format != "xml":
            parser.error("--separate-by-question-type requires --output-format xml.")
        if args.num_papers != 1:
            parser.error(
                "--separate-by-question-type cannot be combined with --num-papers."
            )
    if task == "generate-paper":
        if args.num_tests is not None and args.num_tests < 1:
            parser.error("--num-tests must be at least 1.")
        if args.max_paper_attempts < 1:
            parser.error("--max-paper-attempts must be at least 1.")
        if args.spec is not None:
            batch_paper_run = is_batch_paper_spec(args.spec, args.num_tests)
            if args.resume and not batch_paper_run:
                parser.error("--resume is only supported for batch-paper specs or --num-tests.")
            if batch_paper_run and args.output_format != "xml":
                parser.error("Checkpointed batch-paper generation currently requires --output-format xml.")
            if args.max_paper_attempts != 3 and not batch_paper_run:
                parser.error("--max-paper-attempts is only supported for batch-paper generation.")
        elif args.resume or args.num_tests is not None:
            parser.error("--resume and --num-tests require --spec.")

    translation_files: list[Path] = []
    translation_input_root: Path | None = None
    if task == "translate-artifacts":
        if args.xml_chunk_size < 1:
            parser.error("--xml-chunk-size must be at least 1.")
        if not translate_to:
            parser.error("Provide at least one target language with --translate-to.")
        if any(language.casefold() == "english" for language in translate_to):
            parser.error("--translate-to accepts non-English language names only.")
        normalized_targets = [language.casefold() for language in translate_to]
        if len(normalized_targets) != len(set(normalized_targets)):
            parser.error("--translate-to contains duplicate target languages.")
        translation_files, translation_input_root = collect_translation_inputs(
            parser,
            args.input_file,
            args.input_dir,
            args.output_dir,
        )

    # Setup Logging
    setup_logger(args.log_file, verbose=args.verbose)

    runtime_config = {k: v for k, v in vars(args).items() if not callable(v)}
    logger.info("CLI runtime configuration: %s", runtime_config)

    if getattr(args, "render_pdf", False):
        if task != "generate-tutorial":
            parser.error("--render-pdf is only supported with generate-tutorial.")
        if args.pdf_engine != "html":
            parser.error("--render-pdf requires --pdf-engine html.")
        if bool(args.input_file) == bool(args.input_dir):
            parser.error("Specify exactly one of --input-file or --input-dir with --render-pdf.")

        if args.input_file:
            input_path = args.input_file
            if not input_path.is_file():
                parser.error(f"HTML input file does not exist: {input_path}")
            if input_path.suffix.lower() != ".html":
                parser.error(f"--render-pdf requires an .html input file: {input_path}")
            html_files = [input_path]
        else:
            input_dir = args.input_dir
            if not input_dir.is_dir():
                parser.error(f"HTML input directory does not exist: {input_dir}")
            html_files = sorted(
                path for path in input_dir.rglob("*")
                if path.is_file() and path.suffix.lower() == ".html"
            )
            if not html_files:
                parser.error(f"No HTML files found under: {input_dir}")

        failures = 0
        for html_path in html_files:
            output_pdf = html_path.with_suffix(".pdf")
            temporary_html_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    suffix=".html",
                    prefix=f".{html_path.stem}.render-",
                    dir=html_path.parent,
                    delete=False,
                ) as temporary_html:
                    temporary_html_path = Path(temporary_html.name)
                compile_html_to_pdf(
                    html_path.read_text(encoding="utf-8"),
                    output_pdf,
                    watermark_text=args.watermark_text,
                    header_text=args.header_text,
                    footer_text=args.footer_text,
                    html_output_path=temporary_html_path,
                )
                logger.info("Rendered %s -> %s", html_path, output_pdf)
            except Exception:
                failures += 1
                logger.exception("Failed to render HTML tutorial PDF: %s", html_path)
            finally:
                if temporary_html_path is not None:
                    temporary_html_path.unlink(missing_ok=True)

        logger.info(
            "PDF-only tutorial rendering complete: %d succeeded, %d failed.",
            len(html_files) - failures,
            failures,
        )
        if failures:
            sys.exit(1)
        return

    # Verify environment keys
    if not os.environ.get("GEMINI_API_KEY"):
        logger.error("GEMINI_API_KEY environment variable must be set.")
        sys.exit(1)
    if task != "translate-artifacts" and not os.environ.get("MISTRAL_API_KEY"):
        logger.error("MISTRAL_API_KEY environment variable must be set.")
        sys.exit(1)

    # Initialize communication backend and only create OCR resources for OCR tasks.
    communicator = build_communicator(mode, args)
    ocr_engine = (
        None
        if task == "translate-artifacts"
        else MistralOCREngine(
            model_name=getattr(args, "ocr_model", None),
            enable_cache=not getattr(args, "disable_ocr_cache", False),
        )
    )

    # Auto-infer PDF engine and prompt defaults if not explicitly set
    if getattr(args, "output_format", "xml") == "pdf":
        # If user explicitly supplied tex rules or tex prompt, auto-set pdf_engine to tex
        if (args.pdf_rules_tex and "pdf_tex_rules.md" in str(args.pdf_rules_tex) and "--pdf-rules-tex" in sys.argv) or (args.prompt and "tex" in str(args.prompt)):
            if "--pdf-engine" not in sys.argv:
                args.pdf_engine = "tex"

    def resolve_format_prompt(prompt_path: Path) -> Path:
        """Route built-in prompts to the requested output engine without any fallback path."""
        prompt_path = Path(prompt_path)
        parts = prompt_path.parts
        if "generator" in parts:
            stem = prompt_path.stem
            if stem in {"question_generator", "paper_generator", "tutorial_request"}:
                return GeneratorPromptResolver(Path("prompts/generator")).resolve(
                    stem,
                    output_format=args.output_format,
                    pdf_engine=args.pdf_engine,
                )
        if "extractor" in parts:
            if args.output_format == "xml":
                return prompt_path
            extractor_prompt = Path("prompts/extractor") / args.pdf_engine / "extractor.md"
            if not extractor_prompt.exists():
                raise FileNotFoundError(f"Extractor prompt not found for PDF engine '{args.pdf_engine}'.")
            return extractor_prompt
        return prompt_path

    # Auto-resolve default prompt if not provided by user
    if args.prompt is None:
        if task == "extract":
            args.prompt = resolve_default_extractor_prompt(args.standards, args.output_format, args.pdf_engine)
        elif task == "generate-questions":
            if args.output_format == "pdf":
                args.prompt = Path(f"prompts/generator/{args.pdf_engine}/question_generator.md")
            else:
                args.prompt = Path("prompts/generator/xml/question_generator.md")
        elif task == "generate-tutorial":
            args.output_format = "pdf"
            args.prompt = Path(f"prompts/generator/{args.pdf_engine}/tutorial_request.md")
        elif task == "generate-paper":
            if args.output_format == "pdf":
                args.prompt = Path(f"prompts/generator/{args.pdf_engine}/paper_generator.md")
            else:
                args.prompt = Path("prompts/generator/xml/paper_generator.md")
    else:
        args.prompt = resolve_format_prompt(args.prompt)

    # Build rules dictionary
    rules_dict = {
        "main_prompt": args.prompt,
        "instruction_file": args.instruction_file,
        "xml_rules": args.xml_rules,
        "xml_extraction_rules": args.xml_extraction_rules,
        "tags_rules": args.tags_rules,
        "templates": args.templates,
        "pdf_rules": args.pdf_rules,
        "pdf_rules_html": args.pdf_rules_html,
        "pdf_rules_tex": args.pdf_rules_tex,
        "reasoning_rules": args.reasoning_rules,
    }

    languages_list = [l.strip() for l in args.languages.split(",") if l.strip()]
    args.output_dir = Path(args.output_dir).expanduser().resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Execute Functional Task
    start_time = time.perf_counter()
    try:
        if task == "translate-artifacts":
            for source_path in translation_files:
                relative_path = (
                    source_path.relative_to(translation_input_root)
                    if translation_input_root is not None
                    else Path(source_path.name)
                )
                output_parent = args.output_dir / relative_path.parent
                extension = source_path.suffix.lower()
                output_format = "xml" if extension == ".xml" else "pdf"
                pdf_engine = "html" if extension == ".html" else "tex"
                source_artifact = source_path.read_text(encoding="utf-8")

                for target_language in translate_to:
                    output_stem = build_translation_output_stem(
                        source_path.stem, target_language
                    )
                    output_path = output_parent / f"{output_stem}{extension}"
                    if (
                        output_path.is_file()
                        and output_path.stat().st_size > 0
                        and not args.force
                        and (
                            extension != ".xml"
                            or is_valid_moodle_xml_file(output_path)
                        )
                    ):
                        logger.info("Skipping existing translation: %s", output_path)
                        continue
                    if (
                        extension == ".xml"
                        and output_path.is_file()
                        and not is_valid_moodle_xml_file(output_path)
                    ):
                        logger.warning(
                            "Existing translation is not well-formed Moodle XML; "
                            "regenerating: %s",
                            output_path,
                        )

                    prompt_snapshot_path = (
                        args.output_dir
                        / ".tmp"
                        / "prompts"
                        / relative_path.parent
                        / f"{output_stem}_translation_prompt.md"
                    )
                    prompt_snapshot_path.parent.mkdir(parents=True, exist_ok=True)
                    translated_artifact = translate_assessment_artifact(
                        communicator,
                        source_artifact,
                        target_language,
                        output_format,
                        pdf_engine,
                        output_filename=output_path.name,
                        prompt_snapshot_path=prompt_snapshot_path,
                        force=args.force,
                        xml_chunk_size=args.xml_chunk_size,
                    )
                    output_parent.mkdir(parents=True, exist_ok=True)
                    output_path.write_text(translated_artifact, encoding="utf-8")
                    logger.info("Saved reviewed bilingual artifact: %s", output_path)

        elif task == "extract":
            page_check_client = getattr(communicator, "client", None) or genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
            page_check_communicator = SingleShotBackend(
                client=page_check_client,
                model_name=args.instruction_page_model_name or args.model_name,
                attempt_limit=args.retry_limit,
                verbose=args.verbose,
            )
            extractor = QuestionPaperExtractor(
                communicator=communicator,
                page_check_communicator=page_check_communicator,
                ocr_engine=ocr_engine,
                rules_dict=rules_dict,
                languages=languages_list,
                standards=args.standards,
                tags=args.tags,
                page_range=args.page_range,
                no_instruction_page=args.no_instruction_page,
                instruction_page=args.instruction_page,
                verify_online=args.verify_online,
                rate_limit_delay=args.rate_limit_delay,
                output_format=args.output_format,
                pdf_engine=args.pdf_engine,
                batch_size=getattr(args, "batch_size", 0),
                force_overwrite=getattr(args, "force", False),
                header_text=getattr(args, "header_text", ""),
                footer_text=getattr(args, "footer_text", ""),
                resume=getattr(args, "resume", False),
                replace_pages=getattr(args, "replace_pages", None),
            )
            if args.input_file:
                extractor.process_file(args.input_file, args.output_dir)
            elif args.input_dir:
                extractor.process_directory(args.input_dir, args.output_dir)
            else:
                logger.error("Please specify --input-dir or --input-file for extraction.")
                sys.exit(1)

        elif task == "generate-questions":
            generator = QuestionGenerator(
                communicator=communicator,
                ocr_engine=ocr_engine,
                rules_dict=rules_dict,
                languages=languages_list,
                standards=args.standards,
                tags=args.tags,
                difficulty_mix=args.difficulty_mix,
                num_questions=args.num_questions,
                num_papers=args.num_papers,
                output_format=args.output_format,
                pdf_engine=args.pdf_engine,
                page_range=args.page_range,
                exam_duration_minutes=getattr(args, "exam_duration_minutes", None),
                spec_path=getattr(args, "spec", None),
                mcq_types_path=args.mcq_types,
                watermark_text=getattr(args, "watermark_text", ""),
                header_text=getattr(args, "header_text", ""),
                footer_text=getattr(args, "footer_text", ""),
                translate_to=translate_to,
                force_overwrite=getattr(args, "force", False),
                separate_by_question_type=getattr(
                    args,
                    "separate_by_question_type",
                    False,
                ),
            )
            if args.input_file:
                generator.process_file(args.input_file, args.output_dir)
            elif args.input_dir:
                generator.process_directory(args.input_dir, args.output_dir)
            else:
                logger.error("Please specify --input-dir or --input-file for chapter generation.")
                sys.exit(1)

        elif task == "generate-tutorial":
            args.output_format = "pdf"
            tutorial_generator = TutorialGenerator(
                communicator=communicator,
                ocr_engine=ocr_engine,
                rules_dict=rules_dict,
                languages=languages_list,
                standards=args.standards,
                tags=args.tags,
                output_format="pdf",
                pdf_engine=args.pdf_engine,
                page_range=args.page_range,
                tutorial_title=getattr(args, "title", None),
                watermark_text=getattr(args, "watermark_text", ""),
                header_text=getattr(args, "header_text", ""),
                footer_text=getattr(args, "footer_text", ""),
            )
            if args.input_file:
                tutorial_generator.process_file(args.input_file, args.output_dir)
            elif args.input_dir:
                tutorial_generator.process_directory(args.input_dir, args.output_dir)
            else:
                logger.error("Please specify --input-dir or --input-file for tutorial generation.")
                sys.exit(1)

        elif task == "generate-paper":
            if not args.spec:
                logger.error("Please specify --spec (YAML spec or syllabus markdown/pdf).")
                sys.exit(1)

            mock_generator = PaperGenerator(
                communicator=communicator,
                ocr_engine=ocr_engine,
                rules_dict=rules_dict,
                languages=languages_list,
                standards=args.standards,
                tags=args.tags,
                difficulty_mix=args.difficulty_mix,
                output_format=args.output_format,
                pdf_engine=args.pdf_engine,
                sample_pdf=args.sample_pdf,
                exam_duration_minutes=getattr(args, "exam_duration_minutes", None),
                mcq_types_path=args.mcq_types,
                watermark_text=getattr(args, "watermark_text", ""),
                header_text=getattr(args, "header_text", ""),
                footer_text=getattr(args, "footer_text", ""),
            )
            if is_batch_paper_spec(args.spec, args.num_tests):
                PaperBatchRunner(mock_generator).run(
                    args.spec,
                    args.output_dir,
                    num_tests=args.num_tests,
                    resume=args.resume,
                    force=args.force,
                    max_attempts=args.max_paper_attempts,
                )
            else:
                mock_generator.process_spec(args.spec, args.output_dir)

        elapsed = time.perf_counter() - start_time
        logger.info("⏱️ Total operation time: %.2f seconds (%.2f minutes)", elapsed, elapsed / 60.0)
        if ocr_engine is not None:
            ocr_engine.log_cache_summary()
    except Exception:
        elapsed = time.perf_counter() - start_time
        logger.exception("Pipeline command failed after %.2f seconds (%.2f minutes).", elapsed, elapsed / 60.0)
        raise
    finally:
        if hasattr(communicator, "close"):
            communicator.close()


if __name__ == "__main__":
    main()
