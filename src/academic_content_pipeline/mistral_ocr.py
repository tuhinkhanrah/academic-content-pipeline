#!/usr/bin/env python3
"""
mistral_ocr.py - Mistral OCR & Image Processing Engine for PDF Documents.

Extracts Markdown text and isolated diagram images from PDF via Mistral OCR
with SHA-256 deduplication, page slicing, and Pillow enhancement.
"""

import os
import gc
import base64
import hashlib
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from PIL import Image, ImageEnhance
import pymupdf as fitz
from mistralai.client import Mistral

Image.MAX_IMAGE_PIXELS = None
logger = logging.getLogger("academic_content_pipeline")


class OCRPageData:
    """Represents OCR result for a single page."""
    def __init__(self, page_num: int, markdown: str, images: Dict[str, str]):
        self.page_num = page_num
        self.markdown = markdown
        self.images = images


class OCRResult:
    """Encapsulates full document OCR and per-page breakdown."""
    def __init__(self, full_markdown: str, all_images: Dict[str, str], pages: List[OCRPageData]):
        self.full_markdown = full_markdown
        self.all_images = all_images
        self.images = all_images  # alias
        self.pages = pages

    def __iter__(self):
        # Enables backward-compatible tuple unpacking: full_markdown, image_map = result
        return iter((self.full_markdown, self.all_images))


