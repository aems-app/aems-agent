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
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Maximum number of concurrent download jobs tracked in memory.
# Oldest jobs are evicted when this limit is reached.
MAX_JOBS = 100


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
        raise ManifestValidationError(
            f"Host {hostname!r} not in allowlist: {allowed_hosts}"
        )

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

    for sub in manifest["submissions"]:
        sid = sub["submission_id"]
        target_dir = storage_path / str(aid) / str(sid)
        target_file = target_dir / "submission.pdf"

        # Idempotency: skip if file exists and size matches (or no expected_size)
        if target_file.exists():
            expected_size = sub.get("expected_size")
            if expected_size is None or target_file.stat().st_size == expected_size:
                sha = hashlib.sha256(target_file.read_bytes()).hexdigest()
                results.append(SubmissionResult(sid, "skipped", sha256=sha))
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
                results.append(SubmissionResult(sid, "failed", error="Not a valid PDF"))
                continue

            # Atomic write: temp file then os.replace
            target_dir.mkdir(parents=True, exist_ok=True)
            tmp = target_file.with_suffix(".tmp")
            tmp.write_bytes(content)
            os.replace(str(tmp), str(target_file))

            sha = hashlib.sha256(content).hexdigest()
            results.append(SubmissionResult(sid, "downloaded", sha256=sha))

        except Exception as e:
            results.append(SubmissionResult(sid, "failed", error=str(e)))

    return results


# ---------------------------------------------------------------------------
# In-memory Job Store
# ---------------------------------------------------------------------------


@dataclass
class DownloadJob:
    """Tracks the state of a download job."""

    job_id: str
    status: str = "pending"  # "pending", "running", "completed", "failed"
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
        owns_client = http_client is None
        if owns_client:
            http_client = httpx.AsyncClient()

        try:
            results = await download_submissions(
                manifest=manifest,
                storage_path=storage_path,
                http_client=http_client,
            )
        finally:
            if owns_client and http_client is not None:
                await http_client.aclose()

        # Update job with results
        for r in results:
            job.per_submission.append({
                "submission_id": r.submission_id,
                "status": r.status,
                "sha256": r.sha256,
                "error": r.error,
            })
            if r.status == "downloaded":
                job.downloaded += 1
            elif r.status == "skipped":
                job.skipped += 1
            elif r.status == "failed":
                job.failed += 1

        job.status = "completed" if job.failed == 0 else "completed_with_errors"

    except Exception as e:
        logger.error("Download job %s failed: %s", job_id, e, exc_info=True)
        job.status = "failed"
        job.per_submission.append({
            "submission_id": 0,
            "status": "failed",
            "sha256": "",
            "error": str(e),
        })
