# SPDX-License-Identifier: AGPL-3.0-or-later

"""Canvas download logic: manifest validation, idempotent download, job management.

Separated from routes.py to keep download logic testable without FastAPI.
The route layer (routes.py) handles HTTP concerns; this module handles:
- Manifest validation (expiry, host allowlist, audience binding)
- Idempotent file download with skip-on-exist
- In-memory job store with bounded growth
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
import secrets
import socket
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

# Cap for a single submission download (matches the agent's upload cap).
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024

# Canvas file endpoints typically 302 once to a pre-signed CDN/S3 URL; a long
# redirect chain is abnormal and bounding it limits redirect-based abuse.
MAX_DOWNLOAD_REDIRECTS = 5


class UnsafeRedirectError(Exception):
    """Raised when a Canvas download tries to redirect to a disallowed target."""


def _is_public_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for ordinary public/global addresses (SSRF guard)."""
    # ``not private`` is not the same as globally reachable: for example,
    # 100.64.0.0/10 shared-address space is neither private nor global. Use
    # ipaddress' global classification, with multicast explicitly excluded
    # because it is marked global by the stdlib but is not a safe HTTP target.
    return addr.is_global and not addr.is_multicast


async def _is_safe_redirect_target(url: httpx.URL) -> bool:
    """Return True if *url* is HTTPS and every resolved IP is a public address.

    A Canvas download legitimately redirects to a pre-signed CDN/S3 URL on a
    *different* public host, so we cannot pin to the Canvas host — but we can
    still refuse redirects to a non-HTTPS scheme or to a loopback / private /
    link-local / reserved address (e.g. the cloud metadata endpoint
    169.254.169.254). Resolution is best-effort; a residual DNS-rebinding TOCTOU
    remains between this check and httpx's own connect.
    """
    if url.scheme != "https":
        return False
    host = url.host
    if not host:
        return False
    # Literal IP host: validate directly, no DNS.
    try:
        return _is_public_ip(ipaddress.ip_address(host))
    except ValueError:
        pass
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, url.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not _is_public_ip(addr):
            return False
    return True


async def _redirect_ssrf_guard(response: httpx.Response) -> None:
    """httpx ``response`` event hook that vetoes unsafe redirect targets.

    Fires for every response in a redirect chain. For a redirect it resolves the
    Location and raises — aborting the chain before httpx connects to the next
    hop — when the target is not a public HTTPS address. The agent's Canvas
    bearer token is not exposed: httpx (>=0.25) strips the Authorization header
    on cross-host redirects.
    """
    if not response.is_redirect:
        return
    location = response.headers.get("location")
    if not location:
        return
    target = response.url.join(location)
    if not await _is_safe_redirect_target(target):
        raise UnsafeRedirectError(
            f"Blocked Canvas download redirect to {target.scheme}://{target.host}"
        )


def _new_download_client() -> httpx.AsyncClient:
    """Create the httpx client used for Canvas downloads, with the SSRF guard."""
    return httpx.AsyncClient(
        event_hooks={"response": [_redirect_ssrf_guard]},
        max_redirects=MAX_DOWNLOAD_REDIRECTS,
    )


