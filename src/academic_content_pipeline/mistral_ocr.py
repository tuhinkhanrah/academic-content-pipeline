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
import json
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

    def __init__(self, api_key: Optional[str] = None, cache_dir: Optional[Path] = None, enable_cache: bool = True):
        self.api_key = api_key or os.environ.get("MISTRAL_API_KEY")
        if not self.api_key:
            raise ValueError("MISTRAL_API_KEY environment variable or argument must be set.")

        self.model_name = "mistral-ocr-latest"
        self.cache_version = "ocr-v1"
        self.cache_dir = Path(cache_dir) if cache_dir is not None else Path("output/ocr/cache")
        self.enable_cache = bool(enable_cache)
        self.cache_stats = {"hit": 0, "miss": 0, "skipped": 0}
        self.http_client = httpx.Client(verify=False, timeout=300.0)
        self.client = Mistral(api_key=self.api_key, client=self.http_client)

    def _ensure_cache_defaults(self) -> None:
        if not hasattr(self, "model_name"):
            self.model_name = "mistral-ocr-latest"
        if not hasattr(self, "cache_version"):
            self.cache_version = "ocr-v1"
        if not hasattr(self, "cache_dir"):
            self.cache_dir = Path("output/ocr/cache")
        if not hasattr(self, "enable_cache"):
            self.enable_cache = True
        if not hasattr(self, "cache_stats"):
            self.cache_stats = {"hit": 0, "miss": 0, "skipped": 0}

    def _log_cache_status(self, pdf_path: Path, page_range: Optional[List[int]], *, hit: bool) -> None:
        """Emit a compact local OCR cache status message for a source PDF."""
        if page_range:
            start, end = page_range[:2]
            range_label = f"[{start}-{end}]"
        else:
            range_label = "[full]"

        if hit:
            logger.info("📦 local OCR cache hit for %s %s.", pdf_path.name, range_label)
        else:
            logger.info("📦 local OCR cache miss for %s %s.", pdf_path.name, range_label)

    def log_cache_summary(self) -> None:
        """Log the aggregate result of local OCR cache lookups for the current process."""
        self._ensure_cache_defaults()
        if not self.enable_cache:
            logger.info("📦 OCR cache is disabled. Skipping local cache hits and writes.")
            return

        total = self.cache_stats.get("hit", 0) + self.cache_stats.get("miss", 0)
        logger.info(
            "📊 Total local OCR cache summary for this run: %s hit(s), %s miss(es), %s skipped item(s).",
            self.cache_stats.get("hit", 0),
            self.cache_stats.get("miss", 0),
            self.cache_stats.get("skipped", 0),
        )
        if total == 0:
            logger.info("📦 OCR cache did not evaluate any entries in this run.")

    @staticmethod
    def get_file_hash(filepath: Path) -> str:
        """Calculates SHA-256 hash to prevent redundant PDF uploads to Mistral."""
        sha256 = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                sha256.update(chunk)
        return sha256.hexdigest()

    @staticmethod
    def normalize_page_range(page_range: Optional[List[int]]) -> Optional[Tuple[int, int]]:
        """Normalize a page range to deterministic start/end values."""
        if not page_range or len(page_range) < 2:
            return None
        start = int(page_range[0])
        end = int(page_range[1])
        if start > end:
            start, end = end, start
        return start, end

    def build_cache_key(self, pdf_path: Path, page_range: Optional[List[int]] = None) -> str:
        """Create a stable local cache key from the original PDF content and the requested page range."""
        self._ensure_cache_defaults()
        file_hash = self.get_file_hash(pdf_path)
        normalized_range = self.normalize_page_range(page_range)
        if normalized_range is None:
            range_token = "full"
        else:
            range_token = f"{normalized_range[0]}-{normalized_range[1]}"
        return f"{file_hash}:{range_token}:{self.model_name}:{self.cache_version}"

    def _cache_path_for_key(self, cache_key: str, cache_dir: Path) -> Path:
        cache_dir.mkdir(parents=True, exist_ok=True)
        key_hash = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
        return cache_dir / f"{key_hash}.json"

    def _build_ocr_process_kwargs(
        self,
        *,
        model: str,
        document: Dict[str, str],
        include_image_base64: bool,
    ) -> Dict[str, object]:
        """Build OCR kwargs for the installed Mistral SDK."""
        return {
            "model": model,
            "document": document,
            "include_image_base64": include_image_base64,
        }

    def load_cached_result(self, pdf_path: Path, page_range: Optional[List[int]] = None, cache_dir: Optional[Path] = None) -> Optional[OCRResult]:
        """Return a cached OCR result if one exists for the source PDF and page range."""
        target_cache_dir = Path(cache_dir) if cache_dir is not None else self.cache_dir
        cache_key = self.build_cache_key(pdf_path, page_range=page_range)
        cache_path = self._cache_path_for_key(cache_key, target_cache_dir)
        if not cache_path.exists():
            return None

        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            pages_payload = payload.get("pages", [])
            pages = [
                OCRPageData(
                    page_num=int(page.get("page_num", 0)),
                    markdown=str(page.get("markdown", "")),
                    images={str(k): str(v) for k, v in (page.get("images") or {}).items()},
                )
                for page in pages_payload
            ]
            return OCRResult(
                full_markdown=str(payload.get("full_markdown", "")),
                all_images={str(k): str(v) for k, v in (payload.get("images") or {}).items()},
                pages=pages,
            )
        except Exception as exc:
            logger.warning("OCR cache entry is unreadable at %s: %s", cache_path, exc)
            return None

    def save_cached_result(
        self,
        pdf_path: Path,
        page_range: Optional[List[int]],
        result: OCRResult,
        cache_dir: Optional[Path] = None,
    ) -> None:
        """Persist OCR results to a stable local cache keyed by the original PDF and page range."""
        target_cache_dir = Path(cache_dir) if cache_dir is not None else self.cache_dir
        cache_key = self.build_cache_key(pdf_path, page_range=page_range)
        cache_path = self._cache_path_for_key(cache_key, target_cache_dir)
        payload = {
            "cache_key": cache_key,
            "pdf_hash": self.get_file_hash(pdf_path),
            "page_range": list(self.normalize_page_range(page_range) or (0, 0)),
            "model_name": self.model_name,
            "cache_version": self.cache_version,
            "full_markdown": result.full_markdown,
            "images": {str(k): str(v) for k, v in result.all_images.items()},
            "pages": [
                {
                    "page_num": page.page_num,
                    "markdown": page.markdown,
                    "images": {str(k): str(v) for k, v in page.images.items()},
                }
                for page in result.pages
            ],
        }
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

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
    ) -> OCRResult:
        """
        Converts a PDF (or sliced range) to Markdown and extracts isolated diagram images.

        Returns:
            OCRResult with the full markdown, image map, and per-page detail.
        """
        self._ensure_cache_defaults()
        img_output_dir = Path(img_output_dir)
        img_output_dir.mkdir(parents=True, exist_ok=True)

        cache_dir = img_output_dir / "cache"

        if not self.enable_cache:
            logger.info("📦 OCR cache disabled for %s [%s]. Running live OCR.", pdf_path.name, page_range or "full")
            self.cache_stats["skipped"] = self.cache_stats.get("skipped", 0) + 1
        else:
            cached_result = self.load_cached_result(pdf_path, page_range=page_range, cache_dir=cache_dir)
            if cached_result is not None:
                self._log_cache_status(pdf_path, page_range, hit=True)
                self.cache_stats["hit"] = self.cache_stats.get("hit", 0) + 1
                self.log_cache_summary()
                return cached_result
            self._log_cache_status(pdf_path, page_range, hit=False)
            logger.info("📦 Running live OCR for %s %s.", pdf_path.name, f"[{page_range or 'full'}]" if page_range else "[full]")
            self.cache_stats["miss"] = self.cache_stats.get("miss", 0) + 1

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
            ocr_kwargs = self._build_ocr_process_kwargs(
                model=self.model_name,
                document={"type": "document_url", "document_url": signed_url.url},
                include_image_base64=True,
            )
            ocr_response = self.client.ocr.process(**ocr_kwargs)

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
                    extracted_images[clean_id] = file_path
                    page_images[clean_id] = file_path
                    image_counter += 1

                pages_data.append(OCRPageData(
                    page_num=current_page_num,
                    markdown=page_markdown,
                    images=page_images
                ))

            result = OCRResult(full_markdown.strip(), extracted_images, pages_data)
            if extracted_images and enhance:
                self.enhance_extracted_images(img_output_dir)

            if self.enable_cache:
                self.save_cached_result(pdf_path, page_range, result, cache_dir=cache_dir)
                logger.info("📦 OCR cache saved for %s [%s].", pdf_path.name, page_range or "full")
            else:
                logger.info("📦 OCR cache disabled; skipping save for %s [%s].", pdf_path.name, page_range or "full")
            logger.info(f"Mistral OCR extracted {image_counter} image(s) across {len(pages_data)} page(s).")
            self.log_cache_summary()
            return result

        finally:
            if is_temp and sliced_pdf.exists():
                try:
                    os.remove(sliced_pdf)
                except Exception:
                    pass
