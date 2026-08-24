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
  3. generate-paper         : Synthesize full mock exams from JSON blueprints / syllabi
"""

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from google import genai

try:
    from .ai_communicators import (
        AgentSessionBackend,
        BaseAICommunicator,
        ContextChatBackend,
        RemoteSandboxBackend,
    )
    from .content_processors import (
        PaperGenerator,
        QuestionGenerator,
        QuestionPaperExtractor,
    )
    from .mistral_ocr import MistralOCREngine
    from .pipeline_utils import load_and_merge_config, setup_logger
except ImportError:  # pragma: no cover - fallback for direct script execution
    from ai_communicators import (
        AgentSessionBackend,
        BaseAICommunicator,
        ContextChatBackend,
        RemoteSandboxBackend,
    )
    from content_processors import (
        PaperGenerator,
        QuestionGenerator,
        QuestionPaperExtractor,
    )
    from mistral_ocr import MistralOCREngine
    from pipeline_utils import load_and_merge_config, setup_logger

logger = logging.getLogger("academic_content_pipeline")


def add_common_options(parser: argparse.ArgumentParser) -> None:
    """Adds shared options across all subcommands."""
    parser.add_argument("--config", type=Path, default=None, help="Path to optional JSON configuration file.")
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
    parser.add_argument("--tags-rules", type=Path, default="prompts/core/naming_and_tags_rules.md")
    parser.add_argument("--templates", type=Path, default="prompts/core/moodle_xml_templates.md")
    parser.add_argument("--pdf-rules", type=Path, default=None)
    parser.add_argument("--pdf-rules-html", type=Path, default="prompts/core/pdf_html_rules.md")
    parser.add_argument("--pdf-rules-tex", type=Path, default="prompts/core/pdf_tex_rules.md")

    # Communicator specific tuning
    parser.add_argument("--model-name", default="gemini-3.5-flash", help="Gemini model name.")
    parser.add_argument("--agent-name", default="antigravity-preview-05-2026", help="Agent identifier.")
    parser.add_argument("--memory-span", type=int, default=3, help="Rolling turn history memory span for chat context.")
    parser.add_argument("--rate-limit-delay", type=float, default=6.0, help="Delay in seconds between page requests to stay under TPM limits.")
    parser.add_argument("--retry-limit", type=int, default=5, help="Max retry attempts per page on API/quota error.")
    parser.add_argument("--bucket-name", default=None, help="GCS bucket name for remote sandbox staging.")


def add_task_subparsers(subparser_dest: Any) -> None:
    """Attaches extract, generate-questions, and generate-paper subparsers."""

    # 1. extract
    p_ext = subparser_dest.add_parser("extract", help="Extract questions from exam paper PDFs via Mistral OCR.")
    add_common_options(p_ext)
    p_ext.add_argument("--input-dir", type=Path, default=None, help="Directory containing input PDFs.")
    p_ext.add_argument("--input-file", type=Path, default=None, help="Single input PDF file.")
    p_ext.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"), help="Page range to extract (1-based).")
    p_ext.add_argument("--no-instruction-page", action="store_true", help="Skip front instruction page.")
    p_ext.add_argument("--instruction-page", type=int, default=1, help="Index of front instruction page (1-based).")
    p_ext.add_argument("--verify-online", action="store_true", help="Verify answers online.")

    # 2. generate-questions
    p_chap = subparser_dest.add_parser("generate-questions", help="Synthesize practice questions from chapter PDFs/MDs.")
    add_common_options(p_chap)
    p_chap.add_argument("--input-dir", type=Path, default=None, help="Directory containing chapter PDFs or MDs.")
    p_chap.add_argument("--input-file", type=Path, default=None, help="Single chapter PDF or MD file.")
    p_chap.add_argument("--num-questions", type=int, default=5, help="Number of questions to synthesize.")
    p_chap.add_argument("--difficulty", choices=["easy", "medium", "hard"], default="medium", help="Question difficulty.")
    p_chap.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"), help="Page range for chapter PDF.")

    # 3. generate-paper
    p_syl = subparser_dest.add_parser("generate-paper", help="Synthesize mock exams from blueprints / syllabi.")
    add_common_options(p_syl)
    p_syl.add_argument("--blueprint", type=Path, default=None, help="Path to JSON blueprint or syllabus markdown/pdf.")
    p_syl.add_argument("--sample-pdf", type=Path, default=None, help="Optional sample exam PDF for pattern matching.")
    p_syl.add_argument("--difficulty-mix", default="easy:0.2,medium:0.5,hard:0.3", help="Difficulty ratio breakdown.")


def build_communicator(mode: str, args: argparse.Namespace) -> BaseAICommunicator:
    """Builds the AI communication backend matching the selected mode."""
    genai_client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

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
            print("❌ Error: GCS bucket name must be supplied via --bucket-name or GCS_BUCKET_NAME environment variable for remote mode.")
            sys.exit(1)
        return RemoteSandboxBackend(
            client=genai_client,
            bucket_name=bucket_name,
            agent_name=args.agent_name,
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

    # 2. Task-first direct syntax: `pipeline.py extract --mode ...`
    p_direct_ext = top_subparsers.add_parser("extract", help="Extract questions from PDF question papers.")
    p_direct_ext.add_argument("--mode", choices=["context", "agent", "remote"], default="context")
    add_common_options(p_direct_ext)
    p_direct_ext.add_argument("--input-dir", type=Path, default=None)
    p_direct_ext.add_argument("--input-file", type=Path, default=None)
    p_direct_ext.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"))
    p_direct_ext.add_argument("--no-instruction-page", action="store_true")
    p_direct_ext.add_argument("--instruction-page", type=int, default=1)
    p_direct_ext.add_argument("--verify-online", action="store_true")

    p_direct_chap = top_subparsers.add_parser("generate-questions", help="Synthesize questions from chapter documents.")
    p_direct_chap.add_argument("--mode", choices=["context", "agent", "remote"], default="context")
    add_common_options(p_direct_chap)
    p_direct_chap.add_argument("--input-dir", type=Path, default=None)
    p_direct_chap.add_argument("--input-file", type=Path, default=None)
    p_direct_chap.add_argument("--num-questions", type=int, default=5)
    p_direct_chap.add_argument("--difficulty", choices=["easy", "medium", "hard"], default="medium")
    p_direct_chap.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"))

    p_direct_syl = top_subparsers.add_parser("generate-paper", help="Synthesize mock exams.")
    p_direct_syl.add_argument("--mode", choices=["context", "agent", "remote"], default="context")
    add_common_options(p_direct_syl)
    p_direct_syl.add_argument("--blueprint", type=Path, default=None)
    p_direct_syl.add_argument("--sample-pdf", type=Path, default=None)
    p_direct_syl.add_argument("--difficulty-mix", default="easy:0.2,medium:0.5,hard:0.3")

    args = parser.parse_args()

    # Determine mode and task based on syntax used
    if args.top_command in ["context", "agent", "remote"]:
        mode = args.top_command
        task = args.task
    else:
        mode = getattr(args, "mode", "context")
        task = args.top_command

    # Merge config file if provided
    config_dict = load_and_merge_config(vars(args), getattr(args, "config", None))
    for k, v in config_dict.items():
        if getattr(args, k, None) is None or getattr(args, k, None) == parser.get_default(k):
            setattr(args, k, v)

    # Setup Logging
    setup_logger(args.log_file, verbose=args.verbose)

    runtime_config = {k: v for k, v in vars(args).items() if not callable(v)}
    logger.info("CLI runtime configuration: %s", runtime_config)

    # Verify environment keys
    if not os.environ.get("GEMINI_API_KEY"):
        print("❌ Error: GEMINI_API_KEY environment variable must be set.")
        sys.exit(1)
    if not os.environ.get("MISTRAL_API_KEY"):
        print("❌ Error: MISTRAL_API_KEY environment variable must be set.")
        sys.exit(1)

    # Initialize communication backend & OCR engine
    communicator = build_communicator(mode, args)
    ocr_engine = MistralOCREngine()

    # Auto-infer PDF engine and prompt defaults if not explicitly set
    if getattr(args, "output_format", "xml") == "pdf":
        # If user explicitly supplied tex rules or tex prompt, auto-set pdf_engine to tex
        if (args.pdf_rules_tex and "pdf_tex_rules.md" in str(args.pdf_rules_tex) and "--pdf-rules-tex" in sys.argv) or (args.prompt and "tex" in str(args.prompt)):
            if "--pdf-engine" not in sys.argv:
                args.pdf_engine = "tex"

    # Auto-resolve default prompt if not provided by user
    if args.prompt is None:
        if task == "extract":
            args.prompt = Path("prompts/extractor/neet.md")
        elif task == "generate-questions":
            if args.output_format == "pdf":
                args.prompt = Path(f"prompts/generator/{args.pdf_engine}/question_generator.md")
            else:
                args.prompt = Path("prompts/generator/xml/question_generator.md")
        elif task == "generate-paper":
            if args.output_format == "pdf":
                args.prompt = Path(f"prompts/generator/{args.pdf_engine}/paper_generator.md")
            else:
                args.prompt = Path("prompts/generator/xml/paper_generator.md")
    else:
        # If user provided generic prompt path (e.g. prompts/generator/question_generator.md), route to format-specific one
        p_str = str(args.prompt)
        if p_str in ["prompts/generator/question_generator.md", "prompts/generator/paper_generator.md"]:
            stem = Path(p_str).stem
            if args.output_format == "pdf":
                resolved_path = Path(f"prompts/generator/{args.pdf_engine}/{stem}.md")
            else:
                resolved_path = Path(f"prompts/generator/xml/{stem}.md")
            if resolved_path.exists():
                args.prompt = resolved_path

    # Build rules dictionary
    rules_dict = {
        "main_prompt": args.prompt,
        "instruction_file": args.instruction_file,
        "xml_rules": args.xml_rules,
        "tags_rules": args.tags_rules,
        "templates": args.templates,
        "pdf_rules": args.pdf_rules,
        "pdf_rules_html": args.pdf_rules_html,
        "pdf_rules_tex": args.pdf_rules_tex,
    }

    languages_list = [l.strip() for l in args.languages.split(",") if l.strip()]
    args.output_dir = Path(args.output_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Execute Functional Task
    try:
        if task == "extract":
            extractor = QuestionPaperExtractor(
                communicator=communicator,
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
            )
            if args.input_file:
                extractor.process_file(args.input_file, args.output_dir)
            elif args.input_dir:
                extractor.process_directory(args.input_dir, args.output_dir)
            else:
                print("❌ Error: Please specify --input-dir or --input-file for extraction.")
                sys.exit(1)

        elif task == "generate-questions":
            generator = QuestionGenerator(
                communicator=communicator,
                ocr_engine=ocr_engine,
                rules_dict=rules_dict,
                languages=languages_list,
                standards=args.standards,
                tags=args.tags,
                difficulty=args.difficulty,
                num_questions=args.num_questions,
                output_format=args.output_format,
                pdf_engine=args.pdf_engine,
                page_range=args.page_range,
            )
            if args.input_file:
                generator.process_file(args.input_file, args.output_dir)
            elif args.input_dir:
                generator.process_directory(args.input_dir, args.output_dir)
            else:
                print("❌ Error: Please specify --input-dir or --input-file for chapter generation.")
                sys.exit(1)

        elif task == "generate-paper":
            if not args.blueprint:
                print("❌ Error: Please specify --blueprint (JSON blueprint or syllabus markdown/pdf).")
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
            )
            mock_generator.process_blueprint(args.blueprint, args.output_dir)
    finally:
        if hasattr(communicator, "close"):
            communicator.close()


if __name__ == "__main__":
    main()