def _file_sha256(path: Path) -> str:
    """Compute a SHA-256 digest without buffering the whole file in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_download_url(canvas_base: str, download_url: Any) -> str:
    """Join a manifest ``download_url`` onto the validated Canvas base URL.

    The manifest is produced by the AEMS server and sealed to this agent,
    but defence-in-depth still applies: a value like ``@evil.com/x`` or a
    full URL would otherwise redirect the request — including the Canvas
    bearer token — to a host outside the validated allowlist. Require an
    absolute path and verify the joined URL still resolves to the same
    scheme/host/port as ``canvas_base``.

    Raises:
        ValueError: If the download URL is missing, relative, or escapes
            the Canvas host.
    """
    if not isinstance(download_url, str) or not download_url.startswith("/"):
        raise ValueError("Invalid download_url: must be an absolute path on the Canvas host")
    if download_url.startswith("//"):
        # Protocol-relative form: harmless under plain concatenation, but it
        # would resolve to another authority under urljoin-style handling.
        raise ValueError("Invalid download_url: protocol-relative paths are not allowed")

    url = canvas_base + download_url
    base = urlparse(canvas_base)
    final = urlparse(url)
    if (
        final.scheme != "https"
        or final.hostname != base.hostname
        or final.port != base.port
        or final.username is not None
        or final.password is not None
    ):
        raise ValueError("Invalid download_url: resolves outside the Canvas host")
    return url


async def _stream_download_capped(
    http_client: httpx.AsyncClient,
    url: str,
    token: str,
    max_bytes: int,
) -> tuple[bytes, bool]:
    """Download *url* with the Canvas token, aborting once *max_bytes* is crossed.

    Uses a streaming request and caps bytes while iterating so a hostile or
    buggy server cannot force the agent to buffer an arbitrarily large body in
    memory before the size check runs (``AsyncClient.get`` would read the whole
    body first). The connection is closed as soon as the limit is exceeded.

    Returns ``(content, too_large)``. When ``too_large`` is True the content is
    empty and the caller should reject the submission.
    """
    chunks: list[bytes] = []
    total = 0
    async with http_client.stream(
        "GET",
        url,
        headers={"Authorization": f"Bearer {token}"},
        follow_redirects=True,
        timeout=60.0,
    ) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                return b"", True
            chunks.append(chunk)
    return b"".join(chunks), False


class ManifestValidationError(ValueError):
    """Raised with a safe machine-readable reason for an invalid manifest."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        rejected_host: Optional[str] = None,
    ) -> None:
        self.code = code
        self.rejected_host = rejected_host
        super().__init__(message)

    def as_detail(self) -> dict[str, str]:
        """Return the safe subset suitable for an HTTP error response."""
        detail = {"code": self.code, "message": str(self)}
        if self.rejected_host:
            detail["rejected_host"] = self.rejected_host
        return detail


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
        raise ManifestValidationError("Manifest expired", code="manifest_expired")

    manifest_version = manifest.get("manifest_version")
    if manifest_version != SUPPORTED_MANIFEST_VERSION:
        raise ManifestValidationError(
            f"Unsupported manifest version: {manifest_version!r}",
            code="unsupported_manifest_version",
        )

    # Check HTTPS
    url = manifest.get("canvas_base_url", "")
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ManifestValidationError("HTTPS required for Canvas URL", code="canvas_https_required")

    # Check hostname allowlist (with *.instructure.com wildcard)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    normalized_allowed_hosts = {host.lower().rstrip(".") for host in allowed_hosts}
    host_allowed = hostname in normalized_allowed_hosts
    if not host_allowed and hostname.endswith(".instructure.com"):
        host_allowed = True
    if not host_allowed:
        raise ManifestValidationError(
            f"Canvas host {hostname!r} is not allowed. Add it in Local Agent settings and retry.",
            code="canvas_host_not_allowed",
            rejected_host=hostname or None,
        )

    # Check audience binding
    if manifest.get("audience_key_id") != agent_key_id:
        raise ManifestValidationError(
            "Manifest audience does not match agent key ID",
            code="manifest_audience_mismatch",
        )

    # Check submissions present
    submissions = manifest.get("submissions", [])
    if not submissions:
        raise ManifestValidationError(
            "Manifest contains no submissions", code="manifest_has_no_submissions"
        )

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
                        sha = _file_sha256(target_file)
                        result = SubmissionResult(sid, "skipped", sha256=sha)
                        results.append(result)
                        if progress_callback is not None:
                            progress_callback(result)
                        continue
                except OSError:
                    pass  # Can't read file — fall through to re-download
            else:
                # Size matches expected_size — skip
                sha = _file_sha256(target_file)
                result = SubmissionResult(sid, "skipped", sha256=sha)
                results.append(result)
                if progress_callback is not None:
                    progress_callback(result)
                continue

        # Validate the download URL before any request leaves the agent so a
        # hostile path cannot leak the Canvas token to another host.
        try:
            url = _build_download_url(canvas_base, sub.get("download_url"))
        except ValueError as e:
            result = SubmissionResult(sid, "failed", error=str(e))
            results.append(result)
            if progress_callback is not None:
                progress_callback(result)
            continue

        # Download
        try:
            content, too_large = await _stream_download_capped(
                http_client, url, token, MAX_DOWNLOAD_BYTES
            )

            if too_large:
                result = SubmissionResult(
                    sid,
                    "failed",
                    error=f"Download exceeds size limit ({MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB)",
                )
                results.append(result)
                if progress_callback is not None:
                    progress_callback(result)
                continue

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
    task: Any = None


# Module-level job store. Bounded by MAX_JOBS.
_download_jobs: dict[str, DownloadJob] = {}


def _evict_oldest_jobs() -> None:
    """Evict oldest jobs if store exceeds MAX_JOBS."""
    while len(_download_jobs) >= MAX_JOBS:
        evictable = {key: job for key, job in _download_jobs.items() if job.status != "running"}
        if not evictable:
            break
        oldest_key = min(evictable, key=lambda k: evictable[k].created_at)
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


def attach_download_task(job_id: str, task: Any) -> None:
    """Hold a strong reference to a background download task."""
    job = _download_jobs.get(job_id)
    if job is not None:
        job.task = task


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
        submission_metadata: dict[int, dict[str, Any]] = {}
        for submission in manifest.get("submissions", []):
            if not isinstance(submission, dict):
                continue
            try:
                submission_id = int(submission["submission_id"])
            except (KeyError, TypeError, ValueError):
                continue
            submission_metadata[submission_id] = submission

        def record_result(result: SubmissionResult) -> None:
            metadata = submission_metadata.get(result.submission_id, {})
            job.per_submission.append(
                {
                    "submission_id": result.submission_id,
                    "student_id": metadata.get("student_id") or metadata.get("user_id"),
                    "student_name": metadata.get("student_name"),
                    "filename": metadata.get("filename"),
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
            http_client = _new_download_client()

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
