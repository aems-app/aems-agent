"""Tests for POST /annotate/{aid}/{sid} endpoint."""

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest


def _skip_if_no_fastapi() -> None:
    if importlib.util.find_spec("fastapi") is None or importlib.util.find_spec("httpx") is None:
        pytest.skip("fastapi/httpx not installed")


def _create_submission_and_results(
    storage: Path, aid: str, sid: str, contract_version: int = 1
) -> None:
    """Helper to set up submission PDF and results JSON for testing."""
    from aems_pdf_annotator._fitz import fitz

    # Create submission PDF
    sub_dir = storage / aid / sid
    sub_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = sub_dir / "submission.pdf"
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "Student answer text", fontsize=12)
    doc.save(str(pdf_path))
    doc.close()

    # Create results JSON
    data_dir = storage / "_data" / aid / "results"
    data_dir.mkdir(parents=True, exist_ok=True)
    results = {
        "annotation_contract_version": contract_version,
        "coordinate_space": "visual_top_left_normalized_v1",
        "grader_name": "AI Grader (AEMS)",
        "feedback_items": [
            {
                "check_id": "Q1-01",
                "page": 1,
                "x_normalized": 0.1,
                "y_normalized": 0.1,
                "comment": "Correct approach",
                "priority": "low",
                "verdict": "PASS",
                "is_verdict": False,
            },
            {
                "check_id": "Q1-02",
                "page": 1,
                "x_normalized": 0.5,
                "y_normalized": 0.5,
                "comment": "Sign error in step 3",
                "priority": "high",
                "verdict": "FAIL",
                "is_verdict": False,
            },
            {
                "check_id": "Q1_SUMMARY",
                "page": 1,
                "x_normalized": 0.1,
                "y_normalized": 0.9,
                "comment": "Task 1: 7/10",
                "priority": "low",
                "verdict": "PASS",
                "is_verdict": True,
            },
        ],
    }
    (data_dir / f"{sid}.json").write_text(json.dumps(results))


