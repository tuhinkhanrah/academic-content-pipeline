#!/usr/bin/env python3
"""Standalone OCR runner for mirrored PDF roots.

This file intentionally keeps the OCR-only logic separate from the existing
pipeline implementation. It does not modify any existing project files.

Example:
    PYTHONPATH=src python scripts/run_ocr_only_path_mirrored_extraction.py \
        --source-root /home/motorola/Workspace/question-papers-vault \
        --output-root /tmp/ocr-output \
        --mode batch
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from mistralai.client import Mistral

from academic_content_pipeline.mistral_ocr import MistralOCREngine, OCRPageData, OCRResult


def iter_pdfs(source_root: Path):
    for pdf_path in sorted(source_root.rglob("*.pdf")):
        if pdf_path.name.startswith("sliced_") or pdf_path.name.startswith("temp_"):
            continue
        yield pdf_path


def build_output_layout(source_root: Path, pdf_path: Path, output_root: Path):
    paper_dir = output_root / pdf_path.relative_to(source_root).parent
    work_dir = paper_dir / pdf_path.stem
    markdown_dir = work_dir / "markdown"
    img_dir = work_dir / "ocr"
    temp_dir = img_dir / "temp_sliced"
    final_xml = paper_dir / f"{pdf_path.stem}.xml"
    return paper_dir, work_dir, markdown_dir, img_dir, temp_dir, final_xml


def should_skip(output_path: Path, force: bool) -> bool:
    if force:
        return False
    return output_path.exists() and output_path.stat().st_size > 0


def write_xml_summary(pdf_path: Path, xml_path: Path, model_name: str, page_count: int) -> None:
    xml_path.write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<ocr_result>\n"
        f"  <source>{pdf_path.name}</source>\n"
        f"  <model>{model_name}</model>\n"
        f"  <pages>{page_count}</pages>\n"
        "</ocr_result>\n",
        encoding="utf-8",
    )


def write_cached_result_to_output(pdf_path: Path, source_root: Path, output_root: Path, result: Any, model_name: str) -> None:
    paper_dir, _, markdown_dir, img_dir, _, final_xml = build_output_layout(source_root, pdf_path, output_root)
    paper_dir.mkdir(parents=True, exist_ok=True)
    markdown_dir.mkdir(parents=True, exist_ok=True)
    img_dir.mkdir(parents=True, exist_ok=True)

    markdown_text = getattr(result, "full_markdown", "") if result is not None else ""
    (markdown_dir / f"{pdf_path.stem}.md").write_text(markdown_text, encoding="utf-8")
    write_xml_summary(pdf_path, final_xml, model_name, len(getattr(result, "pages", []) or []))


def run_single_pdf(
    pdf_path: Path,
    source_root: Path,
    output_root: Path,
    *,
    model: str,
    force: bool,
    disable_ocr_cache: bool,
    verbose: bool,
) -> int:
    paper_dir, _, markdown_dir, img_dir, temp_dir, final_xml = build_output_layout(
        source_root,
        pdf_path,
        output_root,
    )

    paper_dir.mkdir(parents=True, exist_ok=True)
    markdown_dir.mkdir(parents=True, exist_ok=True)
    img_dir.mkdir(parents=True, exist_ok=True)

    if should_skip(final_xml, force):
        if verbose:
            print(f"Skipping {pdf_path}: output already exists at {final_xml}")
        return 0

    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY environment variable must be set.")

    engine = MistralOCREngine(
        api_key=api_key,
        cache_dir=img_dir / "cache",
        enable_cache=not disable_ocr_cache,
    )
    engine.model_name = model

    start = time.perf_counter()
    result = engine.process_pdf(
        pdf_path=pdf_path,
        img_output_dir=img_dir,
        page_range=None,
        temp_dir=temp_dir,
    )

    markdown_path = markdown_dir / f"{pdf_path.stem}.md"
    full_markdown = getattr(result, "full_markdown", "") if result is not None else ""
    markdown_path.write_text(full_markdown, encoding="utf-8")

    page_count = len(getattr(result, "pages", []) or [])
    write_xml_summary(pdf_path, final_xml, engine.model_name, page_count)

    elapsed = time.perf_counter() - start
    if verbose:
        print(f"Processed {pdf_path} in {elapsed:.2f}s -> {final_xml}")
    return 0


def persist_batch_result(
    pdf_path: Path,
    source_root: Path,
    output_root: Path,
    payload: dict[str, Any],
    model_name: str,
    cache_engine: MistralOCREngine | None = None,
) -> None:
    _, _, markdown_dir, img_dir, _, final_xml = build_output_layout(source_root, pdf_path, output_root)
    markdown_dir.mkdir(parents=True, exist_ok=True)
    img_dir.mkdir(parents=True, exist_ok=True)

    response_body = payload.get("response", {}).get("body", {})
    pages = response_body.get("pages", [])
    markdown_chunks: list[str] = []
    all_images: dict[str, str] = {}
    page_records: list[OCRPageData] = []

    for page_index, page in enumerate(pages):
        page_markdown = page.get("markdown", "")
        markdown_chunks.append(page_markdown)
        page_images: dict[str, str] = {}

        for image_index, image in enumerate(page.get("images", [])):
            image_data = image.get("image_base64", "")
            if not image_data:
                continue
            if "," in image_data:
                image_data = image_data.split(",", 1)[1]
            image_bytes = base64.b64decode(image_data)
            image_id = image.get("id") or f"page_{page_index + 1}_img_{image_index}.jpeg"
            image_path = img_dir / image_id
            image_path.write_bytes(image_bytes)
            all_images[image_id] = str(image_path)
            page_images[image_id] = str(image_path)

        page_records.append(
            OCRPageData(
                page_num=page_index + 1,
                markdown=page_markdown,
                images=page_images,
            )
        )

    markdown_text = "\n\n---\n\n".join(markdown_chunks)
    (markdown_dir / f"{pdf_path.stem}.md").write_text(markdown_text, encoding="utf-8")
    write_xml_summary(pdf_path, final_xml, model_name, len(pages))

    if cache_engine is not None:
        cache_engine.save_cached_result(
            pdf_path,
            page_range=None,
            result=OCRResult(
                full_markdown=markdown_text,
                all_images=all_images,
                pages=page_records,
            ),
            cache_dir=img_dir / "cache",
        )


def run_mistral_batch(
    source_root: Path,
    output_root: Path,
    pdf_jobs: list[Path],
    *,
    model: str,
    force: bool,
    disable_ocr_cache: bool,
    verbose: bool,
) -> int:
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY environment variable must be set.")

    cache_engine = MistralOCREngine(
        api_key=api_key,
        enable_cache=not disable_ocr_cache,
    )
    cache_engine.model_name = model

    client = Mistral(api_key=api_key)
    pdf_lookup = {pdf_path.relative_to(source_root).as_posix(): pdf_path for pdf_path in pdf_jobs}
    tasks: list[dict[str, Any]] = []

    for pdf_path in pdf_jobs:
        _, _, _, img_dir, _, final_xml = build_output_layout(source_root, pdf_path, output_root)
        cache_dir = img_dir / "cache"
        cached_result = cache_engine.load_cached_result(pdf_path, page_range=None, cache_dir=cache_dir)
        if cached_result is not None:
            write_cached_result_to_output(pdf_path, source_root, output_root, cached_result, model)
            if verbose:
                print(f"Using cached OCR result for {pdf_path} -> {final_xml}")
            continue

        with open(pdf_path, "rb") as handle:
            pdf_bytes = handle.read()

        uploaded_file = client.files.upload(
            file={"file_name": pdf_path.name, "content": pdf_bytes},
            purpose="ocr",
        )
        signed_url = client.files.get_signed_url(file_id=uploaded_file.id, expiry=24).url

        tasks.append(
            {
                "custom_id": pdf_path.relative_to(source_root).as_posix(),
                "body": {
                    "model": model,
                    "document": {"type": "document_url", "document_url": signed_url},
                    "include_image_base64": True,
                },
            }
        )

    batch_dir = output_root / ".mistral_batch"
    batch_dir.mkdir(parents=True, exist_ok=True)
    batch_tasks_file = batch_dir / "batch_ocr_tasks.jsonl"
    with open(batch_tasks_file, "w", encoding="utf-8") as handle:
        for task in tasks:
            handle.write(json.dumps(task, ensure_ascii=False) + "\n")

    batch_input_file = client.files.upload(
        file={"file_name": batch_tasks_file.name, "content": batch_tasks_file.read_bytes()},
        purpose="batch",
    )
    batch_job = client.batch.jobs.create(
        input_files=[batch_input_file.id],
        model=model,
        endpoint="/v1/ocr",
    )

    if verbose:
        print(f"Started Mistral OCR batch job: {batch_job.id}")

    while True:
        status = client.batch.jobs.get(job_id=batch_job.id)
        if verbose:
            print(f"Status: {status.status} ({status.completed_requests}/{status.total_requests})")
        if status.status in {"SUCCESS", "FAILED", "TIMEOUT_EXCEEDED", "CANCELLED"}:
            break
        time.sleep(30)

    if status.status != "SUCCESS":
        raise RuntimeError(f"Mistral batch OCR failed with status: {status.status}")

    results_bytes = client.files.download(file_id=status.output_file)
    results_path = batch_dir / f"{batch_job.id}_results.jsonl"
    if isinstance(results_bytes, (bytes, bytearray)):
        results_path.write_bytes(results_bytes)
        lines = results_bytes.decode("utf-8", errors="strict").splitlines()
    else:
        results_path.write_text(str(results_bytes), encoding="utf-8")
        lines = str(results_bytes).splitlines()

    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue

        custom_id = str(payload.get("custom_id") or "")
        pdf_path = pdf_lookup.get(custom_id)
        if pdf_path is None:
            continue
        if should_skip(build_output_layout(source_root, pdf_path, output_root)[5], force):
            continue
        persist_batch_result(pdf_path, source_root, output_root, payload, model_name=model, cache_engine=cache_engine)

    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Mirror a PDF vault and run OCR while preserving the source tree layout under the output root.",
    )
    parser.add_argument("--source-root", type=Path, required=True, help="Directory containing the source PDFs.")
    parser.add_argument("--output-root", type=Path, required=True, help="Directory where mirrored OCR output will be written.")
    parser.add_argument("--mode", choices=["sequential", "batch"], default="sequential", help="Processing mode: sequential uses direct OCR, batch uses Mistral OCR batch jobs.")
    parser.add_argument("--ocr-model", "--model", dest="model", default=os.environ.get("MISTRAL_OCR_MODEL", "mistral-ocr-latest"), help="Mistral OCR model name to use.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing XML output file.")
    parser.add_argument("--disable-ocr-cache", action="store_true", help="Disable the local OCR cache for the run.")
    parser.add_argument("--dry-run", action="store_true", help="Print the planned work without executing OCR.")
    parser.add_argument("--verbose", action="store_true", help="Print progress logs while processing.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    source_root = args.source_root.resolve()
    output_root = args.output_root.resolve()

    if not source_root.exists():
        raise FileNotFoundError(f"Source root does not exist: {source_root}")

    output_root.mkdir(parents=True, exist_ok=True)

    pdf_jobs = [pdf for pdf in iter_pdfs(source_root)]
    pending_jobs = []

    for pdf_path in pdf_jobs:
        _, _, _, _, _, final_xml = build_output_layout(source_root, pdf_path, output_root)
        if should_skip(final_xml, args.force):
            continue
        pending_jobs.append(pdf_path)

    if args.dry_run:
        print(f"Would process {len(pending_jobs)} PDF(s) from {source_root} into {output_root}")
        for pdf_path in pending_jobs:
            _, _, markdown_dir, img_dir, _, final_xml = build_output_layout(source_root, pdf_path, output_root)
            print(f"  - {pdf_path} -> {final_xml}")
            print(f"      markdown: {markdown_dir}")
            print(f"      ocr: {img_dir}")
        return 0

    if not pending_jobs:
        print(f"No pending OCR work for {source_root}. Use --force to overwrite existing outputs.")
        return 0

    if args.mode == "batch":
        return run_mistral_batch(
            source_root,
            output_root,
            pending_jobs,
            model=args.model,
            force=args.force,
            disable_ocr_cache=args.disable_ocr_cache,
            verbose=args.verbose,
        )

    for pdf_path in pending_jobs:
        return_code = run_single_pdf(
            pdf_path,
            source_root,
            output_root,
            model=args.model,
            force=args.force,
            disable_ocr_cache=args.disable_ocr_cache,
            verbose=args.verbose,
        )
        if return_code != 0:
            return return_code

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
