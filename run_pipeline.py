#!/usr/bin/env python3
"""
run_pipeline.py - Master Dynamic Orchestrator Pipeline.

Routing Logic:
  - Top-level folders containing 'books', 'chapters', 'chapter', or 'modules' -> scripts/chapter2moodle_agent.py
  - All other folders (question papers, PYQs, exams) -> scripts/pdf2moodle_agent.py
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

# Locate script paths dynamically relative to run_pipeline.py
BASE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = BASE_DIR / "scripts"
PDF_AGENT_SCRIPT = SCRIPTS_DIR / "pdf2moodle_agent.py"
CHAPTER_AGENT_SCRIPT = SCRIPTS_DIR / "chapter2moodle_agent.py"

PROMPT_MAP = {
    "jee_advanced": {
        "prompt": BASE_DIR / "prompts" / "prompt_jee_advanced.md",
        "standards": "JEE-Advanced",
    },
    "wbjee": {
        "prompt": BASE_DIR / "prompts" / "prompt_wbjee.md",
        "standards": "WBJEE",
    },
    "jee_main": {
        "prompt": BASE_DIR / "prompts" / "prompt_jee_main.md",
        "standards": "JEE-Main",
    },
    "neet": {
        "prompt": BASE_DIR / "prompts" / "prompt_neet.md",
        "standards": "NEET",
    },
}


def extract_path_tags(paper_dir: Path) -> str:
    tags = []
    year_match = re.search(r"\b(20\d{2})\b", str(paper_dir))
    if year_match:
        tags.append(f"year:{year_match.group(1)}")

    date_match = re.search(r"\b(\d{2}-\d{2}-20\d{2})\b", str(paper_dir))
    if date_match:
        tags.append(f"date:{date_match.group(1)}")

    set_match = re.search(r"SET\s*(\d+)", str(paper_dir), re.IGNORECASE)
    if set_match:
        tags.append(f"set:{set_match.group(1)}")

    shift_match = re.search(r"Shift_(\d+)", str(paper_dir), re.IGNORECASE)
    if shift_match:
        tags.append(f"shift:{shift_match.group(1)}")

    return ",".join(tags)


def detect_exam_type(paper_dir: Path) -> str:
    path_str_lower = str(paper_dir).lower()
    if "jee" in path_str_lower and "advanced" in path_str_lower:
        return "jee_advanced"
    elif "wbjee" in path_str_lower:
        return "wbjee"
    elif "jee" in path_str_lower or "jee_main" in path_str_lower:
        return "jee_main"
    else:
        return "neet"


def normalize_languages(languages_arg: str) -> List[str]:
    langs = [l.strip().lower() for l in languages_arg.split(",") if l.strip()]
    return langs if langs else ["english"]


def resolve_prompt_file(exam_key: str, args: argparse.Namespace) -> Path:
    if args.prompt:
        return args.prompt

    exam_info = PROMPT_MAP[exam_key]
    candidates = [
        exam_info.get("prompt"),
    ]

    # Last-resort defaults preserve existing behavior for missing files.
    if exam_key == "neet":
        candidates.extend([
            BASE_DIR / "prompts" / "prompt_neet.md",
            BASE_DIR / "prompts" / "prompt_jee_main.md",
        ])
    else:
        candidates.extend([
            BASE_DIR / "prompts" / "prompt_jee_main.md",
            BASE_DIR / "prompts" / "prompt_neet.md",
        ])

    for candidate in candidates:
        if candidate and candidate.exists():
            return candidate

    # Return mapped prompt even if missing so downstream logs clearly reveal the problem.
    return exam_info["prompt"]


def create_pdf_config_payload(
    paper_dir: Path, prompt_file: Path, standard: str, args: argparse.Namespace
) -> Dict:
    path_tags = extract_path_tags(paper_dir)
    merged_tags = f"{args.tags},{path_tags}".strip(",") if args.tags else path_tags

    inst_page = 0 if args.no_instruction_page else (args.instruction_page if args.instruction_page is not None else 1)
    languages = normalize_languages(args.languages) if args.languages else ["english"]

    config = {
        "input_dir": str(paper_dir.resolve()),
        "output_dir": str(paper_dir.resolve()),
        "prompt": str(args.prompt.resolve()) if args.prompt else str(prompt_file.resolve()),
        "standards": args.standards or standard,
        "languages": languages,
        "tags": merged_tags,
        "instruction_page": inst_page,
        "agent_name": args.agent_name or "antigravity-preview-05-2026",
        "agent_config": {
            "type": args.agent_type or "antigravity",
            "model": args.model_name or "gemini-3.6-flash",
        },
        "padding_cm": args.padding_cm if args.padding_cm is not None else 0.5,
        "rate_limit_delay": args.rate_limit_delay if args.rate_limit_delay is not None else 45.0,
        "retry_base_delay": args.retry_base_delay if args.retry_base_delay is not None else 4.0,
        "attempt_limit": args.attempt_limit if args.attempt_limit is not None else 10,
        "context_reset_interval": args.context_reset_interval if args.context_reset_interval is not None else 7,
        "dpi": args.dpi if args.dpi is not None else 150,
        "verbose": args.verbose,
    }

    if args.instruction_file:
        config["instruction_file"] = str(args.instruction_file.resolve())
    if args.page_range:
        config["page_range"] = list(args.page_range)

    return config


def create_chapter_config_payload(
    paper_dir: Path, prompt_file: Path, standard: str, args: argparse.Namespace
) -> Dict:
    path_tags = extract_path_tags(paper_dir)
    merged_tags = f"{args.tags},{path_tags}".strip(",") if args.tags else path_tags
    languages = normalize_languages(args.languages) if args.languages else ["english"]

    config = {
        "input_dir": str(paper_dir.resolve()),
        "output_dir": str(paper_dir.resolve()),
        "prompt": str(args.prompt.resolve()) if args.prompt else str(prompt_file.resolve()),
        "standards": args.standards or standard,
        "languages": languages,
        "difficulty": args.difficulty or "Medium",
        "num_questions": args.num_questions if args.num_questions is not None else 2,
        "default_grade": args.default_grade if args.default_grade is not None else 4.0,
        "penalty": args.penalty if args.penalty is not None else 0.25,
        "negative_fraction": args.negative_fraction if args.negative_fraction is not None else -25,
        "tags": merged_tags,
        "agent_name": args.agent_name or "antigravity-preview-05-2026",
        "agent_config": {
            "type": args.agent_type or "antigravity",
            "model": args.model_name or "gemini-3.6-flash",
        },
        "padding_cm": args.padding_cm if args.padding_cm is not None else 0.5,
        "rate_limit_delay": args.rate_limit_delay if args.rate_limit_delay is not None else 45.0,
        "retry_base_delay": args.retry_base_delay if args.retry_base_delay is not None else 4.0,
        "attempt_limit": args.attempt_limit if args.attempt_limit is not None else 10,
        "context_reset_interval": args.context_reset_interval if args.context_reset_interval is not None else 7,
        "dpi": args.dpi if args.dpi is not None else 150,
        "verbose": args.verbose,
    }

    if args.page_range:
        config["page_range"] = list(args.page_range)

    return config


def generate_pdf_configs(paper_dir: Path, args: argparse.Namespace) -> List[Path]:
    exam_key = detect_exam_type(paper_dir)
    exam_info = PROMPT_MAP[exam_key]
    standard = exam_info["standards"]
    prompt_file = resolve_prompt_file(exam_key, args)
    config_payload = create_pdf_config_payload(paper_dir, prompt_file, standard, args)
    config_path = paper_dir / "generated_config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_payload, f, indent=2)
    return [config_path]


def run_batch_processing(args: argparse.Namespace):
    papers_root = args.papers_dir
    if not papers_root.exists():
        print(f"❌ Root directory '{papers_root}' does not exist.")
        sys.exit(1)

    pdf_directories = sorted(list({pdf.parent for pdf in papers_root.glob("**/*.pdf")}))
    if not pdf_directories:
        print(f"⚠️ No PDF files found inside '{papers_root}'.")
        return

    print(f"📁 Found {len(pdf_directories)} PDF folder(s) under '{papers_root}'.\n")

    for folder in pdf_directories:
        folder_path_lower = str(folder).lower()

        # ROUTING RULE: Top-level folder containing 'books', 'chapters', 'chapter', or 'modules' -> chapter2moodle_agent.py
        if any(keyword in folder_path_lower for keyword in ["books", "chapters", "chapter", "modules"]):
            chapter_prompt = BASE_DIR / "prompts" / "prompt_chapter_generation.md"
            exam_key = detect_exam_type(folder)
            standard = PROMPT_MAP[exam_key]["standards"]

            config_payload = create_chapter_config_payload(folder, chapter_prompt, standard, args)
            config_path = folder / "generated_chapter_config.json"
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(config_payload, f, indent=2)

            cmd = [sys.executable, str(CHAPTER_AGENT_SCRIPT), "-c", str(config_path)]
            if args.verbose:
                cmd.append("-v")
            print(f"📖 Branch [BOOKS/CHAPTERS]: Processing {folder}")
            try:
                subprocess.run(cmd, check=True)
                print(f"✅ Successfully processed chapter bank in: {folder}\n")
            except subprocess.CalledProcessError as e:
                print(f"❌ Question generation failed in {folder} (Exit status {e.returncode})\n")

        # ROUTING RULE: Default (papers, pyq, exams) -> pdf2moodle_agent.py
        else:
            config_paths = generate_pdf_configs(folder, args)
            for config_path in config_paths:
                cmd = [sys.executable, str(PDF_AGENT_SCRIPT), "-c", str(config_path)]
                if args.verbose:
                    cmd.append("-v")
                print(f"🚀 Branch [PAPERS]: Extraction using [{config_path.name}] in: {folder}")
                try:
                    subprocess.run(cmd, check=True)
                    print(f"✅ Successfully extracted paper [{config_path.name}] in: {folder}\n")
                except subprocess.CalledProcessError as e:
                    print(f"❌ Extraction failed in {folder} for [{config_path.name}] (Exit status {e.returncode})\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="run_pipeline.py - Master Question Extraction & Generation Pipeline Runner",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # Core Pipeline Inputs
    parser.add_argument("-i", "--papers-dir", type=Path, default=Path("papers"), help="Root directory containing question paper PDFs or Book PDFs.")
    parser.add_argument("-p", "--prompt", type=Path, help="Override system prompt markdown file for all folders.")
    parser.add_argument("-l", "--languages", type=str, default="english", help="Comma-separated target languages (e.g. 'english', 'english,bengali', 'english,tamil', 'english,hindi').")
    parser.add_argument("-s", "--standards", type=str, help="Override standards (e.g. NEET, WBJEE, JEE-Main, JEE-Advanced).")
    parser.add_argument("-t", "--tags", type=str, help="Comma-separated global tags to append.")

    # Agent & Model Configuration Options
    parser.add_argument("-a", "--agent-name", type=str, help="Managed agent resource name.")
    parser.add_argument("--agent-type", type=str, help="Agent configuration type (default: antigravity).")
    parser.add_argument("-m", "--model-name", type=str, help="LLM model name (default: gemini-3.6-flash).")

    # Timing & Performance Options
    parser.add_argument("--rate-limit-delay", type=float, help="Inter-turn delay in seconds (default: 45.0).")
    parser.add_argument("--retry-base-delay", type=float, help="Base delay for API retries (default: 4.0).")
    parser.add_argument("--attempt-limit", type=int, help="Max retry attempts per page/file (default: 10).")
    parser.add_argument("--context-reset-interval", type=int, help="Turn history reset interval (default: 7).")

    # PDF Paper Agent Specific Options
    parser.add_argument("--instruction-file", type=Path, help="Standalone instruction/chapter markdown file.")
    parser.add_argument("--instruction-page", type=int, help="PDF page containing instructions (default: 1). Set to 0 if no instruction page exists.")
    parser.add_argument("--no-instruction-page", action="store_true", help="Explicitly specify that this paper does not have an instruction cover page.")
    parser.add_argument("--page-range", type=int, nargs=2, metavar=("START", "END"), help="Process specific PDF page range.")
    parser.add_argument("--padding-cm", type=float, help="Diagram crop padding in cm (default: 0.5).")
    parser.add_argument("--dpi", type=int, help="DPI rendering for PDF vision processing (default: 150).")

    # Chapter Generator Specific Options
    parser.add_argument("--difficulty", type=str, choices=["Easy", "Medium", "Hard"], help="Target difficulty level for chapter generation.")
    parser.add_argument("-n", "--num-questions", type=int, help="Maximum questions allowed per section/file; model decides actual count dynamically.")
    parser.add_argument("--default-grade", type=float, help="Default grade per question (default: 4.0).")
    parser.add_argument("--penalty", type=float, help="Penalty fraction for multiple attempts (default: 0.25).")
    parser.add_argument("--negative-fraction", type=int, help="Negative score percentage for incorrect choices (default: -25).")

    # Execution & Logging Options
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging.")

    return parser.parse_args()


if __name__ == "__main__":
    cli_args = parse_args()
    run_batch_processing(cli_args)