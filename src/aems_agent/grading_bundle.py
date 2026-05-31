# SPDX-License-Identifier: AGPL-3.0-or-later

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

# Tokens that indicate mathematical/formula content on a page.
_MATH_TOKENS = [
    "\\frac",
    "\\sum",
    "\\int",
    "\\sqrt",
    "\\partial",
    "\\infty",
    "\\alpha",
    "\\beta",
    "\\gamma",
    "\\delta",
    "\\epsilon",
    "\\sigma",
    "\\theta",
    "\\lambda",
    "\\mu",
    "\\phi",
    "\\omega",
    "\\pi",
    "≥",
    "≤",
    "≠",
    "≈",
    "∑",
    "∫",
    "√",
    "∂",
    "∞",
    "→",
    "⇒",
]
_MATH_OPERATORS = set("=+-/^×·∙")


def _page_has_formulas(text: str) -> bool:
    """Return True if the page text looks formula-heavy (matches server logic)."""
    math_score = 0
    lower = text.lower()
    for tok in _MATH_TOKENS:
        math_score += lower.count(tok.lower())
    for ch in text:
        if ch in _MATH_OPERATORS:
            math_score += 1
    return math_score >= 10


def _page_has_images(page) -> bool:
    """Return True if the PDF page contains embedded images or drawings."""
    try:
        return len(page.get_images(full=False)) > 0 or len(page.get_drawings()) > 0
    except Exception:
        return False


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
                return json.loads(cached_path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
            except (json.JSONDecodeError, OSError):
                pass  # Cache corrupted, regenerate

    doc = fitz.open(str(pdf_path))
    try:
        total_pages = len(doc)
        pages_to_process = min(total_pages, max_pages) if max_pages else total_pages

        pages: List[Dict[str, Any]] = []
        handwriting_pages = 0
        total_text_quality = 0.0

        # Pre-scan: detect if any page has handwriting (low extractable text).
        # This must run before the main loop so the "smart" strategy can
        # decide to include images for ALL pages of handwritten documents.
        doc_has_handwriting = False
        if strategy == "smart":
            for j in range(pages_to_process):
                pg = doc[j]
                if (
                    len(pg.get_text("text").strip()) < _MIN_TEXT_LENGTH
                    and pg.rect.width * pg.rect.height > 0
                ):
                    doc_has_handwriting = True
                    break

        for i in range(pages_to_process):
            page = doc[i]
            text = page.get_text("text")
            rect = page.rect
            width = rect.width
            height = rect.height

            # Handwriting heuristic: low text density
            page_area = width * height
            is_low_text = len(text.strip()) < _MIN_TEXT_LENGTH

            if is_low_text and page_area > 0:
                handwriting_pages += 1

            # Per-page visual content detection (for smart strategy)
            has_figures = _page_has_images(page)
            has_formulas = _page_has_formulas(text)

            # A page "needs vision" when we plan to include an image for it.
            # The server's bundle_adapter uses has_handwriting / needs_ocr to
            # decide whether pre_rendered_images apply to this page, so these
            # flags must be True whenever we render an image.
            page_needs_vision = (
                (is_low_text or doc_has_handwriting or has_figures or has_formulas)
                if strategy == "smart"
                else (strategy == "multimodal")
            )

            # For handwritten pages, PyMuPDF's get_text() produces garbled
            # characters that confuse the LLM (it tries to interpret garbage
            # text instead of looking at the image). Clear the text only on
            # the specific low-text pages so the LLM relies on visual content
            # for those, while the long typed pages keep their native text
            # for downstream native-text routing on the server. The earlier
            # doc-wide gate (`doc_has_handwriting`) zeroed text on every
            # page of typed reports that happen to start with a short cover
            # (Phase 9 salmi_simon.pdf, 2026-05-13).
            effective_text = text
            if is_low_text and strategy == "smart":
                effective_text = ""

            page_data: Dict[str, Any] = {
                "page_number": i + 1,
                "text": effective_text,
                "width": width,
                "height": height,
                "has_handwriting": page_needs_vision,
                "needs_ocr": page_needs_vision,
                "has_figures": has_figures,
                "has_formulas": has_formulas,
            }

            # Text quality estimation (text density as proxy)
            text_density = len(text.strip()) / page_area if page_area > 0 else 0.0
            page_quality = min(1.0, text_density / 0.001) if page_area > 0 else 0.0
            total_text_quality += page_quality

            # Image rendering based on strategy.
            # Matches server-side logic in processing.py:needs_vision_refinement:
            #   - handwritten docs: all pages get images (garbled OCR)
            #   - printed docs: pages with embedded figures or formulas get images
            #     (text extraction misses diagrams and math notation)
            needs_image = False
            if strategy == "multimodal":
                needs_image = True
            elif strategy == "smart":
                needs_image = (
                    is_low_text  # very little extractable text
                    or doc_has_handwriting  # any page is handwritten → all need images
                    or has_figures  # page has embedded images / diagrams
                    or has_formulas  # page has math notation
                )
            # text_only: never render images

            if needs_image:
                pixmap = page.get_pixmap(dpi=dpi, alpha=False)
                image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                webp_buffer = BytesIO()
                # Lossy WebP at quality=85 — matches the server-side encoder
                # in providers/ollama/provider.py and the rest of the AEMS
                # image pipeline. Lossless WebP at quality=95 was producing
                # 50-150 MB JSON bundles for multi-page handwritten exams,
                # which exceeded the AEMS server's request-body cap and broke
                # offline-local grading (Zohar's 413 at 39%, 2026-05-26).
                # Vision LLMs do not benefit from lossless input.
                image.save(webp_buffer, format="WEBP", quality=85, method=6)
                img_bytes = webp_buffer.getvalue()
                encoded_image = base64.b64encode(img_bytes).decode("ascii")
                page_data["image_base64"] = encoded_image
                # Compatibility alias for consumers that expect a plural image list.
                page_data["images"] = [encoded_image]
            else:
                page_data["images"] = []

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
            cached_path.write_text(json.dumps(bundle, ensure_ascii=False), encoding="utf-8")
        except OSError:
            logger.warning("Failed to write bundle cache at %s", cached_path)

    return bundle
