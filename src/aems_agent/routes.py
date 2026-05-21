# SPDX-License-Identifier: AGPL-3.0-or-later

"""
FastAPI router with all AEMS Local Bridge Agent endpoints.

Endpoint summary:
    GET  /status                                        - Alive check (no auth)
    GET  /capabilities                                  - Discovery / public key (no auth)
    GET  /info                                          - Version / API info (auth)
    GET  /health                                        - Detailed health with disk info (auth)
    GET  /config/path                                   - Get storage path (auth)
    PUT  /config/path                                   - Set storage path (auth)
    GET  /files/{assignment_id}                         - List submissions (auth)
    GET  /files/{assignment_id}/{submission_id}          - Download PDF (auth)
    PUT  /files/{assignment_id}/{submission_id}          - Store PDF (auth)
    DELETE /files/{assignment_id}                        - Delete all local files for an assessment (auth)
    DELETE /files/{assignment_id}/{submission_id}        - Delete PDF (auth)
    GET  /files/{assignment_id}/{submission_id}/annotated - Download annotated (auth)
    PUT  /files/{assignment_id}/{submission_id}/annotated - Store annotated (auth)
    PUT  /data/{aid}/results/{sid}.json                 - Store result JSON (auth)
    GET  /data/{aid}/results/{sid}.json                 - Get result JSON (auth)
    GET  /data/{aid}/results/                           - List result files (auth)
    PUT  /data/{aid}/assignment.json                    - Store assignment JSON (auth)
    GET  /data/{aid}/assignment.json                    - Get assignment JSON (auth)
    POST /annotate/{assignment_id}/{submission_id}       - Generate annotated PDF (auth)
    GET  /annotations/{aid}/{sid}                       - List annotations (auth)
    GET  /annotations/{aid}/{sid}/version               - Annotation version token (auth)
    POST /annotations/{aid}/{sid}                       - Add annotation (auth)
    PUT  /annotations/{aid}/{sid}/{annot_id}            - Update annotation (auth)
    DELETE /annotations/{aid}/{sid}/{annot_id}           - Delete annotation (auth)
    POST /canvas/download-submissions                   - Start download job (auth, encrypted)
    GET  /canvas/download-jobs/{job_id}                 - Poll download progress (auth)
    POST /grading-bundle/{aid}/{sid}                    - Generate grading input bundle (auth)
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import secrets
import shutil
import sys
import tempfile
import time
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, TextIO
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from . import annotation_crud
from .config import (
    AGENT_VERSION,
    API_VERSION,
    MIN_CLIENT_API_VERSION,
    AgentConfig,
    load_config,
    save_config,
)
from .security import RateLimiter, validate_path_within_storage

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level rate limiter (100 req/min)
_rate_limiter = RateLimiter(max_requests=100, window_seconds=60.0)

# Maximum upload size: 200 MB (exam PDFs can be large with images)
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_MAX_JSON_BYTES = 5 * 1024 * 1024
_PAIRING_CHALLENGE_TTL_SECONDS = 120.0
_PAIRING_FAILURE_DETAIL = "Pairing failed"
_PAIRING_MAX_FAILED_PINS = 5
_PAIRING_LOCKOUT_SECONDS = 24 * 60 * 60.0

# These will be set by app.py at startup
_config_dir: Optional[Path] = None
_auth_token: Optional[str] = None

# Pairing state (in-memory, single active challenge)
_pairing_challenge: Optional[Dict[str, Any]] = None
_pairing_lock = asyncio.Lock()
_pairing_rate_limiter = RateLimiter(max_requests=6, window_seconds=60.0)
_pairing_failed_pin_count = 0
_pairing_failed_pin_window_started_at = 0.0
_pairing_lockout_until = 0.0


def set_agent_globals(config_dir: Path, auth_token: str) -> None:
    """Set module-level globals used by route handlers."""
    global _config_dir, _auth_token
    _config_dir = config_dir
    _auth_token = auth_token


def _get_config() -> AgentConfig:
    """Load the current agent config."""
    return load_config(_config_dir)


def _verify_token(authorization: Optional[str] = Header(default=None)) -> str:
    """FastAPI dependency to verify bearer token authentication."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Invalid authorization format")

    token = parts[1].strip()
    if not _auth_token or not secrets.compare_digest(token, _auth_token):
        raise HTTPException(status_code=403, detail="Invalid token")

    return token


def _check_rate_limit(request: Request) -> None:
    """FastAPI dependency to enforce rate limiting."""
    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limiter.is_allowed(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")


def _get_storage_path() -> Path:
    """Get and validate the configured storage path."""
    config = _get_config()
    if not config.storage_path:
        raise HTTPException(status_code=503, detail="Storage path not configured")

    path = Path(config.storage_path)
    if not path.exists():
        raise HTTPException(status_code=503, detail="Storage path does not exist")

    return path


def _validate_path_segment(value: str, name: str) -> str:
    """Validate a path segment contains only safe characters."""
    if not value or not re.match(r"^[a-zA-Z0-9_\-]+$", value):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {name}: must contain only alphanumeric, dash, or underscore",
        )
    return value


def _submission_dir(storage_path: Path, assignment_id: str, submission_id: str) -> Path:
    """Get the validated submission directory path."""
    _validate_path_segment(assignment_id, "assignment_id")
    _validate_path_segment(submission_id, "submission_id")
    return validate_path_within_storage(storage_path, assignment_id, submission_id)


