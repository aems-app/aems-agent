"""Tests for Canvas download logic: manifest validation and idempotent downloads."""

import hashlib
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest


def _make_manifest(**overrides: Any) -> dict[str, Any]:
    """Create a valid manifest dict with optional overrides."""
    manifest: dict[str, Any] = {
        "canvas_base_url": "https://university.instructure.com",
        "canvas_token": "test_token_123",
        "assignment_id": 100,
        "manifest_version": 1,
        "expires_at": (time.time() + 300),  # 5 min from now
        "nonce": "unique-nonce-1",
        "audience_key_id": "test_key_id",
        "submissions": [
            {"submission_id": 1001, "file_id": 569, "download_url": "/files/569/download"}
        ],
    }
    manifest.update(overrides)
    return manifest


# ---------------------------------------------------------------------------
# Manifest Validation Tests
# ---------------------------------------------------------------------------


class TestValidateManifest:
    """Tests for validate_manifest()."""

    def test_validate_manifest_valid(self) -> None:
        from aems_agent.canvas_download import validate_manifest

        m = _make_manifest()
        result = validate_manifest(
            m,
            allowed_hosts=["university.instructure.com"],
            agent_key_id="test_key_id",
        )
        assert result is True

    def test_validate_manifest_expired(self) -> None:
        from aems_agent.canvas_download import ManifestValidationError, validate_manifest

        m = _make_manifest(expires_at=time.time() - 10)
        with pytest.raises(ManifestValidationError, match="expired"):
            validate_manifest(
                m,
                allowed_hosts=["university.instructure.com"],
                agent_key_id="test_key_id",
            )

    def test_validate_manifest_wrong_host(self) -> None:
        from aems_agent.canvas_download import ManifestValidationError, validate_manifest

        m = _make_manifest(canvas_base_url="https://evil.com")
        with pytest.raises(ManifestValidationError, match="not in allowlist"):
            validate_manifest(
                m,
                allowed_hosts=["university.instructure.com"],
                agent_key_id="test_key_id",
            )

    def test_validate_manifest_http_rejected(self) -> None:
        from aems_agent.canvas_download import ManifestValidationError, validate_manifest

        m = _make_manifest(canvas_base_url="http://university.instructure.com")
        with pytest.raises(ManifestValidationError, match="HTTPS"):
            validate_manifest(
                m,
                allowed_hosts=["university.instructure.com"],
                agent_key_id="test_key_id",
            )

    def test_validate_manifest_wrong_audience(self) -> None:
        from aems_agent.canvas_download import ManifestValidationError, validate_manifest

        m = _make_manifest(audience_key_id="wrong_key")
        with pytest.raises(ManifestValidationError, match="audience"):
            validate_manifest(
                m,
                allowed_hosts=["university.instructure.com"],
                agent_key_id="test_key_id",
            )

    def test_validate_manifest_instructure_wildcard(self) -> None:
        """*.instructure.com hosts are always allowed."""
        from aems_agent.canvas_download import validate_manifest

        m = _make_manifest(canvas_base_url="https://other-school.instructure.com")
        result = validate_manifest(
            m,
            allowed_hosts=[],  # empty allowlist
            agent_key_id="test_key_id",
        )
        assert result is True

    def test_validate_manifest_non_instructure_not_wildcarded(self) -> None:
        """Non-instructure.com hosts must be in the explicit allowlist."""
        from aems_agent.canvas_download import ManifestValidationError, validate_manifest

        m = _make_manifest(canvas_base_url="https://custom-canvas.example.com")
        with pytest.raises(ManifestValidationError, match="not in allowlist"):
            validate_manifest(
                m,
                allowed_hosts=[],
                agent_key_id="test_key_id",
            )

    def test_validate_manifest_missing_submissions(self) -> None:
        from aems_agent.canvas_download import ManifestValidationError, validate_manifest

        m = _make_manifest(submissions=[])
        with pytest.raises(ManifestValidationError, match="submissions"):
            validate_manifest(
                m,
                allowed_hosts=["university.instructure.com"],
                agent_key_id="test_key_id",
            )


# ---------------------------------------------------------------------------
# Idempotent Download Tests
# ---------------------------------------------------------------------------


