# SPDX-License-Identifier: AGPL-3.0-or-later

"""Annotation CRUD operations for the AEMS local agent.

Wraps the shared ``aems_pdf_annotator`` package with input validation,
response serialization, and file-version tracking that matches the server's
JSON shapes consumed by the browser review UI.
"""

import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from aems_pdf_annotator import (
    PDFAnnotator,
    PDFAnnotation,
    BBox,
    AnnotationType,
    AnnotationColor,
    AnnotationSource,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_COMMENT_LENGTH = 10_240
MAX_IDENTIFIER_LENGTH = 500

# PDF annotation type names that must not be mistaken for stable IDs.
_ANNOTATION_TYPE_NAMES = {
    "Text",
    "Note",
    "Highlight",
    "Underline",
    "Squiggly",
    "StrikeOut",
    "FreeText",
    "Square",
    "Circle",
    "Line",
    "Polygon",
    "PolyLine",
    "Stamp",
    "Caret",
    "Ink",
    "Popup",
    "FileAttachment",
    "Sound",
}

_VALID_COLORS = {"red", "amber", "green", "yellow"}

_COLOR_MAP: Dict[str, AnnotationColor] = {
    "red": AnnotationColor.RED,
    "critical": AnnotationColor.RED,
    "amber": AnnotationColor.AMBER,
    "yellow": AnnotationColor.AMBER,
    "green": AnnotationColor.GREEN,
    "low": AnnotationColor.GREEN,
}

_KIND_MAP: Dict[str, AnnotationType] = {
    "text": AnnotationType.TEXT,
    "highlight": AnnotationType.HIGHLIGHT,
    "squiggly": AnnotationType.SQUIGGLY,
    "underline": AnnotationType.UNDERLINE,
    "strikeout": AnnotationType.STRIKEOUT,
    "textbox": AnnotationType.TEXTBOX,
    "drawing": AnnotationType.DRAWING,
}

_IDENTIFIER_RE = re.compile(r"^[a-zA-Z0-9:|\-_]+$")


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------


def _pdf_rect_to_pymupdf(rect: List[float], page_height: float) -> List[float]:
    """Convert a PDF rect (bottom-left origin) to PyMuPDF rect (top-left origin).

    The browser sends coordinates in PDF space.  ``PDFAnnotator.add_annotation``
    expects BBox values in PyMuPDF space for rect-based annotations (highlight,
    underline, etc.).  ``update_annotation`` converts internally, so this helper
    is only needed for the *add* path.
    """
    x0, y0_pdf, x1, y1_pdf = rect
    # In PDF: y0 < y1 (bottom < top).  In PyMuPDF: y0 < y1 (top < bottom).
    y0_mu = page_height - y1_pdf
    y1_mu = page_height - y0_pdf
    return [x0, y0_mu, x1, y1_mu]


# ---------------------------------------------------------------------------
# File-level lock manager
# ---------------------------------------------------------------------------

_pdf_locks: Dict[str, threading.Lock] = {}
_pdf_locks_guard = threading.Lock()


def _get_pdf_lock(pdf_path: Path) -> threading.Lock:
    """Return a per-path lock for serializing PDF mutations."""
    key = str(pdf_path)
    with _pdf_locks_guard:
        if key not in _pdf_locks:
            _pdf_locks[key] = threading.Lock()
        return _pdf_locks[key]


# ---------------------------------------------------------------------------
# Identifier parsing
# ---------------------------------------------------------------------------


def _is_annotation_type_name(value: Optional[str]) -> bool:
    """Check if a value is a PDF annotation type name (not a valid stable ID)."""
    if not value:
        return False
    return str(value).strip() in _ANNOTATION_TYPE_NAMES


def resolve_annotation_identifier(identifier: str) -> Tuple[Optional[int], Optional[str]]:
    """Return ``(xref, stable_id)`` derived from *identifier*.

    Handles UUID strings, plain xref integers, composite
    ``xref:N|id:uuid`` format, and legacy ``Note:N`` format.
    """
    if not identifier:
        return None, None

    if len(str(identifier)) > MAX_IDENTIFIER_LENGTH:
        logger.warning("Annotation identifier exceeds max length: %d", len(str(identifier)))
        return None, None

    try:
        identifier_str = unquote(str(identifier)).strip()
    except Exception:
        identifier_str = str(identifier).strip()

    if not identifier_str:
        return None, None

    # Validate allowed characters
    if not _IDENTIFIER_RE.match(identifier_str):
        logger.warning("Annotation identifier contains invalid characters")
        return None, None

    xref: Optional[int] = None
    stable_id: Optional[str] = None

    # Handle composite formats
    if "xref:" in identifier_str or "id:" in identifier_str or "|" in identifier_str:
        parts = identifier_str.split("|")
        for part in parts:
            part = part.strip()
            if not part:
                continue
            if part.startswith("xref:"):
                try:
                    xref_value = part.split(":", 1)[1]
                    if xref_value:
                        xref = int(xref_value)
                except (ValueError, TypeError):
                    continue
            elif part.startswith("id:"):
                stable_value = part.split(":", 1)[1]
                if stable_value:
                    stable_id = stable_value
            else:
                if xref is None:
                    try:
                        xref = int(part)
                        continue
                    except (ValueError, TypeError):
                        pass
                if stable_id is None:
                    stable_id = part

        if stable_id:
            if _is_annotation_type_name(stable_id):
                stable_id = None
            elif xref is not None and str(xref) == str(stable_id):
                stable_id = None

        return xref, stable_id

    # Handle "Note:1" format
    if ":" in identifier_str and not identifier_str.startswith(("xref:", "id:")):
        parts = identifier_str.split(":", 1)
        if len(parts) == 2:
            stable_id = parts[1].strip()
            return None, stable_id

    # Try pure numeric as xref
    try:
        xref = int(identifier_str)
        return xref, None
    except (TypeError, ValueError):
        return None, identifier_str


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def serialize_annotation_entry(annotation: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Convert an annotation dict (from ``get_annotations_on_page``) to the
    JSON shape expected by the browser.

    The shared package already returns dicts with ``stable_id``, ``xref``,
    ``source``, etc.  This function normalises a few fields and adds derived
    ones (``can_revert_to_ai``, ``id``).
    """
    if not annotation:
        return None

    serialized: Dict[str, Any] = dict(annotation)

    # Ensure rect is a list
    rect = serialized.get("rect")
    if rect and isinstance(rect, tuple):
        serialized["rect"] = list(rect)

    # Resolve stable_id / id
    stable_id = serialized.get("stable_id")
    xref = serialized.get("xref")

    preferred_id = stable_id
    if not preferred_id:
        if xref is not None:
            preferred_id = str(xref)
    serialized["stable_id"] = stable_id if stable_id else None
    serialized["id"] = preferred_id

    # Source tracking
    source = serialized.get("source") or "AI"
    original_source = serialized.get("original_source")
    serialized["source"] = source
    serialized["original_source"] = original_source
    serialized["can_revert_to_ai"] = source == "HUMAN" and original_source == "AI"

    # Verdict flag
    serialized["is_verdict"] = bool(serialized.get("is_verdict", False))

    return serialized


def select_annotation_entry(
    annotations: List[Dict[str, Any]],
    stable_id: Optional[str],
    xref: Optional[int],
) -> Optional[Dict[str, Any]]:
    """Locate an annotation within a serialized list by stable id or xref."""
    for ann in annotations:
        if stable_id:
            ann_stable_id = ann.get("stable_id")
            if ann_stable_id and str(ann_stable_id) == str(stable_id):
                return ann
            # Fallback
            for key in ("id", "name", "title"):
                candidate = ann.get(key)
                if candidate and str(candidate) == str(stable_id):
                    return ann
        if xref is not None and ann.get("xref") == xref:
            return ann
    return None


# ---------------------------------------------------------------------------
# Version token
# ---------------------------------------------------------------------------


def build_version_token(pdf_path: Path) -> str:
    """Build a file version token from ``st_mtime_ns`` and ``st_size``."""
    stat = pdf_path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_page_index(page_index: Any, page_count: int) -> int:
    """Validate and return *page_index* as a non-negative int within range."""
    if page_index is None:
        raise ValueError("page_index is required")
    if not isinstance(page_index, int) or page_index < 0:
        raise ValueError("page_index must be a non-negative integer")
    if page_index >= page_count:
        raise ValueError(f"page_index {page_index} out of range (PDF has {page_count} pages)")
    return page_index


def _validate_rect(rect: Any) -> List[float]:
    """Validate a rect payload and return as a list of 4 floats."""
    if not isinstance(rect, (list, tuple)) or len(rect) != 4:
        raise ValueError("rect must be [x0, y0, x1, y1]")
    values = [float(v) for v in rect]
    if any(v < 0 for v in values):
        raise ValueError("rect values must be non-negative")
    if values[0] >= values[2] or values[1] >= values[3]:
        raise ValueError("rect requires x0 < x1 and y0 < y1")
    return values


def _validate_content(content: Any) -> str:
    """Validate and return content string."""
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    if len(content) > MAX_COMMENT_LENGTH:
        raise ValueError(f"content exceeds maximum length of {MAX_COMMENT_LENGTH} characters")
    return content


def _validate_color(color_name: str) -> AnnotationColor:
    """Validate color name and return the enum."""
    normalized = color_name.strip().lower()
    if normalized not in _COLOR_MAP:
        raise ValueError(f"color must be one of: {', '.join(sorted(_VALID_COLORS))}")
    return _COLOR_MAP[normalized]


def _validate_kind(kind_name: str) -> AnnotationType:
    """Validate annotation kind and return the enum."""
    normalized = kind_name.strip().lower()
    if normalized not in _KIND_MAP:
        raise ValueError(f"type must be one of: {', '.join(sorted(_KIND_MAP))}")
    return _KIND_MAP[normalized]


def _validate_points(points: Any) -> List[List[float]]:
    """Validate drawing points."""
    if not isinstance(points, list) or len(points) < 2:
        raise ValueError("points must be a list with at least 2 [x, y] pairs")
    for pt in points:
        if not isinstance(pt, list) or len(pt) != 2:
            raise ValueError("Each point must be [x, y]")
        if not all(isinstance(v, (int, float)) for v in pt):
            raise ValueError("Point coordinates must be numbers")
    return [[float(p[0]), float(p[1])] for p in points]


def _validate_stroke_color_rgb(value: Any) -> List[int]:
    """Validate RGB stroke color."""
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError("stroke_color_rgb must be a list of 3 integers")
    if any(not isinstance(c, int) for c in value):
        raise ValueError("stroke_color_rgb values must be integers")
    if any(c < 0 or c > 255 for c in value):
        raise ValueError("stroke_color_rgb values must be between 0 and 255")
    return value


def _validate_annotation_id(annotation_id: str) -> str:
    """Validate annotation identifier format."""
    if not annotation_id:
        raise ValueError("annotation_id is required")
    if len(annotation_id) > MAX_IDENTIFIER_LENGTH:
        raise ValueError("annotation_id exceeds maximum length")
    if not _IDENTIFIER_RE.match(annotation_id):
        raise ValueError("annotation_id contains invalid characters")
    return annotation_id


def _parse_bool_field(payload: Dict[str, Any], field_name: str) -> Optional[bool]:
    """Parse an optional boolean field from payload."""
    if field_name not in payload:
        return None
    value = payload[field_name]
    if isinstance(value, bool):
        return value
    raise ValueError(f"{field_name} must be a boolean")


# ---------------------------------------------------------------------------
# Core CRUD operations
# ---------------------------------------------------------------------------


def list_annotations(pdf_path: Path) -> Dict[str, Any]:
    """List all annotations in the PDF, grouped by page index."""
    annotations_by_page: Dict[str, List[Dict[str, Any]]] = {}

    with PDFAnnotator(pdf_path) as annotator:
        if annotator.doc:
            for page_idx in range(annotator.doc.page_count):
                page_annotations = annotator.get_annotations_on_page(page_idx)
                if page_annotations:
                    raw = [serialize_annotation_entry(ann) for ann in page_annotations]
                    # Filter out None entries
                    serialized: list[Dict[str, Any]] = [s for s in raw if s is not None]
                    if serialized:
                        annotations_by_page[str(page_idx)] = serialized

    return {"success": True, "annotations": annotations_by_page}


def get_version(pdf_path: Path) -> Dict[str, Any]:
    """Return a version token for the PDF file."""
    version = build_version_token(pdf_path)
    return {"success": True, "version": version}


def add_annotation(pdf_path: Path, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Add a new annotation to the PDF.

    Raises ``ValueError`` on invalid input.
    """
    # --- Validate required fields ---
    with PDFAnnotator(pdf_path) as probe:
        page_count = probe.doc.page_count if probe.doc else 0

    if "page_index" not in payload:
        raise ValueError("page_index is required")
    page_index = _validate_page_index(payload["page_index"], page_count)

    # Content
    content = (payload.get("content") or "").strip()
    kind_str = (payload.get("kind") or payload.get("type") or "text").strip().lower()

    if content:
        _validate_content(content)

    # Kind
    kind = _validate_kind(kind_str)

    # Color
    color_name = (payload.get("color") or "amber").strip().lower()
    color = _validate_color(color_name)

    # Rect (optional)
    rect_values: Optional[List[float]] = None
    if payload.get("rect") is not None:
        rect_values = _validate_rect(payload["rect"])

    # Source
    source_str = (payload.get("source") or "HUMAN").strip().upper()
    source = AnnotationSource.HUMAN if source_str == "HUMAN" else AnnotationSource.AI

    # Verdict
    is_verdict = _parse_bool_field(payload, "is_verdict") or False

    # Grader name
    grader_name = (payload.get("grader_name") or "Teacher").strip()

    # Drawing-specific fields
    drawing_style: Optional[str] = payload.get("drawing_style")
    points: Optional[List[List[float]]] = None
    stroke_width: Optional[float] = payload.get("stroke_width")
    stroke_opacity: Optional[float] = payload.get("stroke_opacity")
    stroke_color_rgb: Optional[List[int]] = None

    if payload.get("points") is not None:
        points = _validate_points(payload["points"])

    if payload.get("stroke_color_rgb") is not None:
        stroke_color_rgb = _validate_stroke_color_rgb(payload["stroke_color_rgb"])

    # Validate drawing-specific requirements
    if kind_str == "drawing":
        if drawing_style not in ("pen", "highlighter"):
            raise ValueError("drawing_style required for drawing type")
        if not points or len(points) < 2:
            raise ValueError("points required with at least 2 points")
        if stroke_width is not None and (
            not isinstance(stroke_width, (int, float)) or stroke_width <= 0
        ):
            raise ValueError("stroke_width must be a positive number")
        if stroke_opacity is not None and (
            not isinstance(stroke_opacity, (int, float)) or not (0 <= stroke_opacity <= 1)
        ):
            raise ValueError("stroke_opacity must be 0.0-1.0")

    if kind_str == "textbox" and rect_values is None:
        raise ValueError("rect required for textbox type")

    # --- Acquire lock, write annotation ---
    lock = _get_pdf_lock(pdf_path)
    with lock:
        with PDFAnnotator(pdf_path) as annotator:
            if not annotator.doc:
                raise RuntimeError("Failed to open PDF")

            page = annotator.doc[page_index]
            page_rect = page.rect
            page_width = page_rect.width or 612.0
            page_height = page_rect.height or 792.0

            # Build bbox — browser sends PDF coords (bottom-left origin).
            # PDFAnnotator.add_annotation uses BBox directly as PyMuPDF coords
            # for rect-based annotations, so we convert once here (like the
            # server does in its create handler).
            # Drawing points are converted internally by PDFAnnotator.
            bbox: Optional[BBox] = None
            if rect_values is not None:
                pymupdf_rect = _pdf_rect_to_pymupdf(rect_values, page_height)
                bbox = BBox.from_list(pymupdf_rect)
            else:
                # Generate default rect in PyMuPDF coords
                default_width = min(220.0, page_width - 96.0)
                default_height = 120.0
                x0 = 48.0
                y0 = max(48.0, page_height - default_height - 72.0)
                bbox = BBox(
                    x0=x0,
                    y0=y0,
                    x1=min(page_width - 48.0, x0 + default_width),
                    y1=min(page_height - 48.0, y0 + default_height),
                )

            annotation = PDFAnnotation(
                page_index=page_index,
                bbox=bbox,
                kind=kind,
                color=color,
                comment=content,
                is_system_generated=(source == AnnotationSource.AI),
                grader_name=grader_name,
                source=source,
                is_verdict=is_verdict,
                drawing_style=drawing_style,
                points=points,
                stroke_width=stroke_width,
                stroke_opacity=stroke_opacity,
                stroke_color_rgb=stroke_color_rgb,
            )

            if not annotator.add_annotation(annotation):
                raise RuntimeError("Failed to add annotation to PDF")

            annotator.save()

            # Find the newly created annotation
            page_annotations = annotator.get_annotations_on_page(page_index)
            created = select_annotation_entry(page_annotations, stable_id=annotation.id, xref=None)
            created_serialized = serialize_annotation_entry(created)

            if not created_serialized:
                # Fallback: build a minimal response
                created_serialized = {
                    "type": annotation.kind.value,
                    "rect": bbox.to_list(),
                    "color": color.value,
                    "content": content,
                    "id": annotation.id,
                    "stable_id": annotation.id,
                    "xref": None,
                    "page_index": page_index,
                    "grader_name": grader_name,
                    "source": annotation.source.value,
                    "original_source": (
                        annotation.original_source.value if annotation.original_source else None
                    ),
                    "can_revert_to_ai": annotation.can_revert_to_ai(),
                    "is_verdict": annotation.is_verdict,
                }

    return {"success": True, "annotation": created_serialized}


def update_annotation(
    pdf_path: Path,
    annotation_id: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Update an existing annotation.

    Raises ``ValueError`` on invalid input, ``FileNotFoundError`` if the
    annotation is not found.
    """
    _validate_annotation_id(annotation_id)
    xref_value, stable_id = resolve_annotation_identifier(annotation_id)

    # Parse and validate fields
    new_content: Optional[str] = payload.get("content")
    new_rect: Optional[List[float]] = None
    new_color: Optional[str] = payload.get("color")
    new_page_index: Optional[int] = payload.get("page_index")
    new_source: Optional[str] = payload.get("source")
    new_is_verdict: Optional[bool] = _parse_bool_field(payload, "is_verdict")
    new_points: Optional[List[List[float]]] = None
    new_stroke_color_rgb: Optional[List[int]] = None
    grader_name: Optional[str] = payload.get("grader_name")

    if payload.get("rect") is not None:
        new_rect = _validate_rect(payload["rect"])

    if payload.get("points") is not None:
        new_points = _validate_points(payload["points"])

    if payload.get("stroke_color_rgb") is not None:
        new_stroke_color_rgb = _validate_stroke_color_rgb(payload["stroke_color_rgb"])

    if new_content is not None:
        _validate_content(new_content)

    # Check at least one update field provided
    if (
        new_content is None
        and new_rect is None
        and new_color is None
        and new_page_index is None
        and new_source is None
        and new_is_verdict is None
        and new_points is None
        and new_stroke_color_rgb is None
    ):
        raise ValueError(
            "Must provide at least one of: content, rect, color, page_index, "
            "source, is_verdict, points, stroke_color_rgb"
        )

    # Build the canonical identifier for the shared package
    canonical_identifier = stable_id or (str(xref_value) if xref_value else annotation_id)

    lock = _get_pdf_lock(pdf_path)
    with lock:
        with PDFAnnotator(pdf_path) as annotator:
            # Grader name fallback: payload > existing annotation author > "Teacher"
            if not grader_name:
                existing = (
                    annotator.find_annotation_by_id(canonical_identifier) if stable_id else None
                )
                if not existing and xref_value is not None:
                    existing = annotator.find_annotation_by_xref(xref_value)
                if existing:
                    page_idx_existing, _annot_obj = existing
                    page_anns = annotator.get_annotations_on_page(page_idx_existing)
                    found = select_annotation_entry(page_anns, stable_id=stable_id, xref=xref_value)
                    if found:
                        grader_name = found.get("grader_name") or None
                if not grader_name:
                    grader_name = "Teacher"

            # Convert rect from PDF-space (bottom-left origin) to
            # PyMuPDF-space (top-left origin), same as add_annotation does.
            rect_tuple = None
            if new_rect is not None:
                target_pg = new_page_index
                if target_pg is None:
                    found = (
                        annotator.find_annotation_by_id(canonical_identifier)
                        if stable_id
                        else None
                    )
                    if not found and xref_value is not None:
                        found = annotator.find_annotation_by_xref(xref_value)
                    target_pg = found[0] if found else 0
                page_obj = annotator.doc[target_pg]
                page_height = page_obj.rect.height or 792.0
                rect_tuple = tuple(
                    _pdf_rect_to_pymupdf(list(new_rect), page_height)
                )

            update_result = annotator.update_annotation(
                annotation_identifier=canonical_identifier,
                new_content=new_content,
                new_rect=rect_tuple,
                new_color=new_color,
                new_page_index=new_page_index,
                grader_name=grader_name,
                new_source=new_source,
                new_is_verdict=new_is_verdict,
                new_points=new_points,
                new_stroke_color_rgb=new_stroke_color_rgb,
            )

            # Handle cross-page moves which return a tuple
            if isinstance(update_result, tuple):
                success, target_page, new_xref = update_result
                if success and new_xref is not None:
                    xref_value = new_xref
            else:
                success = update_result

            if not success:
                raise FileNotFoundError("Annotation not found or update failed")

            annotator.save()

            # Re-read annotations to find the updated one
            updated_ann: Optional[Dict[str, Any]] = None

            # Try to find via public methods
            if stable_id:
                result = annotator.find_annotation_by_id(stable_id)
                if result:
                    page_idx, _annot = result
                    page_annotations = annotator.get_annotations_on_page(page_idx)
                    updated_ann = select_annotation_entry(
                        page_annotations, stable_id=stable_id, xref=xref_value
                    )

            if not updated_ann and xref_value is not None:
                result = annotator.find_annotation_by_xref(xref_value)
                if result:
                    page_idx, _annot = result
                    page_annotations = annotator.get_annotations_on_page(page_idx)
                    updated_ann = select_annotation_entry(
                        page_annotations, stable_id=stable_id, xref=xref_value
                    )

            serialized = serialize_annotation_entry(updated_ann)

    return {"success": True, "annotation": serialized}


def delete_annotation(pdf_path: Path, annotation_id: str) -> Dict[str, Any]:
    """Delete an annotation from the PDF.

    Raises ``FileNotFoundError`` if the annotation is not found.
    """
    _validate_annotation_id(annotation_id)
    xref_value, stable_id = resolve_annotation_identifier(annotation_id)

    canonical_identifier = stable_id or (str(xref_value) if xref_value else annotation_id)

    lock = _get_pdf_lock(pdf_path)
    with lock:
        with PDFAnnotator(pdf_path) as annotator:
            success = annotator.delete_annotation(canonical_identifier)
            if not success:
                raise FileNotFoundError("Annotation not found or delete failed")
            annotator.save()

    return {"success": True, "message": "Annotation deleted"}