def _assignment_dir(storage_path: Path, assignment_id: str) -> Path:
    """Get the validated assignment directory path."""
    _validate_path_segment(assignment_id, "assignment_id")
    return validate_path_within_storage(storage_path, assignment_id)


def _annotated_pdf_path(storage_path: Path, assignment_id: str, submission_id: str) -> Path:
    """Get the annotated PDF path for a submission."""
    return _submission_dir(storage_path, assignment_id, submission_id) / "submission_annotated.pdf"


def _compute_sha256(data: bytes) -> str:
    """Compute SHA-256 hex digest of data."""
    return hashlib.sha256(data).hexdigest()


def _compute_file_sha256(path: Path) -> str:
    """Compute a SHA-256 digest without buffering the whole file in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_origin(origin: Optional[str]) -> Optional[str]:
    """
    Normalize and validate an origin string.

    Returns a canonical "scheme://host[:port]" representation, or None if
    invalid. Paths/query/fragment are not allowed.
    """
    if not origin:
        return None

    value = origin.strip()
    if not value:
        return None

    try:
        parsed = urlparse(value)
    except (ValueError, AttributeError):
        return None

    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    if parsed.path not in ("", "/"):
        return None
    if parsed.params or parsed.query or parsed.fragment:
        return None

    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError:
        return None
    return f"{parsed.scheme}://{host}:{port}" if port else f"{parsed.scheme}://{host}"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class SetPathRequest(BaseModel):
    """Request body for setting storage path."""

    path: str = Field(..., description="Absolute path to storage directory")

    @field_validator("path")
    @classmethod
    def validate_absolute(cls, v: str) -> str:
        if not Path(v).is_absolute():
            raise ValueError("Path must be absolute")
        return v


class FileInfo(BaseModel):
    """Information about a submission file."""

    submission_id: str
    has_submission: bool = False
    has_annotated: bool = False
    submission_size: Optional[int] = None
    annotated_size: Optional[int] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/status")
async def status() -> Dict[str, Any]:
    """Alive check - no authentication required. Minimal info only."""
    return {
        "status": "ok",
        "service": "aems-agent",
        "version": AGENT_VERSION,
        "api_version": API_VERSION,
        "min_client_version": MIN_CLIENT_API_VERSION,
    }


@router.get("/capabilities")
async def get_capabilities() -> Dict[str, Any]:
    """Return agent version, supported contract versions, and encryption key."""
    import base64

    from aems_pdf_annotator import SUPPORTED_CONTRACT_VERSIONS

    from .crypto import get_key_id, load_public_key

    if _config_dir is None:
        raise HTTPException(status_code=500, detail="Agent config not initialized")
    supported_versions = sorted(SUPPORTED_CONTRACT_VERSIONS)
    return {
        "agent_version": AGENT_VERSION,
        "supported_contract_versions": supported_versions,
        "features": [
            "file_storage",
            "canvas_download",
            "local_annotation",
            "annotation_crud",
            "grading_bundle",
        ],
        "supported_bundle_versions": [1],
        "supported_annotation_contract_versions": supported_versions,
        "encryption_key_id": get_key_id(_config_dir),
        "public_key_base64": base64.b64encode(load_public_key(_config_dir)).decode(),
    }


@router.get("/info")
async def info(
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Authenticated agent info endpoint with minimal metadata."""
    return {
        "version": AGENT_VERSION,
        "api_version": API_VERSION,
        "min_client_version": MIN_CLIENT_API_VERSION,
    }