class TestDownloadSubmissions:
    """Tests for download_submissions()."""

    @pytest.mark.asyncio
    async def test_download_skips_existing(self, tmp_path: Path) -> None:
        """Files that already exist with matching size are skipped."""
        from aems_agent.canvas_download import download_submissions

        # Pre-create a file
        pdf_dir = tmp_path / "100" / "1001"
        pdf_dir.mkdir(parents=True)
        existing = pdf_dir / "submission.pdf"
        existing.write_bytes(b"%PDF-fake-content")

        manifest = _make_manifest()
        manifest["submissions"][0]["expected_size"] = len(b"%PDF-fake-content")

        results = await download_submissions(
            manifest=manifest,
            storage_path=tmp_path,
            http_client=AsyncMock(),  # should not be called
        )
        assert results[0].status == "skipped"
        assert results[0].sha256 == hashlib.sha256(b"%PDF-fake-content").hexdigest()

    @pytest.mark.asyncio
    async def test_download_reports_failure_on_error(self, tmp_path: Path) -> None:
        """Network errors are reported as failed, not raised."""
        import httpx

        from aems_agent.canvas_download import download_submissions

        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("connection refused")

        manifest = _make_manifest()
        results = await download_submissions(
            manifest=manifest,
            storage_path=tmp_path,
            http_client=mock_client,
        )
        assert results[0].status == "failed"
        assert "connection refused" in results[0].error

    @pytest.mark.asyncio
    async def test_download_success(self, tmp_path: Path) -> None:
        """Successful download writes file and returns 'downloaded' status."""
        from aems_agent.canvas_download import download_submissions

        pdf_content = b"%PDF-1.4 test content for download"
        mock_response = AsyncMock()
        mock_response.content = pdf_content
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        manifest = _make_manifest()
        results = await download_submissions(
            manifest=manifest,
            storage_path=tmp_path,
            http_client=mock_client,
        )
        assert results[0].status == "downloaded"
        assert results[0].sha256 == hashlib.sha256(pdf_content).hexdigest()

        # Verify file was written
        target = tmp_path / "100" / "1001" / "submission.pdf"
        assert target.exists()
        assert target.read_bytes() == pdf_content

    @pytest.mark.asyncio
    async def test_download_rejects_non_pdf(self, tmp_path: Path) -> None:
        """Non-PDF content is rejected."""
        from aems_agent.canvas_download import download_submissions

        mock_response = AsyncMock()
        mock_response.content = b"<html>not a pdf</html>"
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        manifest = _make_manifest()
        results = await download_submissions(
            manifest=manifest,
            storage_path=tmp_path,
            http_client=mock_client,
        )
        assert results[0].status == "failed"
        assert "PDF" in results[0].error

    @pytest.mark.asyncio
    async def test_download_multiple_submissions(self, tmp_path: Path) -> None:
        """Multiple submissions in manifest are all processed."""
        from aems_agent.canvas_download import download_submissions

        pdf_content = b"%PDF-1.4 test"
        mock_response = AsyncMock()
        mock_response.content = pdf_content
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        manifest = _make_manifest(
            submissions=[
                {"submission_id": 1001, "file_id": 569, "download_url": "/files/569/download"},
                {"submission_id": 1002, "file_id": 570, "download_url": "/files/570/download"},
                {"submission_id": 1003, "file_id": 571, "download_url": "/files/571/download"},
            ]
        )
        results = await download_submissions(
            manifest=manifest,
            storage_path=tmp_path,
            http_client=mock_client,
        )
        assert len(results) == 3
        assert all(r.status == "downloaded" for r in results)

    @pytest.mark.asyncio
    async def test_download_skips_existing_no_expected_size(self, tmp_path: Path) -> None:
        """Files that exist are skipped even if expected_size is not provided."""
        from aems_agent.canvas_download import download_submissions

        pdf_dir = tmp_path / "100" / "1001"
        pdf_dir.mkdir(parents=True)
        existing = pdf_dir / "submission.pdf"
        existing.write_bytes(b"%PDF-existing")

        manifest = _make_manifest()
        # No expected_size in submission entry

        results = await download_submissions(
            manifest=manifest,
            storage_path=tmp_path,
            http_client=AsyncMock(),
        )
        assert results[0].status == "skipped"

    @pytest.mark.asyncio
    async def test_download_redownloads_size_mismatch(self, tmp_path: Path) -> None:
        """If expected_size doesn't match, file is re-downloaded."""
        from aems_agent.canvas_download import download_submissions

        pdf_dir = tmp_path / "100" / "1001"
        pdf_dir.mkdir(parents=True)
        existing = pdf_dir / "submission.pdf"
        existing.write_bytes(b"%PDF-old")

        new_content = b"%PDF-1.4 new content is longer"
        mock_response = AsyncMock()
        mock_response.content = new_content
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response

        manifest = _make_manifest()
        manifest["submissions"][0]["expected_size"] = len(new_content)

        results = await download_submissions(
            manifest=manifest,
            storage_path=tmp_path,
            http_client=mock_client,
        )
        assert results[0].status == "downloaded"
        assert (pdf_dir / "submission.pdf").read_bytes() == new_content


# ---------------------------------------------------------------------------
# DownloadJob Tests
# ---------------------------------------------------------------------------


class TestDownloadJob:
    """Tests for the in-memory job store and lifecycle."""

    def test_create_download_job(self) -> None:
        from aems_agent.canvas_download import create_download_job, get_download_job

        manifest = _make_manifest()
        job_id = create_download_job(manifest)
        assert isinstance(job_id, str)
        assert len(job_id) > 0

        job = get_download_job(job_id)
        assert job is not None
        assert job.status == "pending"
        assert job.total_submissions == 1

    def test_get_missing_job(self) -> None:
        from aems_agent.canvas_download import get_download_job

        assert get_download_job("nonexistent-id") is None

    @pytest.mark.asyncio
    async def test_run_download_job(self, tmp_path: Path) -> None:
        from aems_agent.canvas_download import (
            create_download_job,
            get_download_job,
            run_download_job,
        )

        pdf_content = b"%PDF-1.4 test"
        mock_response = AsyncMock()
        mock_response.content = pdf_content
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_response
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        manifest = _make_manifest()
        job_id = create_download_job(manifest)

        await run_download_job(
            job_id=job_id,
            manifest=manifest,
            storage_path=tmp_path,
            http_client=mock_client,
        )

        job = get_download_job(job_id)
        assert job is not None
        assert job.status == "completed"
        assert job.downloaded == 1
        assert len(job.per_submission) == 1

    def test_job_store_max_size(self) -> None:
        """Job store evicts old entries when at max capacity."""
        from aems_agent.canvas_download import _download_jobs, create_download_job

        # Clear existing jobs
        _download_jobs.clear()

        manifest = _make_manifest()
        job_ids = []
        # Create many jobs (more than MAX_JOBS)
        for _ in range(110):
            job_ids.append(create_download_job(manifest))

        # Should not exceed MAX_JOBS
        assert len(_download_jobs) <= 100