class MistralOCREngine:
    """Encapsulates Mistral OCR processing, PDF slicing, and diagram extraction."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("MISTRAL_API_KEY")
        if not self.api_key:
            raise ValueError("MISTRAL_API_KEY environment variable or argument must be set.")

        self.http_client = httpx.Client(verify=False, timeout=300.0)
        self.client = Mistral(api_key=self.api_key, client=self.http_client)

    @staticmethod
    def get_file_hash(filepath: Path) -> str:
        """Calculates SHA-256 hash to prevent redundant PDF uploads to Mistral."""
        sha256 = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    @staticmethod
    def slice_pdf_pages(
        pdf_path: Path,
        page_range: Optional[List[int]],
        temp_dir: Path = Path("output/ocr/temp_sliced"),
    ) -> Tuple[Path, bool]:
        """Extracts the specified page range [start, end] (1-based) from a PDF into a temporary file."""
        if not page_range or len(page_range) < 2:
            return pdf_path, False

        doc = fitz.open(pdf_path)
        total_pages = len(doc)
        start_page = max(0, page_range[0] - 1)
        end_page = min(total_pages - 1, page_range[1] - 1)

        if start_page > end_page:
            doc.close()
            raise ValueError(f"Invalid page range: {page_range} for PDF with {total_pages} pages.")

        logger.info(f"✂️ Slicing PDF pages {start_page+1} to {end_page+1} (total: {end_page - start_page + 1} pages)...")
        sliced_doc = fitz.open()
        sliced_doc.insert_pdf(doc, from_page=start_page, to_page=end_page)

        temp_dir.mkdir(parents=True, exist_ok=True)
        sliced_path = temp_dir / f"sliced_{start_page+1}_to_{end_page+1}_{pdf_path.name}"

        sliced_doc.save(str(sliced_path))
        sliced_doc.close()
        doc.close()
        return sliced_path, True

    @staticmethod
    def enhance_extracted_images(
        output_dir: Path, scale_factor: float = 1.2, max_dim: int = 2500
    ) -> None:
        """Enhances contrast and resolution of extracted diagram images."""
        logger.info(f"Enhancing extracted images in {output_dir} ({scale_factor}x scale, cap: {max_dim}px)...")
        if not os.path.exists(output_dir):
            return

        for filename in os.listdir(output_dir):
            if filename.lower().endswith((".jpg", ".jpeg", ".png")):
                filepath = os.path.join(output_dir, filename)
                try:
                    with Image.open(filepath) as img:
                        w, h = img.size
                        target_w, target_h = int(w * scale_factor), int(h * scale_factor)

                        if max(target_w, target_h) > max_dim:
                            cap_scale = max_dim / float(max(w, h))
                            new_size = (int(w * cap_scale), int(h * cap_scale))
                        else:
                            new_size = (target_w, target_h)

                        img_scaled = img.resize(new_size, Image.Resampling.LANCZOS)
                        img_final = ImageEnhance.Contrast(img_scaled).enhance(1.4)
                        img_final.save(filepath, quality=95)

                    del img_scaled, img_final
                    gc.collect()
                except Exception as e:
                    logger.warning(f"Could not enhance image {filepath}: {e}")

    def process_pdf(
        self,
        pdf_path: Path,
        img_output_dir: Path,
        page_range: Optional[List[int]] = None,
        enhance: bool = True,
        temp_dir: Optional[Path] = None,
    ) -> Tuple[str, Dict[str, str]]:
        """
        Converts a PDF (or sliced range) to Markdown and extracts isolated diagram images.

        Returns:
            Tuple[str, Dict[str, str]]: (full_markdown_text, image_map {image_filename: local_filepath})
        """
        img_output_dir.mkdir(parents=True, exist_ok=True)

        slice_dir = Path(temp_dir) if temp_dir is not None else Path("output/ocr/temp_sliced")
        sliced_pdf, is_temp = self.slice_pdf_pages(pdf_path, page_range, temp_dir=slice_dir)

        try:
            file_hash = self.get_file_hash(sliced_pdf)
            remote_filename = f"{file_hash}_{sliced_pdf.name}"

            my_files = self.client.files.list()
            uploaded_file_id = next((f.id for f in my_files.data if f.filename == remote_filename), None)

            if not uploaded_file_id:
                logger.info(f"Uploading '{sliced_pdf.name}' to Mistral OCR...")
                with open(sliced_pdf, "rb") as f:
                    uploaded_file = self.client.files.upload(
                        file={"file_name": remote_filename, "content": f.read()}, purpose="ocr"
                    )
                uploaded_file_id = uploaded_file.id
            else:
                logger.info("-> File hash match found on Mistral server. Skipping upload!")

            signed_url = self.client.files.get_signed_url(file_id=uploaded_file_id, expiry=1)

            logger.info("Executing Mistral OCR processing...")
            ocr_response = self.client.ocr.process(
                model="mistral-ocr-latest",
                document={"type": "document_url", "document_url": signed_url.url},
                include_image_base64=True,
            )

            full_markdown = ""
            extracted_images: Dict[str, str] = {}
            pages_data: List[OCRPageData] = []
            start_page_offset = page_range[0] if (page_range and len(page_range) >= 1) else 1

            image_counter = 0
            for page_idx, page in enumerate(ocr_response.pages):
                current_page_num = start_page_offset + page_idx
                page_markdown = page.markdown or ""
                full_markdown += page_markdown + "\n\n"
                page_images: Dict[str, str] = {}

                for img in (page.images or []):
                    raw_b64 = img.image_base64.split("base64,")[-1] if "base64," in img.image_base64 else img.image_base64
                    img_data = base64.b64decode(raw_b64)

                    clean_id = img.id.replace(".jpeg", "").replace(".jpg", "").replace(".png", "")
                    file_name = f"{clean_id}.jpeg"
                    file_path = str(img_output_dir / file_name)
                    with open(file_path, "wb") as f:
                        f.write(img_data)

                    extracted_images[file_name] = file_path
                    page_images[file_name] = file_path
                    # Also register alias without extension
                    extracted_images[clean_id] = file_path
                    page_images[clean_id] = file_path
                    image_counter += 1

                pages_data.append(OCRPageData(
                    page_num=current_page_num,
                    markdown=page_markdown,
                    images=page_images
                ))

            if extracted_images and enhance:
                self.enhance_extracted_images(img_output_dir)

            logger.info(f"Mistral OCR extracted {image_counter} image(s) across {len(pages_data)} page(s).")
            return OCRResult(full_markdown.strip(), extracted_images, pages_data)

        finally:
            if is_temp and sliced_pdf.exists():
                try:
                    os.remove(sliced_pdf)
                except Exception:
                    pass
