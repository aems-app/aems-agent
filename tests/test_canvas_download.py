"""Tests for Canvas download logic: manifest validation and idempotent downloads."""

import hashlib
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

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


def _stream_client(
    content: bytes = b"",
    *,
    stream_error: Exception | None = None,
    status_error: Exception | None = None,
) -> AsyncMock:
    """Build a mock httpx client whose ``.stream()`` yields ``content``.

    Mirrors ``httpx.AsyncClient.stream`` (an async context manager exposing
    ``aiter_bytes()``), so tests exercise the streaming/size-cap download path.
    ``.stream`` is a ``MagicMock`` so call args (the request URL) are recorded.
    Pass ``stream_error`` to raise on request, ``status_error`` to raise from
    ``raise_for_status()``.
    """

    class _Resp:
        def raise_for_status(self) -> None:
            if status_error is not None:
                raise status_error

        async def aiter_bytes(self) -> Any:
            # Yield in two chunks so the capped-accumulation loop is exercised.
            if content:
                mid = max(1, len(content) // 2)
                yield content[:mid]
                if content[mid:]:
                    yield content[mid:]

    class _Ctx:
        async def __aenter__(self) -> Any:
            if stream_error is not None:
                raise stream_error
            return _Resp()

        async def __aexit__(self, *exc: Any) -> bool:
            return False

    client = AsyncMock()
    client.stream = MagicMock(side_effect=lambda *a, **k: _Ctx())
    return client


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
        with pytest.raises(ManifestValidationError, match="not allowed") as excinfo:
            validate_manifest(
                m,
                allowed_hosts=["university.instructure.com"],
                agent_key_id="test_key_id",
            )
        assert excinfo.value.code == "canvas_host_not_allowed"
        assert excinfo.value.rejected_host == "evil.com"
        assert excinfo.value.as_detail()["rejected_host"] == "evil.com"

    def test_validate_manifest_matches_equivalent_ipv6_host_spellings(self) -> None:
        from aems_agent.canvas_download import validate_manifest

        manifest = _make_manifest(
            canvas_base_url="https://[2001:0db8:0000:0000:0000:0000:0000:0001]"
        )

        assert validate_manifest(
            manifest,
            allowed_hosts=["2001:db8::1"],
            agent_key_id="test_key_id",
        )

    def test_validate_manifest_reports_missing_https_hostname_structurally(self) -> None:
        from aems_agent.canvas_download import ManifestValidationError, validate_manifest

        manifest = _make_manifest(canvas_base_url="https://")

        with pytest.raises(ManifestValidationError) as excinfo:
            validate_manifest(
                manifest,
                allowed_hosts=["canvas.example.edu"],
                agent_key_id="test_key_id",
            )

        assert excinfo.value.code == "canvas_host_invalid"
        assert excinfo.value.as_detail() == {
            "code": "canvas_host_invalid",
            "message": "Canvas URL must include a valid hostname",
        }

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
        with pytest.raises(ManifestValidationError, match="not allowed"):
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

    def test_validate_manifest_rejects_unsupported_version(self) -> None:
        from aems_agent.canvas_download import ManifestValidationError, validate_manifest

        m = _make_manifest(manifest_version=2)
        with pytest.raises(ManifestValidationError, match="Unsupported manifest version"):
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

        mock_client = _stream_client(stream_error=httpx.ConnectError("connection refused"))

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
        mock_client = _stream_client(pdf_content)

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

        mock_client = _stream_client(b"<html>not a pdf</html>")

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
        mock_client = _stream_client(pdf_content)

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
        mock_client = _stream_client(new_content)

        manifest = _make_manifest()
        manifest["submissions"][0]["expected_size"] = len(new_content)

        results = await download_submissions(
            manifest=manifest,
            storage_path=tmp_path,
            http_client=mock_client,
        )
        assert results[0].status == "downloaded"
        assert (pdf_dir / "submission.pdf").read_bytes() == new_content

    @pytest.mark.asyncio
    async def test_download_reports_progress_per_submission(self, tmp_path: Path) -> None:
        """Each submission result is reported as soon as it finishes."""
        from aems_agent.canvas_download import SubmissionResult, download_submissions

        pdf_content = b"%PDF-1.4 first"
        mock_client = _stream_client(pdf_content)

        manifest = _make_manifest(
            submissions=[
                {"submission_id": 1001, "file_id": 569, "download_url": "/files/569/download"},
                {"submission_id": 1002, "file_id": 570, "download_url": "/files/570/download"},
            ]
        )
        seen: list[SubmissionResult] = []

        def progress_callback(result: SubmissionResult) -> None:
            seen.append(result)

        results = await download_submissions(
            manifest=manifest,
            storage_path=tmp_path,
            http_client=mock_client,
            progress_callback=progress_callback,
        )

        assert [result.submission_id for result in seen] == [1001, 1002]
        assert [result.submission_id for result in results] == [1001, 1002]

    @pytest.mark.asyncio
    async def test_download_cleans_up_temp_file_on_replace_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Partial writes must not leave temp files behind on disk."""
        from aems_agent import canvas_download

        pdf_content = b"%PDF-1.4 temp cleanup"
        mock_client = _stream_client(pdf_content)

        def fail_replace(_src: str, _dst: str) -> None:
            raise OSError("replace failed")

        monkeypatch.setattr(canvas_download.os, "replace", fail_replace)

        manifest = _make_manifest()
        results = await canvas_download.download_submissions(
            manifest=manifest,
            storage_path=tmp_path,
            http_client=mock_client,
        )

        assert results[0].status == "failed"
        assert not list(tmp_path.rglob("*.tmp"))


# ---------------------------------------------------------------------------
# DownloadJob Tests
# ---------------------------------------------------------------------------


class TestDownloadJob:
    """Tests for the in-memory job store and lifecycle."""

    @pytest.mark.parametrize(
        ("metadata", "expected"),
        [
            ({"student_id": 0, "user_id": 42}, 0),
            ({"student_id": None, "user_id": "42"}, 42),
            ({"student_id": "007"}, 7),
            ({"student_id": {"unexpected": "shape"}}, None),
        ],
    )
    def test_student_id_metadata_is_normalized(
        self,
        metadata: dict[str, Any],
        expected: int | None,
    ) -> None:
        from aems_agent.canvas_download import _student_id_from_metadata

        assert _student_id_from_metadata(metadata) == expected

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
        mock_client = _stream_client(pdf_content)

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

    @pytest.mark.asyncio
    async def test_run_download_job_preserves_student_identity_for_preview(
        self, tmp_path: Path
    ) -> None:
        from aems_agent.canvas_download import (
            create_download_job,
            get_download_job,
            run_download_job,
        )

        manifest = _make_manifest(
            submissions=[
                {
                    "submission_id": 1001,
                    "user_id": 42,
                    "student_name": "Fixture Student",
                    "filename": "answer.pdf",
                    "file_id": 569,
                    "download_url": "/files/569/download",
                }
            ]
        )
        job_id = create_download_job(manifest)

        await run_download_job(
            job_id=job_id,
            manifest=manifest,
            storage_path=tmp_path,
            http_client=_stream_client(b"%PDF-1.4 fixture"),
        )

        job = get_download_job(job_id)
        assert job is not None
        assert job.per_submission[0]["student_id"] == 42
        assert job.per_submission[0]["student_name"] == "Fixture Student"
        assert job.per_submission[0]["filename"] == "answer.pdf"

    @pytest.mark.asyncio
    async def test_run_download_job_updates_progress_incrementally(self, tmp_path: Path) -> None:
        from aems_agent.canvas_download import (
            create_download_job,
            get_download_job,
            run_download_job,
        )

        pdf_content = b"%PDF-1.4 progress"

        class SlowClient:
            """Streaming client whose first download is slow, to observe progress."""

            def __init__(self) -> None:
                self.calls = 0

            def stream(self, *args: Any, **kwargs: Any) -> Any:
                self.calls += 1
                slow = self.calls == 1

                class _Ctx:
                    async def __aenter__(self) -> Any:
                        if slow:
                            import asyncio

                            await asyncio.sleep(0.05)

                        class _Resp:
                            def raise_for_status(self) -> None:
                                return None

                            async def aiter_bytes(self) -> Any:
                                yield pdf_content

                        return _Resp()

                    async def __aexit__(self, *exc: Any) -> bool:
                        return False

                return _Ctx()

        manifest = _make_manifest(
            submissions=[
                {"submission_id": 1001, "file_id": 569, "download_url": "/files/569/download"},
                {"submission_id": 1002, "file_id": 570, "download_url": "/files/570/download"},
            ]
        )
        job_id = create_download_job(manifest)

        import asyncio

        task = asyncio.create_task(
            run_download_job(
                job_id=job_id,
                manifest=manifest,
                storage_path=tmp_path,
                http_client=SlowClient(),
            )
        )
        await asyncio.sleep(0.01)

        job = get_download_job(job_id)
        assert job is not None
        assert job.status == "running"
        assert job.downloaded == 0

        await asyncio.sleep(0.08)
        job = get_download_job(job_id)
        assert job is not None
        assert job.downloaded >= 1
        assert len(job.per_submission) >= 1

        await task

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


# ---------------------------------------------------------------------------
# Download URL pinning + size cap (v0.4.18)
# ---------------------------------------------------------------------------


class TestDownloadUrlPinning:
    """download_url must never route the Canvas token off the validated host."""

    @pytest.mark.asyncio
    async def test_full_url_download_url_marked_failed(self, tmp_path: Path) -> None:
        """An absolute URL smuggled into download_url must not be fetched."""
        from aems_agent.canvas_download import download_submissions

        manifest = _make_manifest()
        manifest["submissions"][0]["download_url"] = "https://evil.example.com/exfil"

        mock_client = AsyncMock()
        results = await download_submissions(
            manifest=manifest,
            storage_path=tmp_path,
            http_client=mock_client,
        )

        assert results[0].status == "failed"
        assert "download_url" in results[0].error
        mock_client.stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_userinfo_trick_download_url_marked_failed(self, tmp_path: Path) -> None:
        """`@evil.com/...` would turn the Canvas host into URL userinfo."""
        from aems_agent.canvas_download import download_submissions

        manifest = _make_manifest()
        manifest["submissions"][0]["download_url"] = "@evil.example.com/exfil"

        mock_client = AsyncMock()
        results = await download_submissions(
            manifest=manifest,
            storage_path=tmp_path,
            http_client=mock_client,
        )

        assert results[0].status == "failed"
        mock_client.stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_download_url_marked_failed(self, tmp_path: Path) -> None:
        from aems_agent.canvas_download import download_submissions

        manifest = _make_manifest()
        del manifest["submissions"][0]["download_url"]

        mock_client = AsyncMock()
        results = await download_submissions(
            manifest=manifest,
            storage_path=tmp_path,
            http_client=mock_client,
        )

        assert results[0].status == "failed"
        mock_client.stream.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_path_download_url_still_fetched(self, tmp_path: Path) -> None:
        from aems_agent.canvas_download import download_submissions

        pdf_content = b"%PDF-1.4 pinned host ok"
        mock_client = _stream_client(pdf_content)

        manifest = _make_manifest()
        results = await download_submissions(
            manifest=manifest,
            storage_path=tmp_path,
            http_client=mock_client,
        )

        assert results[0].status == "downloaded"
        # stream("GET", url, ...) — the URL is the second positional argument.
        called_url = mock_client.stream.call_args[0][1]
        assert called_url == "https://university.instructure.com/files/569/download"

    def test_build_download_url_rejects_protocol_relative(self) -> None:
        """A `//host/path` value must not survive validation on a hostile base."""
        from aems_agent.canvas_download import _build_download_url

        with pytest.raises(ValueError):
            _build_download_url("https://school.instructure.com", "//evil.example.com/x")


class TestDownloadSizeCap:
    """Oversized Canvas responses are rejected instead of written to disk."""

    @pytest.mark.asyncio
    async def test_oversized_download_marked_failed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from aems_agent import canvas_download
        from aems_agent.canvas_download import download_submissions

        monkeypatch.setattr(canvas_download, "MAX_DOWNLOAD_BYTES", 16)

        mock_client = _stream_client(b"%PDF-" + b"y" * 64)

        manifest = _make_manifest()
        results = await download_submissions(
            manifest=manifest,
            storage_path=tmp_path,
            http_client=mock_client,
        )

        assert results[0].status == "failed"
        assert "size limit" in results[0].error
        assert not (tmp_path / "100" / "1001" / "submission.pdf").exists()
