# SPDX-License-Identifier: AGPL-3.0-or-later

"""Local annotation generation for the AEMS agent."""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from aems_pdf_annotator import PDFAnnotator
from aems_pdf_annotator.contract import (
    CURRENT_CONTRACT_VERSION,
    payload_to_annotations,
)

logger = logging.getLogger(__name__)


def generate_annotated_pdf(
    storage_path: Path,
    assignment_id: str,
    submission_id: str,
    force: bool = False,
) -> Dict[str, Any]:
    """Generate an annotated PDF from stored results JSON and submission PDF.

    Args:
        storage_path: Root storage directory.
        assignment_id: Assignment identifier.
        submission_id: Submission identifier.
        force: If True, regenerate even if annotated PDF exists.

    Returns:
        Dict with status, annotation_count, output_path, contract_version, existing.

    Raises:
        FileNotFoundError: If results JSON or submission PDF not found.
        ContractValidationError: If contract version is unsupported.
    """
    # Locate files
    results_path = storage_path / "_data" / assignment_id / "results" / f"{submission_id}.json"
    if not results_path.exists():
        raise FileNotFoundError(f"Results JSON not found: {results_path}")

    pdf_path = storage_path / assignment_id / submission_id / "submission.pdf"
    if not pdf_path.exists():
        raise FileNotFoundError(f"Submission PDF not found: {pdf_path}")

    output_path = storage_path / assignment_id / submission_id / "submission_annotated.pdf"

    # Check idempotency — reuse only if annotated PDF is newer than both inputs.
    # If deletion failed after a source update (e.g. file locked on Windows),
    # the mtime check ensures we regenerate rather than serve stale content.
    if output_path.exists() and not force:
        annotated_mtime = output_path.stat().st_mtime_ns
        source_mtime = pdf_path.stat().st_mtime_ns
        results_mtime = results_path.stat().st_mtime_ns
        if annotated_mtime > source_mtime and annotated_mtime > results_mtime:
            return {
                "status": "ok",
                "annotation_count": None,
                "output_path": str(output_path.relative_to(storage_path)),
                "contract_version": CURRENT_CONTRACT_VERSION,
                "existing": True,
            }

    # Load and validate results
    results = json.loads(results_path.read_text(encoding="utf-8"))
    grader_name: str = results.get("grader_name", "AEMS AI")

    # Get page dimensions from PDF
    from aems_pdf_annotator._fitz import fitz

    page_dimensions: List[Tuple[float, float]] = []
    with fitz.open(str(pdf_path)) as doc:
        for page in doc:
            page_dimensions.append((page.rect.width, page.rect.height))

    annotations = payload_to_annotations(
        results,
        page_dimensions,
        grader_name=grader_name,
    )

    # Apply annotations to PDF
    with PDFAnnotator(pdf_path) as annotator:
        count = annotator.add_annotations(annotations)
        annotator.save(output_path)

    logger.info("Generated annotated PDF: %s (%d annotations)", output_path, count)

    return {
        "status": "ok",
        "annotation_count": count,
        "output_path": str(output_path.relative_to(storage_path)),
        "contract_version": CURRENT_CONTRACT_VERSION,
        "existing": False,
    }
