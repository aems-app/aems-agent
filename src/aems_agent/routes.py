"""
FastAPI router with all AEMS Local Bridge Agent endpoints.

Endpoint summary:
    GET  /status                                        - Alive check (no auth)
    GET  /capabilities                                  - Discovery / public key (no auth)
    GET  /health                                        - Detailed health (auth)
    GET  /config/path                                   - Get storage path (auth)
    PUT  /config/path                                   - Set storage path (auth)
    GET  /files/{assignment_id}                         - List submissions (auth)
    GET  /files/{assignment_id}/{submission_id}          - Download PDF (auth)
    PUT  /files/{assignment_id}/{submission_id}          - Store PDF (auth)
    DELETE /files/{assignment_id}/{submission_id}        - Delete PDF (auth)
    GET  /files/{assignment_id}/{submission_id}/annotated - Download annotated (auth)
    PUT  /files/{assignment_id}/{submission_id}/annotated - Store annotated (auth)
"""

import asyncio
import hashlib
import logging
import os
import random
import re
import secrets
import shutil
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from .config import AGENT_VERSION, API_VERSION, MIN_CLIENT_API_VERSION, AgentConfig, load_config, save_config
from .security import RateLimiter, validate_path_within_storage

logger = logging.getLogger(__name__)

router = APIRouter()

# Module-level rate limiter (100 req/min)
_rate_limiter = RateLimiter(max_requests=100, window_seconds=60.0)

# Maximum upload size: 200 MB (exam PDFs can be large with images)
_MAX_UPLOAD_BYTES = 200 * 1024 * 1024

# These will be set by app.py at startup
_config_dir: Optional[Path] = None
_auth_token: Optional[str] = None

# Pairing state (in-memory, single active challenge)
_pairing_challenge: Optional[Dict[str, Any]] = None
_pairing_lock = asyncio.Lock()
_pairing_rate_limiter = RateLimiter(max_requests=3, window_seconds=60.0)

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


def _compute_sha256(data: bytes) -> str:
    """Compute SHA-256 hex digest of data."""
    return hashlib.sha256(data).hexdigest()


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
    if parsed.path not in ("", "/"):
        return None
    if parsed.params or parsed.query or parsed.fragment:
        return None

    host = parsed.hostname.lower()
    port = parsed.port
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
    config = _get_config()
    return {
        "status": "ok",
        "service": "aems-agent",
        "version": AGENT_VERSION,
        "api_version": API_VERSION,
        "min_client_version": MIN_CLIENT_API_VERSION,
        "storage_configured": bool(config.storage_path),
    }


@router.get("/capabilities")
async def get_capabilities() -> Dict[str, Any]:
    """Return agent version, supported contract versions, and encryption key."""
    import base64

    from .crypto import get_key_id, load_public_key

    if _config_dir is None:
        raise HTTPException(status_code=500, detail="Agent config not initialized")
    return {
        "agent_version": AGENT_VERSION,
        "supported_contract_versions": [1],
        "features": ["file_storage", "canvas_download"],
        "encryption_key_id": get_key_id(_config_dir),
        "public_key_base64": base64.b64encode(
            load_public_key(_config_dir)
        ).decode(),
    }


@router.get("/health")
async def health(
    _token: str = Depends(_verify_token),
    _rl: None = Depends(_check_rate_limit),
) -> Dict[str, Any]:
    """Detailed health check with disk space and storage info."""
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

    data = pdf_path.read_bytes()
    sha256 = _compute_sha256(data)

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=f"submission_{submission_id}.pdf",
        headers={"X-SHA256": sha256},
    )


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

    data = pdf_path.read_bytes()
    sha256 = _compute_sha256(data)

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
        _pairing_challenge = {
            "challenge_id": challenge_id,
            "origin": origin_header,
            "pin": pin,
            "created_at": time.time(),
            "expires_at": time.time() + 120,
        }

    # Print PIN to console for operator confirmation
    logger.debug("Pairing PIN generated (origin: %s)", origin_header)
    print(f"\n{'=' * 40}")
    print(f"  PAIRING PIN: {pin}")
    print(f"  Origin: {origin_header}")
    print(f"{'=' * 40}\n")

    # Tray notification if available
    _notify_pairing_pin(request, pin)

    return {
        "challenge_id": challenge_id,
        "agent_name": f"AEMS Agent ({config.host}:{config.port})",
        "storage_configured": config.storage_path is not None,
        "expires_in": 120,
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

    origin_header = _normalize_origin(request.headers.get("origin"))
    origin_body = _normalize_origin(body.origin)
    if not origin_header or not origin_body:
        async with _pairing_lock:
            _pairing_challenge = None
        raise HTTPException(status_code=400, detail="Invalid origin")
    if not secrets.compare_digest(origin_header, origin_body):
        async with _pairing_lock:
            _pairing_challenge = None
        raise HTTPException(status_code=403, detail="Origin header mismatch")

    async with _pairing_lock:
        if not _pairing_challenge:
            raise HTTPException(status_code=400, detail="No active pairing challenge")

        # Check expiry
        if time.time() > _pairing_challenge["expires_at"]:
            _pairing_challenge = None
            raise HTTPException(status_code=410, detail="Pairing challenge expired")

        # Validate challenge ID (constant-time comparison)
        if not secrets.compare_digest(body.challenge_id, _pairing_challenge["challenge_id"]):
            _pairing_challenge = None
            raise HTTPException(status_code=403, detail="Invalid challenge ID")

        # Bind completion to the same browser origin that initiated pairing.
        expected_origin = str(_pairing_challenge.get("origin") or "")
        if not secrets.compare_digest(origin_header, expected_origin):
            _pairing_challenge = None
            raise HTTPException(status_code=403, detail="Origin mismatch for pairing challenge")

        # Validate PIN (constant-time comparison)
        if not secrets.compare_digest(body.pin, _pairing_challenge["pin"]):
            _pairing_challenge = None
            raise HTTPException(status_code=403, detail="Invalid PIN")

        # Consume the challenge (single-use)
        _pairing_challenge = None

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
    Check pairing status — returns active challenge info (PIN + origin).

    No auth required (localhost-only service). Used by tray/UI to display
    the PIN for operator confirmation.

    Note: no _pairing_lock needed — asyncio single-threaded event loop
    provides atomicity between await points, and this handler has none.
    """
    if not _pairing_challenge:
        return {"active": False}

    now = time.time()
    if now > _pairing_challenge["expires_at"]:
        return {"active": False}

    return {
        "active": True,
        "pin": _pairing_challenge["pin"],
        "origin": _pairing_challenge.get("origin", ""),
        "expires_in": int(_pairing_challenge["expires_at"] - now),
    }


def _notify_pairing_pin(request: Request, pin: str) -> None:
    """Send tray notification with pairing PIN if tray notifier is available."""
    notifier = getattr(request.app.state, "tray_notifier", None)
    if notifier is not None:
        try:
            notifier(pin)
        except Exception as e:
            logger.debug("Tray notification failed: %s", e)
