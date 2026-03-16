"""Grading bundle generation for local-mode grading.

Reads a submission PDF, extracts text and optionally renders page images,
detects handwriting/OCR quality signals, and returns a structured bundle
for the server's grading service.
"""

import base64
import hashlib
import json
import logging
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Optional

import fitz  # PyMuPDF
from PIL import Image

logger = logging.getLogger(__name__)

# Minimum text length per page to consider "has substantial text"
_MIN_TEXT_LENGTH = 50

# Below this threshold (chars per page area unit), page is considered "low text"
_LOW_TEXT_DENSITY_THRESHOLD = 0.0001


def get_cache_key(
    pdf_path: Path,
    strategy: str,
    dpi: int,
    max_pages: Optional[int],
) -> str:
    """Build a cache key from PDF identity + request parameters."""
    stat = pdf_path.stat()
    identity = f"{stat.st_mtime_ns}:{stat.st_size}"
    params = f"{strategy}:{dpi}:{max_pages}"
    raw = f"{identity}|{params}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def get_cache_path(
    cache_dir: Path,
    cache_key: str,
    strategy: str,
    dpi: int,
    max_pages: Optional[int],
) -> Path:
    """Build the filesystem path for a cached bundle."""
    filename = f"{strategy}_{dpi}_{max_pages}_{cache_key}.json"
    return cache_dir / filename


def generate_bundle(
    pdf_path: Path,
    strategy: str = "text_only",
    dpi: int = 150,
    max_pages: Optional[int] = None,
    cache_dir: Optional[Path] = None,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Generate a grading input bundle from a submission PDF.

    Args:
        pdf_path: Path to the submission PDF.
        strategy: 'text_only', 'multimodal', or 'smart'.
        dpi: Image rendering DPI (only used when images are rendered).
        max_pages: Limit the number of pages processed (None = all).
        cache_dir: Directory for bundle caching (None = no caching).
        force_refresh: If True, bypass the cache.

    Returns:
        Bundle dict with bundle_version, strategy, pages, metadata.

    Raises:
        FileNotFoundError: If pdf_path does not exist.
    """
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    # Check cache
    if cache_dir and not force_refresh:
        cache_key = get_cache_key(pdf_path, strategy, dpi, max_pages)
        cached_path = get_cache_path(cache_dir, cache_key, strategy, dpi, max_pages)
        if cached_path.exists():
            try:
                return json.loads(cached_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass  # Cache corrupted, regenerate

    doc = fitz.open(str(pdf_path))
    try:
        total_pages = len(doc)
        pages_to_process = min(total_pages, max_pages) if max_pages else total_pages

        pages: List[Dict[str, Any]] = []
        handwriting_pages = 0
        total_text_quality = 0.0

        for i in range(pages_to_process):
            page = doc[i]
            text = page.get_text("text")
            rect = page.rect
            width = rect.width
            height = rect.height

            page_data: Dict[str, Any] = {
                "page_number": i + 1,
                "text": text,
                "width": width,
                "height": height,
            }

            # Handwriting heuristic: low text density
            page_area = width * height
            is_low_text = len(text.strip()) < _MIN_TEXT_LENGTH

            if is_low_text and page_area > 0:
                handwriting_pages += 1

            # Text quality estimation (text density as proxy)
            text_density = len(text.strip()) / page_area if page_area > 0 else 0.0
            page_quality = min(1.0, text_density / 0.001) if page_area > 0 else 0.0
            total_text_quality += page_quality

            # Image rendering based on strategy
            needs_image = False
            if strategy == "multimodal":
                needs_image = True
            elif strategy == "smart":
                needs_image = is_low_text  # Render image for low-text pages
            # text_only: never render images

            if needs_image:
                pixmap = page.get_pixmap(dpi=dpi)
                png_bytes = pixmap.tobytes("png")
                image = Image.open(BytesIO(png_bytes))
                webp_buffer = BytesIO()
                image.save(webp_buffer, format="WEBP", quality=95)
                img_bytes = webp_buffer.getvalue()
                page_data["image_base64"] = base64.b64encode(img_bytes).decode("ascii")

            pages.append(page_data)

        avg_quality = total_text_quality / pages_to_process if pages_to_process > 0 else 0.0

        bundle: Dict[str, Any] = {
            "bundle_version": 1,
            "strategy": strategy,
            "pages": pages,
            "metadata": {
                "page_count": pages_to_process,
                "total_pages": total_pages,
                "has_handwriting": handwriting_pages > 0,
                "avg_ocr_quality": round(avg_quality, 3),
                "bundle_size_bytes": 0,  # Placeholder, updated after serialization
                "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        }

        # Calculate size and update
        bundle_json = json.dumps(bundle, ensure_ascii=False)
        bundle["metadata"]["bundle_size_bytes"] = len(bundle_json.encode("utf-8"))

    finally:
        doc.close()

    # Write to cache
    if cache_dir:
        cache_key = get_cache_key(pdf_path, strategy, dpi, max_pages)
        cached_path = get_cache_path(cache_dir, cache_key, strategy, dpi, max_pages)
        try:
            cached_path.parent.mkdir(parents=True, exist_ok=True)
            cached_path.write_text(
                json.dumps(bundle, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            logger.warning("Failed to write bundle cache at %s", cached_path)

    return bundle
