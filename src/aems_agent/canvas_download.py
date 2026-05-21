# SPDX-License-Identifier: AGPL-3.0-or-later

"""Canvas download logic: manifest validation, idempotent download, job management.

Separated from routes.py to keep download logic testable without FastAPI.
The route layer (routes.py) handles HTTP concerns; this module handles:
- Manifest validation (expiry, host allowlist, audience binding)
- Idempotent file download with skip-on-exist
- In-memory job store with bounded growth
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from urllib.parse import urlparse

# Safe path segment pattern (matches _validate_path_segment in routes.py)
_SAFE_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9_\-]+$")

logger = logging.getLogger(__name__)

# Maximum number of concurrent download jobs tracked in memory.
# Oldest jobs are evicted when this limit is reached.
MAX_JOBS = 100
SUPPORTED_MANIFEST_VERSION = 1


class ManifestValidationError(Exception):
    """Raised when a download manifest fails validation."""

    pass


def validate_manifest(
    manifest: dict[str, Any],
    allowed_hosts: list[str],
    agent_key_id: str,
) -> bool:
    """Validate a decrypted download manifest.

    Checks:
    - Expiry (expires_at must be in the future)
    - HTTPS required for Canvas URL
    - Host must be in allowlist OR match *.instructure.com
    - Audience key ID must match this agent's key
    - Must contain at least one submission

    Args:
        manifest: Decrypted manifest dict.
        allowed_hosts: List of allowed Canvas hostnames.
        agent_key_id: This agent's key fingerprint.

    Returns:
        True if valid.

    Raises:
        ManifestValidationError: If any check fails.
    """
    # Check expiry
    expires_at = manifest.get("expires_at", 0)
    if time.time() > expires_at:
        raise ManifestValidationError("Manifest expired")

    manifest_version = manifest.get("manifest_version")
    if manifest_version != SUPPORTED_MANIFEST_VERSION:
        raise ManifestValidationError(f"Unsupported manifest version: {manifest_version!r}")

    # Check HTTPS
    url = manifest.get("canvas_base_url", "")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ManifestValidationError("HTTPS required for Canvas URL")

    # Check hostname allowlist (with *.instructure.com wildcard)
    hostname = parsed.hostname or ""
    host_allowed = hostname in allowed_hosts
    if not host_allowed and hostname.endswith(".instructure.com"):
        host_allowed = True
    if not host_allowed:
        raise ManifestValidationError(f"Host {hostname!r} not in allowlist: {allowed_hosts}")

    # Check audience binding
    if manifest.get("audience_key_id") != agent_key_id:
        raise ManifestValidationError("Manifest audience does not match agent key ID")

    # Check submissions present
    submissions = manifest.get("submissions", [])
    if not submissions:
        raise ManifestValidationError("Manifest contains no submissions")

    return True


@dataclass
class SubmissionResult:
    """Result of downloading a single submission."""

    submission_id: int
    status: str  # "downloaded", "skipped", "failed"
    sha256: str = ""
    error: str = ""


async def download_submissions(
    manifest: dict[str, Any],
    storage_path: Path,
    http_client: httpx.AsyncClient,
    progress_callback: Optional[Callable[[SubmissionResult], None]] = None,
) -> list[SubmissionResult]:
    """Download submissions from Canvas. Idempotent: skips existing files.

    Args:
        manifest: Validated manifest dict.
        storage_path: Root directory for storing PDFs.
        http_client: Async HTTP client for Canvas API calls.

    Returns:
        List of SubmissionResult, one per submission in the manifest.
    """
    results: list[SubmissionResult] = []
    canvas_base = manifest["canvas_base_url"].rstrip("/")
    token = manifest["canvas_token"]
    aid = manifest["assignment_id"]

    # Validate assignment_id path segment
    aid_str = str(aid)
    if not _SAFE_SEGMENT_RE.match(aid_str):
        raise ValueError(f"Invalid assignment_id path segment: {aid_str!r}")

    for sub in manifest["submissions"]:
        sid = sub["submission_id"]

        # Validate submission_id path segment
        sid_str = str(sid)
        if not _SAFE_SEGMENT_RE.match(sid_str):
            raise ValueError(f"Invalid submission_id path segment: {sid_str!r}")

        target_dir = storage_path / aid_str / sid_str
        # Verify resolved path stays within storage_path
        resolved_target = target_dir.resolve()
        resolved_storage = storage_path.resolve()
        if not resolved_target.is_relative_to(resolved_storage):
            raise ValueError(f"Path traversal detected: {target_dir} escapes {storage_path}")

        target_file = target_dir / "submission.pdf"

        # Idempotency: skip if file exists and size matches (or no expected_size)
        if target_file.exists():
            expected_size = sub.get("expected_size")
            if expected_size is not None and target_file.stat().st_size != expected_size:
                pass  # Size mismatch — fall through to re-download
            elif expected_size is None:
                # No expected_size: validate PDF magic bytes to catch corrupt files
                try:
                    with open(target_file, "rb") as f:
                        magic = f.read(5)
                    if magic != b"%PDF-":
                        logger.warning(
                            "Existing file %s is not a valid PDF (magic=%r), re-downloading",
                            target_file,
                            magic,
                        )
                        pass  # Not a valid PDF — fall through to re-download
                    else:
                        sha = hashlib.sha256(target_file.read_bytes()).hexdigest()
                        result = SubmissionResult(sid, "skipped", sha256=sha)
                        results.append(result)
                        if progress_callback is not None:
                            progress_callback(result)
                        continue
                except OSError:
                    pass  # Can't read file — fall through to re-download
            else:
                # Size matches expected_size — skip
                sha = hashlib.sha256(target_file.read_bytes()).hexdigest()
                result = SubmissionResult(sid, "skipped", sha256=sha)
                results.append(result)
                if progress_callback is not None:
                    progress_callback(result)
                continue

        # Download
        try:
            url = canvas_base + sub["download_url"]
            resp = await http_client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                follow_redirects=True,
                timeout=60.0,
            )
            resp.raise_for_status()
            content = resp.content

            # Validate PDF magic bytes
            if not content[:5] == b"%PDF-":
                result = SubmissionResult(sid, "failed", error="Not a valid PDF")
                results.append(result)
                if progress_callback is not None:
                    progress_callback(result)
                continue

            # Atomic write: temp file then os.replace
            target_dir.mkdir(parents=True, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                dir=str(target_dir),
                prefix="submission.",
                suffix=".tmp",
            )
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content)
                os.replace(tmp_path, str(target_file))
            except Exception:
                with suppress(OSError):
                    os.unlink(tmp_path)
                raise

            # Remove stale annotated PDF after source replacement
            annotated_path = target_dir / "submission_annotated.pdf"
            if annotated_path.exists():
                try:
                    annotated_path.unlink()
                except OSError:
                    # File may be locked; rename as fallback so mtime check catches staleness
                    try:
                        annotated_path.rename(target_dir / "submission_annotated.pdf.stale")
                    except OSError:
                        pass

            sha = hashlib.sha256(content).hexdigest()
            result = SubmissionResult(sid, "downloaded", sha256=sha)
            results.append(result)
            if progress_callback is not None:
                progress_callback(result)

        except Exception as e:
            result = SubmissionResult(sid, "failed", error=str(e))
            results.append(result)
            if progress_callback is not None:
                progress_callback(result)

    return results


# ---------------------------------------------------------------------------
# In-memory Job Store
# ---------------------------------------------------------------------------


@dataclass
class DownloadJob:
    """Tracks the state of a download job."""

    job_id: str
    status: str = "pending"  # "pending", "running", "completed", "completed_with_errors", "failed"
    total_submissions: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    per_submission: list[dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)


# Module-level job store. Bounded by MAX_JOBS.
_download_jobs: dict[str, DownloadJob] = {}


def _evict_oldest_jobs() -> None:
    """Evict oldest jobs if store exceeds MAX_JOBS."""
    while len(_download_jobs) >= MAX_JOBS:
        oldest_key = min(_download_jobs, key=lambda k: _download_jobs[k].created_at)
        del _download_jobs[oldest_key]


def create_download_job(manifest: dict[str, Any]) -> str:
    """Create a new download job and return its ID.

    Args:
        manifest: The validated download manifest.

    Returns:
        Unique job ID string.
    """
    _evict_oldest_jobs()

    job_id = secrets.token_urlsafe(16)
    job = DownloadJob(
        job_id=job_id,
        total_submissions=len(manifest.get("submissions", [])),
    )
    _download_jobs[job_id] = job
    return job_id


def get_download_job(job_id: str) -> Optional[DownloadJob]:
    """Look up a download job by ID.

    Args:
        job_id: The job identifier.

    Returns:
        DownloadJob if found, None otherwise.
    """
    return _download_jobs.get(job_id)


async def run_download_job(
    job_id: str,
    manifest: dict[str, Any],
    storage_path: Path,
    http_client: Optional[httpx.AsyncClient] = None,
) -> None:
    """Execute a download job, updating job state as it progresses.

    If no http_client is provided, creates one internally.

    Args:
        job_id: The job identifier.
        manifest: Validated download manifest.
        storage_path: Root directory for PDF storage.
        http_client: Optional pre-configured async HTTP client.
    """
    job = _download_jobs.get(job_id)
    if job is None:
        logger.error("Download job %s not found", job_id)
        return

    job.status = "running"

    try:

        def record_result(result: SubmissionResult) -> None:
            job.per_submission.append(
                {
                    "submission_id": result.submission_id,
                    "status": result.status,
                    "sha256": result.sha256,
                    "error": result.error,
                }
            )
            if result.status == "downloaded":
                job.downloaded += 1
            elif result.status == "skipped":
                job.skipped += 1
            elif result.status == "failed":
                job.failed += 1

        owns_client = http_client is None
        if owns_client:
            http_client = httpx.AsyncClient()

        assert http_client is not None  # narrowing for mypy
        try:
            await download_submissions(
                manifest=manifest,
                storage_path=storage_path,
                http_client=http_client,
                progress_callback=record_result,
            )
        finally:
            if owns_client and http_client is not None:
                await http_client.aclose()

        job.status = "completed" if job.failed == 0 else "completed_with_errors"

    except Exception as e:
        logger.error("Download job %s failed: %s", job_id, e, exc_info=True)
        job.status = "failed"
        job.per_submission.append(
            {
                "submission_id": 0,
                "status": "failed",
                "sha256": "",
                "error": str(e),
            }
        )