@router.get("/health")
async def health(
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Authenticated health endpoint with storage diagnostics."""
    config = _get_config()
    result: Dict[str, Any] = {
        "status": "ok",
        "service": "aems-agent",
        "version": AGENT_VERSION,
        "storage_path": config.storage_path,
        "storage_configured": config.storage_path is not None,
    }

    if config.storage_path:
        path = Path(config.storage_path)
        result["storage_exists"] = path.exists()
        result["storage_writable"] = path.exists() and os.access(path, os.W_OK)
        if path.exists():
            try:
                usage = shutil.disk_usage(path)
                result["disk_total_bytes"] = usage.total
                result["disk_free_bytes"] = usage.free
                result["disk_used_bytes"] = usage.used
            except OSError:
                pass

    return result


@router.get("/config/path")
async def get_path(
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Get the current storage path."""
    config = _get_config()
    return {"path": config.storage_path}


@router.put("/config/path")
async def set_path(
    body: SetPathRequest,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Set the storage path (validates the directory is writable)."""
    path = Path(body.path)

    if not path.exists():
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Cannot create directory %s: %s", path, e)
            raise HTTPException(status_code=400, detail="Cannot create directory")

    if not path.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    if not os.access(path, os.W_OK):
        raise HTTPException(status_code=400, detail="Directory is not writable")

    config = _get_config()
    config.storage_path = str(path)
    save_config(config, _config_dir)

    return {"path": str(path), "message": "Storage path updated"}


@router.get("/files/{assignment_id}")
async def list_submissions(
    assignment_id: str,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """List submissions in an assignment directory."""
    _validate_path_segment(assignment_id, "assignment_id")
    storage_path = _get_storage_path()
    assignment_dir = validate_path_within_storage(storage_path, assignment_id)

    submissions: List[Dict[str, Any]] = []
    if assignment_dir.exists() and assignment_dir.is_dir():
        for entry in sorted(assignment_dir.iterdir()):
            if not entry.is_dir():
                continue
            # Skip entries with invalid names (e.g., unexpected chars on disk)
            if not re.match(r"^[a-zA-Z0-9_\-]+$", entry.name):
                continue
            sub_pdf = entry / "submission.pdf"
            ann_pdf = entry / "submission_annotated.pdf"
            info = FileInfo(
                submission_id=entry.name,
                has_submission=sub_pdf.exists(),
                has_annotated=ann_pdf.exists(),
                submission_size=sub_pdf.stat().st_size if sub_pdf.exists() else None,
                annotated_size=ann_pdf.stat().st_size if ann_pdf.exists() else None,
            )
            submissions.append(info.model_dump())

    return {"assignment_id": assignment_id, "submissions": submissions}


@router.get("/files/{assignment_id}/{submission_id}")
async def get_submission(
    assignment_id: str,
    submission_id: str,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Response:
    """Download a submission PDF."""
    storage_path = _get_storage_path()
    sub_dir = _submission_dir(storage_path, assignment_id, submission_id)
    pdf_path = sub_dir / "submission.pdf"

    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Submission PDF not found")

    sha256 = _compute_file_sha256(pdf_path)

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=f"submission_{submission_id}.pdf",
        headers={"X-SHA256": sha256},
    )


@router.delete("/files/{assignment_id}")
async def delete_assignment_files(
    assignment_id: str,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Delete all local files associated with an assessment."""
    storage_path = _get_storage_path()
    assignment_dir = _assignment_dir(storage_path, assignment_id)
    data_dir = _data_dir(storage_path, assignment_id)
    cache_dir = validate_path_within_storage(storage_path, "_cache", "bundles", assignment_id)

    assignment_deleted = False
    data_deleted = False
    cache_deleted = False

    if assignment_dir.exists():
        shutil.rmtree(str(assignment_dir))
        assignment_deleted = True
    if data_dir.exists():
        shutil.rmtree(str(data_dir))
        data_deleted = True
    if cache_dir.exists():
        shutil.rmtree(str(cache_dir))
        cache_deleted = True

    if not (assignment_deleted or data_deleted or cache_deleted):
        raise HTTPException(status_code=404, detail="Assessment files not found")

    return {
        "success": True,
        "assignment_id": assignment_id,
        "assignment_deleted": assignment_deleted,
        "data_deleted": data_deleted,
        "cache_deleted": cache_deleted,
        "message": "Assessment files deleted",
    }


@router.put("/files/{assignment_id}/{submission_id}")
async def store_submission(
    assignment_id: str,
    submission_id: str,
    request: Request,
    x_sha256: Optional[str] = Header(default=None),
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Store a submission PDF with atomic write."""
    storage_path = _get_storage_path()
    sub_dir = _submission_dir(storage_path, assignment_id, submission_id)

    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty request body")

    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
        )

    # Validate PDF magic bytes
    if not data[:5] == b"%PDF-":
        raise HTTPException(status_code=400, detail="Not a valid PDF")

    # Verify SHA-256 if provided
    actual_sha256 = _compute_sha256(data)
    if x_sha256:
        if not re.match(r"^[a-fA-F0-9]{64}$", x_sha256):
            raise HTTPException(status_code=400, detail="Invalid X-SHA256 format")
        if x_sha256.lower() != actual_sha256:
            raise HTTPException(status_code=400, detail="SHA-256 mismatch")

    # Atomic write: temp file then os.replace
    sub_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = sub_dir / "submission.pdf"

    fd, tmp_path = tempfile.mkstemp(dir=str(sub_dir), suffix=".tmp")
    try:
        os.write(fd, data)
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(pdf_path))
    except Exception:
        if fd >= 0:
            os.close(fd)
        with suppress(OSError):
            os.unlink(tmp_path)
        raise

    # Remove stale annotated PDF after source replacement
    annotated_path = sub_dir / "submission_annotated.pdf"
    if annotated_path.exists():
        try:
            annotated_path.unlink()
        except OSError:
            # File may be locked (e.g. on Windows); rename as fallback so
            # the annotate route won't reuse it (mtime check will also catch this).
            with suppress(OSError):
                annotated_path.rename(sub_dir / "submission_annotated.pdf.stale")

    return {
        "success": True,
        "assignment_id": assignment_id,
        "submission_id": submission_id,
        "size": len(data),
        "sha256": actual_sha256,
    }


@router.delete("/files/{assignment_id}/{submission_id}")
async def delete_submission(
    assignment_id: str,
    submission_id: str,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Delete a submission directory and all its files."""
    storage_path = _get_storage_path()
    sub_dir = _submission_dir(storage_path, assignment_id, submission_id)

    if not sub_dir.exists():
        raise HTTPException(status_code=404, detail="Submission not found")

    shutil.rmtree(str(sub_dir))

    return {
        "success": True,
        "assignment_id": assignment_id,
        "submission_id": submission_id,
        "message": "Submission deleted",
    }


@router.get("/files/{assignment_id}/{submission_id}/annotated")
async def get_annotated(
    assignment_id: str,
    submission_id: str,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Response:
    """Download an annotated submission PDF."""
    storage_path = _get_storage_path()
    sub_dir = _submission_dir(storage_path, assignment_id, submission_id)
    pdf_path = sub_dir / "submission_annotated.pdf"

    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Annotated PDF not found")

    sha256 = _compute_file_sha256(pdf_path)

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=f"submission_{submission_id}_annotated.pdf",
        headers={"X-SHA256": sha256},
    )


@router.put("/files/{assignment_id}/{submission_id}/annotated")
async def store_annotated(
    assignment_id: str,
    submission_id: str,
    request: Request,
    x_sha256: Optional[str] = Header(default=None),
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Store an annotated submission PDF with atomic write."""
    storage_path = _get_storage_path()
    sub_dir = _submission_dir(storage_path, assignment_id, submission_id)

    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty request body")

    if len(data) > _MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB)",
        )

    if not data[:5] == b"%PDF-":
        raise HTTPException(status_code=400, detail="Not a valid PDF")

    actual_sha256 = _compute_sha256(data)
    if x_sha256:
        if not re.match(r"^[a-fA-F0-9]{64}$", x_sha256):
            raise HTTPException(status_code=400, detail="Invalid X-SHA256 format")
        if x_sha256.lower() != actual_sha256:
            raise HTTPException(status_code=400, detail="SHA-256 mismatch")

    sub_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = sub_dir / "submission_annotated.pdf"

    fd, tmp_path = tempfile.mkstemp(dir=str(sub_dir), suffix=".tmp")
    try:
        os.write(fd, data)
        os.close(fd)
        fd = -1
        os.replace(tmp_path, str(pdf_path))
    except Exception:
        if fd >= 0:
            os.close(fd)
        with suppress(OSError):
            os.unlink(tmp_path)
        raise

    return {
        "success": True,
        "assignment_id": assignment_id,
        "submission_id": submission_id,
        "size": len(data),
        "sha256": actual_sha256,
    }


# ---------------------------------------------------------------------------
# Data JSON Storage Endpoints
# ---------------------------------------------------------------------------


def _data_dir(storage_path: Path, aid: str) -> Path:
    """Get the validated _data/{aid} directory path within storage."""
    _validate_path_segment(aid, "assignment_id")
    return validate_path_within_storage(storage_path, "_data", aid)


def _write_json_atomic(target: Path, content: bytes) -> Dict[str, Any]:
    """
    Atomically write JSON content to *target* and return a write receipt.

    Writes to a temporary file first, then uses os.replace() for atomicity.
    Returns dict with ``receipt`` (SHA-256 hex) and ``written_at`` (ISO 8601).
    """
    sha = hashlib.sha256(content).hexdigest()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f"{target.stem}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(tmp_path, str(target))
    except Exception:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {"receipt": sha, "written_at": now}


async def _read_limited_body(request: Request, max_bytes: int) -> bytes:
    """Read a request body up to *max_bytes* and raise 413 if exceeded."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"JSON body too large (max {max_bytes // (1024 * 1024)} MiB)",
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc

    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"JSON body too large (max {max_bytes // (1024 * 1024)} MiB)",
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_json_body(request: Request) -> Any:
    """Read JSON request bodies with a bounded size and a 400 on malformed JSON."""
    try:
        data = await _read_limited_body(request, _MAX_JSON_BYTES)
        return json.loads(data)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Malformed JSON body") from exc


def _reset_pairing_failures() -> None:
    """Clear failed-PIN counters after a successful or stale pairing flow."""
    global _pairing_failed_pin_count, _pairing_failed_pin_window_started_at
    _pairing_failed_pin_count = 0
    _pairing_failed_pin_window_started_at = 0.0


def _pairing_retry_after(now: float) -> int:
    """Return retry-after seconds for an active lockout."""
    return max(1, int(_pairing_lockout_until - now))


def _ensure_pairing_not_locked(now: float) -> None:
    """Reject pairing while a failed-PIN lockout is active."""
    global _pairing_lockout_until
    if _pairing_lockout_until and now >= _pairing_lockout_until:
        _pairing_lockout_until = 0.0
    if _pairing_lockout_until:
        retry_after = _pairing_retry_after(now)
        raise HTTPException(
            status_code=429,
            detail="Pairing temporarily locked",
            headers={"Retry-After": str(retry_after)},
        )


def _record_failed_pin_attempt(now: float) -> bool:
    """Track failed PIN attempts and start a lockout when the threshold is reached."""
    global _pairing_failed_pin_count, _pairing_failed_pin_window_started_at, _pairing_lockout_until

    if (
        _pairing_failed_pin_window_started_at == 0.0
        or (now - _pairing_failed_pin_window_started_at) >= _PAIRING_LOCKOUT_SECONDS
    ):
        _pairing_failed_pin_count = 0
        _pairing_failed_pin_window_started_at = now

    _pairing_failed_pin_count += 1
    if _pairing_failed_pin_count >= _PAIRING_MAX_FAILED_PINS:
        _pairing_lockout_until = now + _PAIRING_LOCKOUT_SECONDS
        _reset_pairing_failures()
        return True
    return False


def _maybe_echo_pairing_pin(
    pin: str,
    origin_header: str,
    stdout: Optional[TextIO] = None,
) -> bool:
    """Echo the PIN to a terminal only when stdout is an interactive TTY."""
    stream = stdout or sys.stdout
    is_tty = getattr(stream, "isatty", None)
    if not callable(is_tty) or not is_tty():
        return False
    print(f"\n{'=' * 40}", file=stream)
    print(f"  PAIRING PIN: {pin}", file=stream)
    print(f"  Origin: {origin_header}", file=stream)
    print(f"{'=' * 40}\n", file=stream)
    return True


@router.put("/data/{aid}/results/{sid}.json")
async def put_result_json(
    aid: str,
    sid: str,
    request: Request,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Store a grading result JSON with atomic write and write receipt."""
    storage_path = _get_storage_path()
    aid = _validate_path_segment(aid, "assignment_id")
    sid = _validate_path_segment(sid, "submission_id")
    results_dir = validate_path_within_storage(storage_path, "_data", aid, "results")

    # Check for delivery_id idempotency before reading the request body
    delivery_id: str | None = request.headers.get("X-AEMS-Delivery-Id")
    sidecar_path = results_dir / f"{sid}.delivery"

    if delivery_id:
        # Check sidecar for existing delivery with same ID
        try:
            if sidecar_path.exists():
                sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
                if sidecar.get("delivery_id") == delivery_id:
                    # Same delivery_id → return original receipt (idempotent)
                    return {
                        "receipt": sidecar["receipt"],
                        "written_at": sidecar["written_at"],
                    }
        except (json.JSONDecodeError, OSError, KeyError):
            pass  # Corrupted sidecar, proceed with normal write

    body = await _read_json_body(request)
    content = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
    target = results_dir / f"{sid}.json"
    previous_content = target.read_bytes() if target.exists() else None
    receipt = _write_json_atomic(target, content)

    if previous_content != content:
        annotated_path = _annotated_pdf_path(storage_path, aid, sid)
        if annotated_path.exists():
            try:
                annotated_path.unlink()
            except OSError:
                # File may be locked; rename as fallback so mtime check catches staleness
                with suppress(OSError):
                    annotated_path.rename(annotated_path.with_suffix(".pdf.stale"))

    # Write delivery sidecar if delivery_id provided
    if delivery_id:
        sidecar_data: Dict[str, str] = {
            "delivery_id": delivery_id,
            "receipt": receipt["receipt"],
            "written_at": receipt["written_at"],
        }
        try:
            sidecar_path.write_text(
                json.dumps(sidecar_data, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass  # Non-fatal: result was written, sidecar is best-effort

    return receipt


@router.get("/data/{aid}/results/{sid}.json")
async def get_result_json(
    aid: str,
    sid: str,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Any:
    """Retrieve a stored grading result JSON."""
    storage_path = _get_storage_path()
    aid = _validate_path_segment(aid, "assignment_id")
    sid = _validate_path_segment(sid, "submission_id")
    results_dir = validate_path_within_storage(storage_path, "_data", aid, "results")
    target = results_dir / f"{sid}.json"
    if not target.exists():
        raise HTTPException(status_code=404, detail="Result not found")
    return json.loads(target.read_bytes())


@router.get("/data/{aid}/results/")
async def list_results(
    aid: str,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """List all stored result files for an assignment."""
    storage_path = _get_storage_path()
    aid = _validate_path_segment(aid, "assignment_id")
    results_dir = validate_path_within_storage(storage_path, "_data", aid, "results")
    files: List[str] = []
    if results_dir.exists() and results_dir.is_dir():
        files = sorted(f.name for f in results_dir.glob("*.json"))
    return {"assignment_id": aid, "results": files}


@router.put("/data/{aid}/assignment.json")
async def put_assignment_json(
    aid: str,
    request: Request,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Store assignment metadata JSON with atomic write and write receipt."""
    storage_path = _get_storage_path()
    data_dir = _data_dir(storage_path, aid)
    body = await _read_json_body(request)
    content = json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8")
    target = data_dir / "assignment.json"
    return _write_json_atomic(target, content)


@router.get("/data/{aid}/assignment.json")
async def get_assignment_json(
    aid: str,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Any:
    """Retrieve stored assignment metadata JSON."""
    storage_path = _get_storage_path()
    data_dir = _data_dir(storage_path, aid)
    target = data_dir / "assignment.json"
    if not target.exists():
        raise HTTPException(status_code=404, detail="Assignment metadata not found")
    return json.loads(target.read_bytes())


# ---------------------------------------------------------------------------
# Grading Bundle Endpoint
# ---------------------------------------------------------------------------


@router.post("/grading-bundle/{aid}/{sid}")
async def create_grading_bundle(
    aid: str,
    sid: str,
    request: Request,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Generate a grading input bundle from the submission PDF."""
    storage_path = _get_storage_path()
    aid = _validate_path_segment(aid, "assignment_id")
    sid = _validate_path_segment(sid, "submission_id")

    body = await _read_json_body(request)
    strategy = body.get("strategy", "text_only")
    dpi = body.get("dpi", 150)
    max_pages = body.get("max_pages")
    force_refresh = body.get("force_refresh", False)

    # Validate strategy
    if strategy not in ("text_only", "multimodal", "smart"):
        raise HTTPException(status_code=400, detail=f"Invalid strategy: {strategy}")

    pdf_path = validate_path_within_storage(storage_path, aid, sid, "submission.pdf")
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Submission PDF not found")

    cache_dir = Path(storage_path) / "_cache" / "bundles" / aid / sid

    from aems_agent.grading_bundle import generate_bundle

    bundle = generate_bundle(
        pdf_path,
        strategy=strategy,
        dpi=dpi,
        max_pages=max_pages,
        cache_dir=cache_dir,
        force_refresh=force_refresh,
    )
    bundle["assignment_id"] = aid
    bundle["submission_id"] = sid
    return bundle


# ---------------------------------------------------------------------------
# Annotation Generation Endpoint
# ---------------------------------------------------------------------------


@router.post("/annotate/{assignment_id}/{submission_id}")
async def annotate_submission(
    assignment_id: str,
    submission_id: str,
    force: bool = False,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Generate annotated PDF from stored results and submission PDF."""
    from aems_pdf_annotator.contract import ContractValidationError

    from .annotate import generate_annotated_pdf

    config = _get_config()
    if not config.storage_path:
        raise HTTPException(status_code=400, detail="Storage path not configured")

    # Validate path components
    _validate_path_segment(assignment_id, "assignment_id")
    _validate_path_segment(submission_id, "submission_id")

    storage = Path(config.storage_path)
    try:
        result = generate_annotated_pdf(storage, assignment_id, submission_id, force=force)
        return result
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ContractValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("Annotation generation failed")
        raise HTTPException(status_code=500, detail="Annotation generation failed")


# ---------------------------------------------------------------------------
# Annotation CRUD Endpoints
# ---------------------------------------------------------------------------


def _require_annotation_contract_version(request: Request) -> None:
    """Require ``X-AEMS-Annotation-Contract-Version: 1`` on annotation CRUD requests."""
    value = request.headers.get("X-AEMS-Annotation-Contract-Version")
    if value != "1":
        raise HTTPException(status_code=409, detail="Unsupported annotation contract version")


@router.get("/annotations/{aid}/{sid}")
async def list_annotations(
    aid: str,
    sid: str,
    request: Request,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """List all annotations in the annotated PDF."""
    _require_annotation_contract_version(request)
    storage_path = _get_storage_path()
    aid = _validate_path_segment(aid, "assignment_id")
    sid = _validate_path_segment(sid, "submission_id")
    pdf_path = _annotated_pdf_path(storage_path, aid, sid)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Annotated PDF not found")
    return annotation_crud.list_annotations(pdf_path)


@router.get("/annotations/{aid}/{sid}/version")
async def get_annotation_version(
    aid: str,
    sid: str,
    request: Request,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Return a version token for the annotated PDF."""
    _require_annotation_contract_version(request)
    storage_path = _get_storage_path()
    aid = _validate_path_segment(aid, "assignment_id")
    sid = _validate_path_segment(sid, "submission_id")
    pdf_path = _annotated_pdf_path(storage_path, aid, sid)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Annotated PDF not found")
    return annotation_crud.get_version(pdf_path)


@router.post("/annotations/{aid}/{sid}", status_code=201)
async def create_annotation(
    aid: str,
    sid: str,
    request: Request,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Add a new annotation to the annotated PDF."""
    _require_annotation_contract_version(request)
    storage_path = _get_storage_path()
    aid = _validate_path_segment(aid, "assignment_id")
    sid = _validate_path_segment(sid, "submission_id")
    pdf_path = _annotated_pdf_path(storage_path, aid, sid)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Annotated PDF not found")
    payload = await _read_json_body(request)
    try:
        return annotation_crud.add_annotation(pdf_path, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/annotations/{aid}/{sid}/{annot_id:path}")
async def update_annotation_route(
    aid: str,
    sid: str,
    annot_id: str,
    request: Request,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Update an existing annotation in the annotated PDF."""
    _require_annotation_contract_version(request)
    storage_path = _get_storage_path()
    aid = _validate_path_segment(aid, "assignment_id")
    sid = _validate_path_segment(sid, "submission_id")
    pdf_path = _annotated_pdf_path(storage_path, aid, sid)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Annotated PDF not found")
    payload = await _read_json_body(request)
    try:
        return annotation_crud.update_annotation(pdf_path, annot_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Annotation not found")


@router.delete("/annotations/{aid}/{sid}/{annot_id:path}")
async def delete_annotation_route(
    aid: str,
    sid: str,
    annot_id: str,
    request: Request,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Delete an annotation from the annotated PDF."""
    _require_annotation_contract_version(request)
    storage_path = _get_storage_path()
    aid = _validate_path_segment(aid, "assignment_id")
    sid = _validate_path_segment(sid, "submission_id")
    pdf_path = _annotated_pdf_path(storage_path, aid, sid)
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail="Annotated PDF not found")
    try:
        return annotation_crud.delete_annotation(pdf_path, annot_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Annotation not found")


# ---------------------------------------------------------------------------
# Canvas Download Endpoints
# ---------------------------------------------------------------------------


class EncryptedPayload(BaseModel):
    """Request body for encrypted manifest submission."""

    encrypted_payload: str = Field(..., description="Base64-encoded NaCl SealedBox ciphertext")


@router.post("/canvas/download-submissions")
async def canvas_download_submissions(
    body: EncryptedPayload,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Decrypt manifest, create a download job, and return job metadata.

    The encrypted payload contains a JSON manifest with Canvas download URLs.
    The agent decrypts it using its private key, validates the manifest, and
    starts a background download task. Returns 202-style response with job_id.
    """
    import base64

    from .canvas_download import (
        ManifestValidationError,
        create_download_job,
        run_download_job,
        validate_manifest,
    )
    from .crypto import decrypt_sealed_box, get_key_id

    config = _get_config()
    if _config_dir is None:
        raise HTTPException(status_code=500, detail="Agent config not initialized")

    # Decrypt
    try:
        ciphertext = base64.b64decode(body.encrypted_payload)
        plaintext = decrypt_sealed_box(_config_dir, ciphertext)
        manifest = json.loads(plaintext)
    except Exception:
        raise HTTPException(status_code=400, detail="Failed to decrypt manifest")

    # Canvas SaaS hosts are allowed by validate_manifest() via the Instructure wildcard.
    # Self-hosted Canvas instances must be explicitly allowlisted in config.
    allowed_hosts: list[str] = list(config.canvas_allowed_hosts)

    agent_key_id = get_key_id(_config_dir)

    try:
        validate_manifest(manifest, allowed_hosts=allowed_hosts, agent_key_id=agent_key_id)
    except ManifestValidationError as e:
        logger.warning("Manifest validation failed: %s", e)
        raise HTTPException(status_code=403, detail="Manifest validation failed")

    # Create job and start background download
    if not config.storage_path:
        raise HTTPException(status_code=503, detail="Storage path not configured")

    job_id = create_download_job(manifest)
    asyncio.create_task(
        run_download_job(
            job_id=job_id,
            manifest=manifest,
            storage_path=Path(config.storage_path),
        )
    )

    return {
        "job_id": job_id,
        "status": "pending",
        "total_submissions": len(manifest["submissions"]),
    }


@router.get("/canvas/download-jobs/{job_id}")
async def get_canvas_download_job(
    job_id: str,
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Return current download job status and per-submission progress."""
    from .canvas_download import get_download_job

    job = get_download_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Unknown download job")

    return {
        "job_id": job_id,
        "status": job.status,
        "downloaded": job.downloaded,
        "skipped": job.skipped,
        "failed": job.failed,
        "total_submissions": job.total_submissions,
        "per_submission": job.per_submission,
    }


# ---------------------------------------------------------------------------
# Pairing Endpoints (no auth required — challenge-based)
# ---------------------------------------------------------------------------


class PairInitiateRequest(BaseModel):
    """Request body for pairing initiation."""

    origin: str = Field(..., description="Browser origin requesting pairing")


class PairCompleteRequest(BaseModel):
    """Request body for pairing completion."""

    challenge_id: str = Field(..., description="Challenge ID from initiate step")
    origin: str = Field(..., description="Browser origin requesting pairing")
    pin: str = Field(..., min_length=6, max_length=6, pattern=r"^\d{6}$")


@router.post("/pair/initiate")
async def pair_initiate(
    body: PairInitiateRequest,
    request: Request,
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """
    Initiate pairing — no auth required.

    Returns a challenge that the browser must complete within 120 seconds.
    Only one active challenge at a time.
    """
    global _pairing_challenge

    # Rate limit pairing attempts (3 per minute)
    client_ip = request.client.host if request.client else "unknown"
    if not _pairing_rate_limiter.is_allowed(client_ip):
        raise HTTPException(status_code=429, detail="Too many pairing attempts")
    now = time.time()
    _ensure_pairing_not_locked(now)

    origin_header = _normalize_origin(request.headers.get("origin"))
    origin_body = _normalize_origin(body.origin)
    if not origin_header or not origin_body:
        raise HTTPException(status_code=400, detail="Invalid origin")
    if not secrets.compare_digest(origin_header, origin_body):
        raise HTTPException(status_code=403, detail="Origin header mismatch")

    config = _get_config()
    challenge_id = secrets.token_urlsafe(32)
    pin = f"{random.SystemRandom().randint(0, 999999):06d}"

    async with _pairing_lock:
        if _pairing_challenge and now <= float(_pairing_challenge["expires_at"]):
            expires_in = max(1, int(float(_pairing_challenge["expires_at"]) - now))
            return JSONResponse(
                status_code=409,
                content={"detail": "Pairing already in progress", "expires_in": expires_in},
            )
        _pairing_challenge = {
            "challenge_id": challenge_id,
            "origin": origin_header,
            "pin": pin,
            "created_at": now,
            "expires_at": now + _PAIRING_CHALLENGE_TTL_SECONDS,
        }

    logger.debug("Pairing PIN generated (origin: %s)", origin_header)
    _maybe_echo_pairing_pin(pin, origin_header)

    # Put the PIN on the OS clipboard so the user can paste it directly
    # into the AEMS web UI -- the tray toast is non-interactive, so there
    # is no other way to copy from it.
    clipboard_ok = _copy_pin_to_clipboard(pin)

    # Tray notification if available
    _notify_pairing_pin(request, pin, clipboard_ok)

    return {
        "challenge_id": challenge_id,
        "agent_name": f"AEMS Agent ({config.host}:{config.port})",
        "expires_in": int(_PAIRING_CHALLENGE_TTL_SECONDS),
        "requires_pin": True,
    }


@router.post("/pair/complete")
async def pair_complete(
    body: PairCompleteRequest,
    request: Request,
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """
    Complete pairing — validates challenge and returns an auth token.

    The challenge is single-use and expires after 120 seconds.
    """
    global _pairing_challenge

    # Rate limit
    client_ip = request.client.host if request.client else "unknown"
    if not _pairing_rate_limiter.is_allowed(client_ip):
        raise HTTPException(status_code=429, detail="Too many pairing attempts")
    now = time.time()
    _ensure_pairing_not_locked(now)

    origin_header = _normalize_origin(request.headers.get("origin"))
    origin_body = _normalize_origin(body.origin)
    if not origin_header or not origin_body:
        raise HTTPException(status_code=403, detail=_PAIRING_FAILURE_DETAIL)
    if not secrets.compare_digest(origin_header, origin_body):
        raise HTTPException(status_code=403, detail=_PAIRING_FAILURE_DETAIL)

    async with _pairing_lock:
        if not _pairing_challenge:
            raise HTTPException(status_code=403, detail=_PAIRING_FAILURE_DETAIL)

        # Check expiry
        if now > float(_pairing_challenge["expires_at"]):
            _pairing_challenge = None
            _reset_pairing_failures()
            raise HTTPException(status_code=410, detail="Pairing challenge expired")

        # Validate challenge ID (constant-time comparison)
        if not secrets.compare_digest(body.challenge_id, _pairing_challenge["challenge_id"]):
            raise HTTPException(status_code=403, detail=_PAIRING_FAILURE_DETAIL)

        # Bind completion to the same browser origin that initiated pairing.
        expected_origin = str(_pairing_challenge.get("origin") or "")
        if not secrets.compare_digest(origin_header, expected_origin):
            raise HTTPException(status_code=403, detail=_PAIRING_FAILURE_DETAIL)

        # Validate PIN (constant-time comparison)
        if not secrets.compare_digest(body.pin, _pairing_challenge["pin"]):
            _pairing_challenge = None
            if _record_failed_pin_attempt(now):
                raise HTTPException(status_code=429, detail="Pairing temporarily locked")
            raise HTTPException(status_code=403, detail=_PAIRING_FAILURE_DETAIL)

        # Consume the challenge (single-use)
        _pairing_challenge = None
        _reset_pairing_failures()

    # Add origin to paired_origins, persist, and update live CORS list
    config = _get_config()
    if origin_header not in config.paired_origins:
        config.paired_origins.append(origin_header)
        save_config(config, _config_dir)
    cors_origins: list[str] | None = getattr(request.app.state, "cors_origins", None)
    if cors_origins is not None and origin_header not in cors_origins:
        cors_origins.append(origin_header)

    # Return the auth token
    return {
        "token": _auth_token,
        "message": "Pairing successful",
    }


@router.get("/pair/confirm")
async def pair_confirm() -> Dict[str, Any]:
    """
    Check pairing status without exposing the operator PIN.

    Note: no _pairing_lock needed — asyncio single-threaded event loop
    provides atomicity between await points, and this handler has none.
    """
    challenge = _pairing_challenge
    if not challenge:
        return {"active": False}

    now = time.time()
    if now > float(challenge["expires_at"]):
        return {"active": False}

    return {
        "active": True,
        "expires_in": int(float(challenge["expires_at"]) - now),
    }


def _copy_pin_to_clipboard(pin: str) -> bool:
    """Best-effort: place the pairing PIN on the OS clipboard.

    Returns True if the clipboard was updated, False otherwise.  Failure
    is silent -- the PIN is still in the tray toast and may also be echoed
    to an interactive terminal.
    """
    import platform
    import subprocess

    system = platform.system()
    try:
        if system == "Windows":
            # `clip.exe` is built into Windows and takes stdin verbatim.
            subprocess.run(["clip"], input=pin, encoding="utf-16-le", check=True, timeout=5)
            return True
        if system == "Darwin":
            subprocess.run(["pbcopy"], input=pin, text=True, check=True, timeout=5)
            return True
        if system == "Linux":
            # Try wl-copy (Wayland) then xclip (X11); both are common on
            # desktop distros.  If neither is installed, fall through.
            for argv in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
                try:
                    subprocess.run(argv, input=pin, text=True, check=True, timeout=5)
                    return True
                except (FileNotFoundError, subprocess.CalledProcessError):
                    continue
    except Exception as e:  # pragma: no cover -- platform-dependent
        logger.debug("Clipboard copy failed: %s", e)
    return False


def _notify_pairing_pin(request: Request, pin: str, clipboard_ok: bool = False) -> None:
    """Send tray notification with pairing PIN if tray notifier is available."""
    notifier = getattr(request.app.state, "tray_notifier", None)
    if notifier is not None:
        try:
            notifier(pin, clipboard_ok)
        except TypeError:
            # Older tray notifiers don't know about the clipboard flag yet.
            try:
                notifier(pin)
            except Exception as e:  # pragma: no cover
                logger.debug("Tray notification failed: %s", e)
        except Exception as e:  # pragma: no cover
            logger.debug("Tray notification failed: %s", e)
