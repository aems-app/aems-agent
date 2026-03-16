"""Tests for grading bundle generation and caching."""

import json
import base64
from pathlib import Path

import fitz  # PyMuPDF
import pytest

from aems_agent.grading_bundle import generate_bundle, get_cache_key, get_cache_path


@pytest.fixture
def sample_pdf(tmp_path: Path) -> Path:
    """Create a simple 2-page PDF for testing."""
    doc = fitz.open()
    page1 = doc.new_page(width=612, height=792)
    page1.insert_text((72, 100), "This is page 1 with typed text content.", fontsize=12)
    page2 = doc.new_page(width=612, height=792)
    page2.insert_text((72, 100), "X", fontsize=8)
    pdf_path = tmp_path / "submission.pdf"
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


@pytest.fixture
def empty_pdf(tmp_path: Path) -> Path:
    """Create an empty single-page PDF."""
    doc = fitz.open()
    doc.new_page(width=612, height=792)
    pdf_path = tmp_path / "empty.pdf"
    doc.save(str(pdf_path))
    doc.close()
    return pdf_path


class TestGenerateBundle:
    def test_returns_valid_bundle_structure(self, sample_pdf: Path) -> None:
        bundle = generate_bundle(sample_pdf, strategy="text_only", dpi=150)
        assert bundle["bundle_version"] == 1
        assert bundle["strategy"] == "text_only"
        assert len(bundle["pages"]) == 2
        assert "metadata" in bundle

    def test_text_extraction(self, sample_pdf: Path) -> None:
        bundle = generate_bundle(sample_pdf, strategy="text_only", dpi=150)
        assert "page 1 with typed text" in bundle["pages"][0]["text"]

    def test_page_dimensions(self, sample_pdf: Path) -> None:
        bundle = generate_bundle(sample_pdf, strategy="text_only", dpi=150)
        page = bundle["pages"][0]
        assert page["width"] == 612
        assert page["height"] == 792

    def test_text_only_no_images(self, sample_pdf: Path) -> None:
        bundle = generate_bundle(sample_pdf, strategy="text_only", dpi=150)
        for page in bundle["pages"]:
            assert "image_base64" not in page

    def test_multimodal_all_images(self, sample_pdf: Path) -> None:
        bundle = generate_bundle(sample_pdf, strategy="multimodal", dpi=72)
        for page in bundle["pages"]:
            assert "image_base64" in page
            assert len(page["image_base64"]) > 0

    def test_multimodal_images_are_webp(self, sample_pdf: Path) -> None:
        bundle = generate_bundle(sample_pdf, strategy="multimodal", dpi=72)
        first_image = base64.b64decode(bundle["pages"][0]["image_base64"])
        assert first_image.startswith(b"RIFF")
        assert first_image[8:12] == b"WEBP"

    def test_smart_strategy_selective_images(self, sample_pdf: Path) -> None:
        bundle = generate_bundle(sample_pdf, strategy="smart", dpi=72)
        assert bundle["metadata"]["page_count"] == 2

    def test_max_pages_limit(self, sample_pdf: Path) -> None:
        bundle = generate_bundle(sample_pdf, strategy="text_only", dpi=150, max_pages=1)
        assert len(bundle["pages"]) == 1

    def test_metadata_fields(self, sample_pdf: Path) -> None:
        bundle = generate_bundle(sample_pdf, strategy="text_only", dpi=150)
        meta = bundle["metadata"]
        assert "page_count" in meta
        assert "has_handwriting" in meta
        assert "avg_ocr_quality" in meta
        assert "bundle_size_bytes" in meta
        assert "generated_at" in meta

    def test_missing_pdf_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            generate_bundle(tmp_path / "nonexistent.pdf", strategy="text_only", dpi=150)


class TestBundleCaching:
    def test_cache_key_includes_strategy_and_dpi(self, sample_pdf: Path) -> None:
        key1 = get_cache_key(sample_pdf, strategy="text_only", dpi=150, max_pages=None)
        key2 = get_cache_key(sample_pdf, strategy="multimodal", dpi=150, max_pages=None)
        assert key1 != key2

    def test_cache_key_changes_on_pdf_modification(self, sample_pdf: Path) -> None:
        key1 = get_cache_key(sample_pdf, strategy="text_only", dpi=150, max_pages=None)
        sample_pdf.write_bytes(sample_pdf.read_bytes() + b"\0")
        key2 = get_cache_key(sample_pdf, strategy="text_only", dpi=150, max_pages=None)
        assert key1 != key2

    def test_cached_bundle_returned_on_second_call(self, sample_pdf: Path, tmp_path: Path) -> None:
        cache_dir = tmp_path / "_cache"
        bundle1 = generate_bundle(
            sample_pdf, strategy="text_only", dpi=150, cache_dir=cache_dir
        )
        bundle2 = generate_bundle(
            sample_pdf, strategy="text_only", dpi=150, cache_dir=cache_dir
        )
        assert bundle1 == bundle2

    def test_force_refresh_bypasses_cache(self, sample_pdf: Path, tmp_path: Path) -> None:
        cache_dir = tmp_path / "_cache"
        generate_bundle(sample_pdf, strategy="text_only", dpi=150, cache_dir=cache_dir)
        bundle = generate_bundle(
            sample_pdf, strategy="text_only", dpi=150,
            cache_dir=cache_dir, force_refresh=True,
        )
        assert bundle["bundle_version"] == 1
