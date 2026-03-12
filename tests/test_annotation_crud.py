"""Tests for annotation CRUD operations."""

import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from aems_pdf_annotator._fitz import fitz
from aems_pdf_annotator import (
    PDFAnnotator,
    PDFAnnotation,
    BBox,
    AnnotationType,
    AnnotationColor,
    AnnotationSource,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_pdf(tmp_path: Path) -> Path:
    """Create a blank 2-page PDF."""
    pdf_path = tmp_path / "empty.pdf"
    doc = fitz.open()
    doc.new_page(width=612, height=792)
    doc.new_page(width=612, height=792)
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


@pytest.fixture
def annotated_pdf(tmp_path: Path) -> Path:
    """Create a 2-page PDF with 3 AI annotations via PDFAnnotator."""
    pdf_path = tmp_path / "annotated.pdf"
    doc = fitz.open()
    doc.new_page(width=612, height=792)
    doc.new_page(width=612, height=792)
    doc.save(str(pdf_path))
    doc.close()

    annotations = [
        PDFAnnotation(
            page_index=0,
            bbox=BBox(x0=50, y0=700, x1=200, y1=750),
            kind=AnnotationType.TEXT,
            color=AnnotationColor.GREEN,
            comment="Correct approach",
            source=AnnotationSource.AI,
            grader_name="AI Grader",
            is_verdict=False,
        ),
        PDFAnnotation(
            page_index=0,
            bbox=BBox(x0=50, y0=500, x1=200, y1=550),
            kind=AnnotationType.TEXT,
            color=AnnotationColor.RED,
            comment="Sign error in step 3",
            source=AnnotationSource.AI,
            grader_name="AI Grader",
            is_verdict=False,
        ),
        PDFAnnotation(
            page_index=1,
            bbox=BBox(x0=50, y0=600, x1=300, y1=650),
            kind=AnnotationType.TEXT,
            color=AnnotationColor.AMBER,
            comment="Task 2: 5/10",
            source=AnnotationSource.AI,
            grader_name="AI Grader",
            is_verdict=True,
        ),
    ]

    with PDFAnnotator(pdf_path) as annotator:
        for ann in annotations:
            annotator.add_annotation(ann)
        annotator.save()

    return pdf_path


# ---------------------------------------------------------------------------
# TestListAnnotations
# ---------------------------------------------------------------------------


class TestListAnnotations:
    def test_list_returns_all_annotations_grouped_by_page(
        self, annotated_pdf: Path
    ) -> None:
        from aems_agent.annotation_crud import list_annotations

        result = list_annotations(annotated_pdf)
        assert result["success"] is True
        annotations = result["annotations"]
        # Page 0 has 2 annotations, page 1 has 1
        assert len(annotations["0"]) == 2
        assert len(annotations["1"]) == 1

    def test_list_empty_pdf_returns_empty_dict(self, empty_pdf: Path) -> None:
        from aems_agent.annotation_crud import list_annotations

        result = list_annotations(empty_pdf)
        assert result["success"] is True
        # Empty PDF should have no annotations at all
        assert result["annotations"] == {}

    def test_list_serialization_includes_required_fields(
        self, annotated_pdf: Path
    ) -> None:
        from aems_agent.annotation_crud import list_annotations

        result = list_annotations(annotated_pdf)
        annotations = result["annotations"]
        first_ann = annotations["0"][0]

        # Check all required response fields are present
        required_fields = [
            "xref",
            "stable_id",
            "id",
            "source",
            "original_source",
            "is_verdict",
            "can_revert_to_ai",
            "type",
            "rect",
            "color",
            "content",
            "page_index",
            "grader_name",
        ]
        for field in required_fields:
            assert field in first_ann, f"Missing field: {field}"

    def test_list_annotation_source_is_ai(self, annotated_pdf: Path) -> None:
        from aems_agent.annotation_crud import list_annotations

        result = list_annotations(annotated_pdf)
        for page_anns in result["annotations"].values():
            for ann in page_anns:
                assert ann["source"] == "AI"

    def test_list_verdict_flag_preserved(self, annotated_pdf: Path) -> None:
        from aems_agent.annotation_crud import list_annotations

        result = list_annotations(annotated_pdf)
        # Page 1 annotation is the verdict
        page1_ann = result["annotations"]["1"][0]
        assert page1_ann["is_verdict"] is True
        # Page 0 annotations are not verdicts
        for ann in result["annotations"]["0"]:
            assert ann["is_verdict"] is False


# ---------------------------------------------------------------------------
# TestGetVersion
# ---------------------------------------------------------------------------


class TestGetVersion:
    def test_version_returns_mtime_string(self, annotated_pdf: Path) -> None:
        from aems_agent.annotation_crud import get_version

        result = get_version(annotated_pdf)
        assert result["success"] is True
        assert isinstance(result["version"], str)
        # Should contain mtime_ns (a large integer as string)
        assert len(result["version"]) > 0

    def test_version_changes_after_modification(self, annotated_pdf: Path) -> None:
        from aems_agent.annotation_crud import get_version, add_annotation

        version_before = get_version(annotated_pdf)["version"]

        # Add an annotation to change the file
        add_annotation(
            annotated_pdf,
            {
                "page_index": 0,
                "content": "New note",
                "color": "amber",
            },
        )

        version_after = get_version(annotated_pdf)["version"]
        assert version_before != version_after


# ---------------------------------------------------------------------------
# TestAddAnnotation
# ---------------------------------------------------------------------------


class TestAddAnnotation:
    def test_add_text_annotation(self, empty_pdf: Path) -> None:
        from aems_agent.annotation_crud import add_annotation

        result = add_annotation(
            empty_pdf,
            {
                "page_index": 0,
                "content": "Good work!",
                "color": "green",
                "type": "text",
                "rect": [50.0, 700.0, 200.0, 750.0],
            },
        )
        assert result["success"] is True
        ann = result["annotation"]
        assert ann["content"] == "Good work!"
        assert ann["color"] == "green"
        assert ann["stable_id"] is not None

    def test_add_highlight_annotation(self, empty_pdf: Path) -> None:
        from aems_agent.annotation_crud import add_annotation

        result = add_annotation(
            empty_pdf,
            {
                "page_index": 0,
                "content": "Highlighted",
                "color": "amber",
                "type": "highlight",
                "rect": [50.0, 700.0, 200.0, 720.0],
            },
        )
        assert result["success"] is True
        ann = result["annotation"]
        assert ann is not None

    def test_add_drawing_annotation(self, empty_pdf: Path) -> None:
        from aems_agent.annotation_crud import add_annotation

        result = add_annotation(
            empty_pdf,
            {
                "page_index": 0,
                "content": "",
                "type": "drawing",
                "color": "red",
                "rect": [50.0, 600.0, 200.0, 700.0],
                "drawing_style": "pen",
                "points": [[60.0, 650.0], [100.0, 660.0], [150.0, 640.0]],
                "stroke_width": 2.0,
                "stroke_opacity": 0.8,
                "stroke_color_rgb": [255, 0, 0],
            },
        )
        assert result["success"] is True

    def test_add_returns_annotation_with_stable_id(self, empty_pdf: Path) -> None:
        from aems_agent.annotation_crud import add_annotation

        result = add_annotation(
            empty_pdf,
            {
                "page_index": 0,
                "content": "Test note",
            },
        )
        assert result["success"] is True
        ann = result["annotation"]
        assert ann["stable_id"] is not None
        assert len(ann["stable_id"]) > 0

    def test_add_round_trips_pdf_rect_without_double_conversion(
        self, empty_pdf: Path
    ) -> None:
        """Verify that the rect sent in PDF coordinates comes back in PDF coordinates.

        Use a highlight annotation because text (sticky note) annotations are
        stored at a point, not a rect, so the exact rect round-trip does not
        apply to them.
        """
        from aems_agent.annotation_crud import add_annotation

        input_rect = [50.0, 700.0, 200.0, 720.0]
        result = add_annotation(
            empty_pdf,
            {
                "page_index": 0,
                "content": "Rect test",
                "type": "highlight",
                "rect": input_rect,
            },
        )
        assert result["success"] is True
        ann = result["annotation"]
        returned_rect = ann["rect"]
        assert returned_rect is not None
        # Highlight rect should round-trip in PDF coordinates.
        # y values should stay in the 700+ range (near top in PDF space).
        assert returned_rect[1] > 600  # y0 should be near top in PDF coords
        # x0 should be approximately the same
        assert abs(returned_rect[0] - 50.0) < 5.0

    def test_add_missing_page_index_raises(self, empty_pdf: Path) -> None:
        from aems_agent.annotation_crud import add_annotation

        with pytest.raises(ValueError, match="page_index"):
            add_annotation(empty_pdf, {"content": "No page"})

    def test_add_invalid_page_index_raises(self, empty_pdf: Path) -> None:
        from aems_agent.annotation_crud import add_annotation

        with pytest.raises(ValueError, match="page_index"):
            add_annotation(empty_pdf, {"page_index": 99, "content": "Bad page"})

    def test_add_content_too_long_raises(self, empty_pdf: Path) -> None:
        from aems_agent.annotation_crud import add_annotation

        with pytest.raises(ValueError, match="content"):
            add_annotation(
                empty_pdf,
                {
                    "page_index": 0,
                    "content": "x" * 10241,
                },
            )

    def test_add_invalid_color_raises(self, empty_pdf: Path) -> None:
        from aems_agent.annotation_crud import add_annotation

        with pytest.raises(ValueError, match="color"):
            add_annotation(
                empty_pdf,
                {
                    "page_index": 0,
                    "content": "Bad color",
                    "color": "purple",
                },
            )

    def test_add_generates_default_rect_when_not_provided(
        self, empty_pdf: Path
    ) -> None:
        from aems_agent.annotation_crud import add_annotation

        result = add_annotation(
            empty_pdf,
            {
                "page_index": 0,
                "content": "Default rect",
            },
        )
        assert result["success"] is True
        ann = result["annotation"]
        assert ann["rect"] is not None
        # Should have 4 coordinates
        assert len(ann["rect"]) == 4


# ---------------------------------------------------------------------------
# TestResolveAnnotationIdentifier
# ---------------------------------------------------------------------------


class TestResolveAnnotationIdentifier:
    def test_uuid_string(self) -> None:
        from aems_agent.annotation_crud import resolve_annotation_identifier

        xref, stable_id = resolve_annotation_identifier(
            "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        )
        assert xref is None
        assert stable_id == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_xref_integer_string(self) -> None:
        from aems_agent.annotation_crud import resolve_annotation_identifier

        xref, stable_id = resolve_annotation_identifier("42")
        assert xref == 42
        assert stable_id is None

    def test_composite_format(self) -> None:
        from aems_agent.annotation_crud import resolve_annotation_identifier

        xref, stable_id = resolve_annotation_identifier(
            "xref:42|id:a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        )
        assert xref == 42
        assert stable_id == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

    def test_empty_string(self) -> None:
        from aems_agent.annotation_crud import resolve_annotation_identifier

        xref, stable_id = resolve_annotation_identifier("")
        assert xref is None
        assert stable_id is None

    def test_too_long_identifier(self) -> None:
        from aems_agent.annotation_crud import resolve_annotation_identifier

        xref, stable_id = resolve_annotation_identifier("a" * 501)
        assert xref is None
        assert stable_id is None

    def test_invalid_characters(self) -> None:
        from aems_agent.annotation_crud import resolve_annotation_identifier

        xref, stable_id = resolve_annotation_identifier("id with spaces!")
        assert xref is None
        assert stable_id is None

    def test_legacy_note_format(self) -> None:
        from aems_agent.annotation_crud import resolve_annotation_identifier

        xref, stable_id = resolve_annotation_identifier("Note:123")
        assert xref is None
        assert stable_id == "123"

    def test_annotation_type_name_filtered(self) -> None:
        """Annotation type names like 'Text' should not be used as stable_id."""
        from aems_agent.annotation_crud import resolve_annotation_identifier

        xref, stable_id = resolve_annotation_identifier("xref:42|Text")
        assert xref == 42
        assert stable_id is None


# ---------------------------------------------------------------------------
# TestUpdateAnnotation
# ---------------------------------------------------------------------------


class TestUpdateAnnotation:
    def _get_first_annotation_id(self, pdf_path: Path) -> str:
        """Helper: get the id of the first annotation on page 0."""
        from aems_agent.annotation_crud import list_annotations

        result = list_annotations(pdf_path)
        return result["annotations"]["0"][0]["id"]

    def _get_first_annotation_stable_id(self, pdf_path: Path) -> str:
        """Helper: get the stable_id of the first annotation on page 0."""
        from aems_agent.annotation_crud import list_annotations

        result = list_annotations(pdf_path)
        return result["annotations"]["0"][0]["stable_id"]

    def test_update_content(self, annotated_pdf: Path) -> None:
        from aems_agent.annotation_crud import update_annotation

        ann_id = self._get_first_annotation_id(annotated_pdf)
        result = update_annotation(
            annotated_pdf,
            ann_id,
            {"content": "Updated content"},
        )
        assert result["success"] is True
        assert result["annotation"]["content"] == "Updated content"

    def test_update_color(self, annotated_pdf: Path) -> None:
        from aems_agent.annotation_crud import update_annotation

        ann_id = self._get_first_annotation_id(annotated_pdf)
        result = update_annotation(
            annotated_pdf,
            ann_id,
            {"color": "red"},
        )
        assert result["success"] is True
        assert result["annotation"]["color"] == "red"

    def test_update_rect_position(self, annotated_pdf: Path) -> None:
        from aems_agent.annotation_crud import update_annotation

        ann_id = self._get_first_annotation_id(annotated_pdf)
        new_rect = [100.0, 600.0, 250.0, 650.0]
        result = update_annotation(
            annotated_pdf,
            ann_id,
            {"rect": new_rect},
        )
        assert result["success"] is True
        returned_rect = result["annotation"]["rect"]
        assert returned_rect is not None

    def test_update_round_trips_pdf_rect_without_double_conversion(
        self, annotated_pdf: Path
    ) -> None:
        """Rect sent in PDF coords should come back in PDF coords."""
        from aems_agent.annotation_crud import update_annotation

        ann_id = self._get_first_annotation_id(annotated_pdf)
        new_rect = [100.0, 600.0, 250.0, 650.0]
        result = update_annotation(
            annotated_pdf,
            ann_id,
            {"rect": new_rect},
        )
        assert result["success"] is True
        returned_rect = result["annotation"]["rect"]
        # The y-values should still be in the 600-650 range (PDF space, near top)
        assert returned_rect[1] > 500

    def test_update_cross_page_move(self, annotated_pdf: Path) -> None:
        from aems_agent.annotation_crud import update_annotation, list_annotations

        ann_id = self._get_first_annotation_id(annotated_pdf)
        # Move from page 0 to page 1
        result = update_annotation(
            annotated_pdf,
            ann_id,
            {"page_index": 1},
        )
        assert result["success"] is True
        # Verify page 0 now has 1 annotation and page 1 has 2
        list_result = list_annotations(annotated_pdf)
        assert len(list_result["annotations"].get("0", [])) == 1
        assert len(list_result["annotations"]["1"]) == 2

    def test_update_auto_ownership_transfer(self, annotated_pdf: Path) -> None:
        """Modifying an AI annotation should auto-transfer to HUMAN."""
        from aems_agent.annotation_crud import update_annotation

        ann_id = self._get_first_annotation_id(annotated_pdf)
        result = update_annotation(
            annotated_pdf,
            ann_id,
            {"content": "Human edited this"},
        )
        assert result["success"] is True
        assert result["annotation"]["source"] == "HUMAN"

    def test_update_nonexistent_annotation_raises(
        self, annotated_pdf: Path
    ) -> None:
        from aems_agent.annotation_crud import update_annotation

        with pytest.raises(ValueError, match="not found"):
            update_annotation(
                annotated_pdf,
                "nonexistent-id-12345",
                {"content": "Will fail"},
            )

    def test_update_no_fields_raises(self, annotated_pdf: Path) -> None:
        from aems_agent.annotation_crud import update_annotation

        ann_id = self._get_first_annotation_id(annotated_pdf)
        with pytest.raises(ValueError, match="at least one"):
            update_annotation(annotated_pdf, ann_id, {})

    def test_update_supports_uuid_xref_and_composite_identifiers(
        self, annotated_pdf: Path
    ) -> None:
        from aems_agent.annotation_crud import update_annotation, list_annotations

        result = list_annotations(annotated_pdf)
        ann = result["annotations"]["0"][0]

        # Test with composite id (the default `id` field)
        composite_id = ann["id"]
        update_result = update_annotation(
            annotated_pdf,
            composite_id,
            {"content": "Updated via composite"},
        )
        assert update_result["success"] is True

        # Test with stable_id (UUID)
        result2 = list_annotations(annotated_pdf)
        ann2 = result2["annotations"]["0"][0]
        stable_id = ann2["stable_id"]
        if stable_id:
            update_result2 = update_annotation(
                annotated_pdf,
                stable_id,
                {"content": "Updated via stable_id"},
            )
            assert update_result2["success"] is True

        # Test with xref (integer string)
        result3 = list_annotations(annotated_pdf)
        ann3 = result3["annotations"]["0"][0]
        xref = ann3.get("xref")
        if xref is not None:
            update_result3 = update_annotation(
                annotated_pdf,
                str(xref),
                {"content": "Updated via xref"},
            )
            assert update_result3["success"] is True


# ---------------------------------------------------------------------------
# TestDeleteAnnotation
# ---------------------------------------------------------------------------


class TestDeleteAnnotation:
    def test_delete_existing_annotation(self, annotated_pdf: Path) -> None:
        from aems_agent.annotation_crud import delete_annotation, list_annotations

        result = list_annotations(annotated_pdf)
        ann_id = result["annotations"]["0"][0]["id"]

        delete_result = delete_annotation(annotated_pdf, ann_id)
        assert delete_result["success"] is True
        assert "deleted" in delete_result["message"].lower()

    def test_delete_nonexistent_annotation_raises(
        self, annotated_pdf: Path
    ) -> None:
        from aems_agent.annotation_crud import delete_annotation

        with pytest.raises(ValueError, match="not found"):
            delete_annotation(annotated_pdf, "nonexistent-id-12345")

    def test_delete_reduces_annotation_count(self, annotated_pdf: Path) -> None:
        from aems_agent.annotation_crud import delete_annotation, list_annotations

        result_before = list_annotations(annotated_pdf)
        total_before = sum(
            len(anns) for anns in result_before["annotations"].values()
        )
        assert total_before == 3

        ann_id = result_before["annotations"]["0"][0]["id"]
        delete_annotation(annotated_pdf, ann_id)

        result_after = list_annotations(annotated_pdf)
        total_after = sum(
            len(anns) for anns in result_after["annotations"].values()
        )
        assert total_after == 2
