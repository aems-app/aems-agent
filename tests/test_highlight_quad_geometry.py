"""Regression tests for text-anchored highlight quad geometry in the local agent.

Mirror of the AEMS canvas/offline test (codex 2026-07-20 finding 2). The agent's
``annotation_crud`` handled the interactive extend/shorten quad persist with a
naive ``page_height`` flip, which mis-places the highlight on a CropBox-offset or
rotated page. The fix routes quads through fitz ``page.transformation_matrix``.
These are RED against the pre-fix naive flip on the CropBox / rotation cases and
GREEN with ``_pdf_quad_to_pymupdf``; the normal-page test pins the strict no-op.
"""
from __future__ import annotations

from typing import List, Tuple

import fitz
import pytest

from aems_agent.annotation_crud import (
    _page_quad_geometry_supported,
    _pdf_quad_to_pymupdf,
    _pdf_rect_to_pymupdf,
)


def _make_page(
    cropbox: Tuple[float, float, float, float] | None = None, rotation: int = 0
) -> Tuple[fitz.Document, fitz.Page]:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((120, 200), "HELLOWORLD", fontsize=24)
    if cropbox is not None:
        page.set_cropbox(fitz.Rect(*cropbox))
    if rotation:
        page.set_rotation(rotation)
    return doc, page


def _pdf_space_quad(page: fitz.Page) -> List[float]:
    r = page.search_for("HELLOWORLD")[0]
    pdf = fitz.Rect(r) * ~page.transformation_matrix
    return [pdf.x0, pdf.y0, pdf.x1, pdf.y1]


def _dist(a: List[float], b: List[float]) -> float:
    ca = ((a[0] + a[2]) / 2, (a[1] + a[3]) / 2)
    cb = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
    return ((ca[0] - cb[0]) ** 2 + (ca[1] - cb[1]) ** 2) ** 0.5


def test_normal_page_is_strict_noop_vs_rect_path() -> None:
    doc, page = _make_page()
    try:
        quad = _pdf_space_quad(page)
        geo = _pdf_quad_to_pymupdf(list(quad), page)
        naive = _pdf_rect_to_pymupdf(list(quad), page.rect.height)
        assert geo == pytest.approx(naive, abs=1e-6)
    finally:
        doc.close()


def test_cropbox_offset_lands_on_text_and_differs_from_naive() -> None:
    doc, page = _make_page(cropbox=(50, 30, 562, 762))
    try:
        text = list(page.search_for("HELLOWORLD")[0])
        quad = _pdf_space_quad(page)
        assert _dist(_pdf_quad_to_pymupdf(list(quad), page), text) < 2.0
        assert _dist(_pdf_rect_to_pymupdf(list(quad), page.rect.height), text) > 5.0
    finally:
        doc.close()


@pytest.mark.parametrize("rotation", [90, 180, 270])
def test_rotated_page_lands_on_text(rotation: int) -> None:
    doc, page = _make_page(rotation=rotation)
    try:
        text = list(page.search_for("HELLOWORLD")[0])
        assert _dist(_pdf_quad_to_pymupdf(list(_pdf_space_quad(page)), page), text) < 2.0
    finally:
        doc.close()


def test_geometry_supported_matrix() -> None:
    for cropbox, rotation, expected in [
        (None, 0, True),
        ((50, 30, 562, 762), 0, True),
        (None, 90, True),
        ((40, 25, 572, 767), 90, False),
    ]:
        doc, page = _make_page(cropbox=cropbox, rotation=rotation)
        try:
            assert _page_quad_geometry_supported(page) is expected
        finally:
            doc.close()