class TestAnnotateEndpoint:
    """Tests for POST /annotate/{aid}/{sid}."""

    def test_annotate_creates_pdf(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        _skip_if_no_fastapi()
        _create_submission_and_results(tmp_storage_path, "assign-1", "sub-1")
        resp = agent_client.post("/annotate/assign-1/sub-1", headers=auth_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["annotation_count"] == 3
        assert data["contract_version"] == 1
        assert data["existing"] is False
        # Verify file exists
        annotated = tmp_storage_path / "assign-1" / "sub-1" / "submission_annotated.pdf"
        assert annotated.exists()

    def test_annotate_idempotent(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        _skip_if_no_fastapi()
        _create_submission_and_results(tmp_storage_path, "assign-1", "sub-1")
        resp1 = agent_client.post("/annotate/assign-1/sub-1", headers=auth_headers)
        assert resp1.status_code == 200

        resp2 = agent_client.post("/annotate/assign-1/sub-1", headers=auth_headers)
        assert resp2.status_code == 200
        assert resp2.json()["existing"] is True

    def test_annotate_force_regenerate(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        _skip_if_no_fastapi()
        _create_submission_and_results(tmp_storage_path, "assign-1", "sub-1")
        resp1 = agent_client.post("/annotate/assign-1/sub-1", headers=auth_headers)
        assert resp1.status_code == 200

        resp2 = agent_client.post("/annotate/assign-1/sub-1?force=true", headers=auth_headers)
        assert resp2.status_code == 200
        assert resp2.json()["existing"] is False

    def test_annotate_missing_results(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.post("/annotate/assign-1/nonexistent", headers=auth_headers)
        assert resp.status_code == 404

    def test_annotate_missing_pdf(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        _skip_if_no_fastapi()
        # Create results but no PDF
        data_dir = tmp_storage_path / "_data" / "a1" / "results"
        data_dir.mkdir(parents=True)
        results = {
            "annotation_contract_version": 1,
            "coordinate_space": "visual_top_left_normalized_v1",
            "feedback_items": [
                {
                    "page": 1,
                    "x_normalized": 0.1,
                    "y_normalized": 0.2,
                    "comment": "test",
                    "priority": "low",
                },
            ],
        }
        (data_dir / "s1.json").write_text(json.dumps(results))

        resp = agent_client.post("/annotate/a1/s1", headers=auth_headers)
        assert resp.status_code == 404

    def test_annotate_wrong_contract_version(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        _skip_if_no_fastapi()
        _create_submission_and_results(tmp_storage_path, "assign-1", "sub-1", contract_version=99)
        resp = agent_client.post("/annotate/assign-1/sub-1", headers=auth_headers)
        assert resp.status_code == 422

    def test_annotate_missing_feedback_items_returns_422(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        _skip_if_no_fastapi()
        sub_dir = tmp_storage_path / "assign-1" / "sub-1"
        sub_dir.mkdir(parents=True, exist_ok=True)

        from aems_pdf_annotator._fitz import fitz

        pdf_path = sub_dir / "submission.pdf"
        doc = fitz.open()
        doc.new_page(width=612, height=792)
        doc.save(str(pdf_path))
        doc.close()

        data_dir = tmp_storage_path / "_data" / "assign-1" / "results"
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "sub-1.json").write_text(
            json.dumps(
                {
                    "annotation_contract_version": 1,
                    "coordinate_space": "visual_top_left_normalized_v1",
                }
            )
        )

        resp = agent_client.post("/annotate/assign-1/sub-1", headers=auth_headers)
        assert resp.status_code == 422

    def test_annotate_invalid_feedback_item_returns_422(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        _skip_if_no_fastapi()
        _create_submission_and_results(tmp_storage_path, "assign-1", "sub-1")

        data_dir = tmp_storage_path / "_data" / "assign-1" / "results"
        (data_dir / "sub-1.json").write_text(
            json.dumps(
                {
                    "annotation_contract_version": 1,
                    "coordinate_space": "visual_top_left_normalized_v1",
                    "feedback_items": [
                        {
                            "page": "not-an-int",
                            "x_normalized": 0.1,
                            "y_normalized": 0.2,
                            "comment": "Broken item",
                        }
                    ],
                }
            )
        )

        resp = agent_client.post("/annotate/assign-1/sub-1", headers=auth_headers)
        assert resp.status_code == 422

    def test_annotate_prefers_rendered_annotations_when_present(
        self, agent_client: Any, auth_headers: dict, tmp_storage_path: Path
    ) -> None:
        _skip_if_no_fastapi()
        _create_submission_and_results(tmp_storage_path, "assign-1", "sub-1")

        data_dir = tmp_storage_path / "_data" / "assign-1" / "results"
        (data_dir / "sub-1.json").write_text(
            json.dumps(
                {
                    "annotation_contract_version": 1,
                    "coordinate_space": "visual_top_left_normalized_v1",
                    "feedback_items": [
                        {
                            "page": 1,
                            "x_normalized": 0.9,
                            "y_normalized": 0.9,
                            "comment": "Fallback placement",
                            "priority": "low",
                            "is_verdict": True,
                        }
                    ],
                    "rendered_annotations": [
                        {
                            "id": "ann-1",
                            "page_index": 0,
                            "bbox": {"x0": 49.2, "y0": 67.2, "x1": 73.2, "y1": 91.2},
                            "kind": "text",
                            "color": "green",
                            "comment": "Exact placement",
                            "source": "AI",
                            "original_source": "AI",
                        }
                    ],
                }
            )
        )

        resp = agent_client.post("/annotate/assign-1/sub-1", headers=auth_headers)
        assert resp.status_code == 200

        from aems_pdf_annotator._fitz import fitz

        annotated = tmp_storage_path / "assign-1" / "sub-1" / "submission_annotated.pdf"
        doc = fitz.open(str(annotated))
        page = doc[0]
        annots = list(page.annots())
        assert len(annots) == 1
        assert annots[0].rect.y0 < 120
        doc.close()

    def test_annotate_requires_auth(self, agent_client: Any, tmp_storage_path: Path) -> None:
        _skip_if_no_fastapi()
        resp = agent_client.post("/annotate/assign-1/sub-1")
        assert resp.status_code in (401, 403)

    def test_annotate_invalid_path_component(self, agent_client: Any, auth_headers: dict) -> None:
        _skip_if_no_fastapi()
        # Dots are rejected by _validate_path_segment
        resp = agent_client.post("/annotate/assign.evil/sub-1", headers=auth_headers)
        assert resp.status_code == 400
