# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Platform-aware configuration management for the AEMS Local Bridge Agent.

Config directory:
    - Windows: %APPDATA%\\AEMS\\agent\\
    - macOS: ~/Library/Application Support/AEMS/agent/
    - Linux: ~/.config/aems/agent/

Stores:
    - config.json: storage_path, port, allowed_origins
    - auth_token: bearer token for API authentication
"""

import json
import ipaddress
import logging
import os
import platform
import secrets
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)


def _resolve_agent_version() -> str:
    """Best-effort lookup of the agent version.

    1. importlib.metadata works for normal pip installs.
    2. In PyInstaller bundles the dist-info is missing, so fall back to the
       packaged ``_version.txt`` written at build time.
    3. As a last resort return ``0.0.0-dev``.
    """
    try:
        from importlib.metadata import version as _pkg_version

        return _pkg_version("aems-agent")
    except Exception:
        pass
    try:
        from pathlib import Path

        version_file = Path(__file__).parent / "_version.txt"
        if version_file.exists():
            text = version_file.read_text(encoding="utf-8").strip()
            if text:
                return text
    except Exception:
        pass
    return "0.0.0-dev"


AGENT_VERSION = _resolve_agent_version()
API_VERSION = "1.0.0"
MIN_CLIENT_API_VERSION = "1.0.0"


class ConfigLoadError(RuntimeError):
    """Raised when an existing config file cannot be loaded safely."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Cannot load agent config {path}: {reason}. The file was not changed.")


def normalize_canvas_host(value: str) -> str:
    """Return one canonical Canvas hostname or raise a validation error."""
    host = value.strip().lower().rstrip(".")
    if not host:
        raise ValueError("Canvas host must not be empty")
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if any(character in host for character in (":", "/", "\\", "?", "#", "@")):
        raise ValueError("Canvas host must be a hostname only, without scheme, port, or path")
    if len(host) > 253:
        raise ValueError("Canvas hostname is too long")
    labels = host.split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(
            character.isascii() and (character.isalnum() or character == "-") for character in label
        )
        for label in labels
    ):
        raise ValueError("Canvas host is not a valid hostname or IP address")
    return host


def get_config_dir() -> Path:
    """Return the platform-specific config directory for the agent."""
    system = platform.system()
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "AEMS" / "agent"
        return Path.home() / "AppData" / "Roaming" / "AEMS" / "agent"
    elif system == "Darwin":
        new_path = Path.home() / "Library" / "Application Support" / "AEMS" / "agent"
        old_path = Path.home() / ".config" / "aems" / "agent"
        if old_path.exists() and not new_path.exists():
            import shutil

            try:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(str(old_path), str(new_path))
                logger.info("Migrated config from %s to %s", old_path, new_path)
            except (OSError, shutil.Error) as e:
                logger.warning("Config migration failed: %s", e)
        return new_path
    else:
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config:
            return Path(xdg_config) / "aems" / "agent"
        return Path.home() / ".config" / "aems" / "agent"


class AgentConfig(BaseModel):
    """Configuration model for the AEMS Local Bridge Agent."""

    storage_path: Optional[str] = Field(
        default=None,
        description="Absolute path to the local storage directory (e.g., D:\\Exams)",
    )
    port: int = Field(
        default=61234,
        ge=1024,
        le=65535,
        description="Port to listen on (default 61234)",
    )
    host: str = Field(
        default="127.0.0.1",
        description="Host to bind to (default localhost only)",
    )
    allowed_origins: List[str] = Field(
        default_factory=lambda: [
            "http://127.0.0.1:8080",
            "http://localhost:8080",
            "https://api.aems.app",
        ],
        description="CORS allowed origins",
    )
    paired_origins: List[str] = Field(
        default_factory=list,
        description="Origins that have completed pairing (auto-populated)",
    )
    canvas_allowed_hosts: List[str] = Field(
        default_factory=list,
        description="Extra Canvas hostnames allowed in download manifests (for self-hosted Canvas instances)",
    )

    @field_validator("storage_path")
    @classmethod
    def validate_storage_path(cls, v: Optional[str]) -> Optional[str]:
        """Validate storage path is absolute if provided."""
        if v is not None:
            path = Path(v)
            if not path.is_absolute():
                raise ValueError(f"Storage path must be absolute: {v}")
        return v

    @field_validator("canvas_allowed_hosts")
    @classmethod
    def validate_canvas_allowed_hosts(cls, values: List[str]) -> List[str]:
        """Normalize and de-duplicate configured self-hosted Canvas domains."""
        return list(dict.fromkeys(normalize_canvas_host(value) for value in values))


def _write_owner_only_text(path: Path, content: str) -> None:
    """Write *content* to *path*, created owner-only (0600) from the start.

    Avoids the write-then-chmod window where the file is briefly readable
    by other local users. On Windows the POSIX mode is ignored and the file
    inherits ACLs from its parent directory, matching previous behaviour.

    ``O_BINARY`` (0 on POSIX) keeps the CRT from doing its own newline
    translation on Windows; ``newline=""`` keeps the text layer from doing
    it too, so the bytes written are exactly ``content`` on every platform.
    """
    tmp_path = path.with_name(f".{path.name}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(str(tmp_path), flags, 0o600)
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8", newline="")
    except BaseException:
        os.close(fd)  # fdopen didn't take ownership of the fd; close it ourselves
        raise
    with handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def load_config(config_dir: Optional[Path] = None) -> AgentConfig:
    """
    Load agent configuration from disk.

    Args:
        config_dir: Override config directory (for testing).

    Returns:
        AgentConfig instance with loaded or default values.
    """
    if config_dir is None:
        config_dir = get_config_dir()

    config_file = config_dir / "config.json"
    if not config_file.exists():
        return AgentConfig()

    try:
        raw = config_file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ConfigLoadError(config_file, "file could not be read as UTF-8") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigLoadError(config_file, "invalid JSON") from exc
    if not isinstance(data, dict):
        raise ConfigLoadError(config_file, "JSON root must be an object")
    try:
        return AgentConfig(**data)
    except (ValidationError, TypeError, ValueError) as exc:
        raise ConfigLoadError(config_file, "configuration values failed validation") from exc


def save_config(config: AgentConfig, config_dir: Optional[Path] = None) -> None:
    """
    Save agent configuration to disk.

    Args:
        config: AgentConfig instance to persist.
        config_dir: Override config directory (for testing).
    """
    if config_dir is None:
        config_dir = get_config_dir()

    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.json"
    _write_owner_only_text(
        config_file,
        json.dumps(config.model_dump(mode="json"), indent=2),
    )
    try:
        # Tighten permissions on files created by older agent versions; the
        # O_CREAT mode above only applies when the file is first created.
        config_file.chmod(0o600)
    except OSError:
        pass  # Best-effort; Windows ACLs handled differently


def ensure_auth_token(config_dir: Optional[Path] = None) -> str:
    """
    Ensure an auth token exists, creating one if needed.

    Args:
        config_dir: Override config directory (for testing).

    Returns:
        The bearer token string.
    """
    if config_dir is None:
        config_dir = get_config_dir()

    config_dir.mkdir(parents=True, exist_ok=True)
    token_file = config_dir / "auth_token"

    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            try:
                # Tighten permissions on tokens created by older versions.
                token_file.chmod(0o600)
            except OSError:
                pass  # Best-effort; Windows ACLs handled differently
            return token

    token = secrets.token_urlsafe(32)
    # Created owner-only (0600) atomically — never world-readable, even
    # briefly, on multi-user machines.
    _write_owner_only_text(token_file, token)

    return token


def get_auth_token(config_dir: Optional[Path] = None) -> Optional[str]:
    """
    Read the existing auth token without creating one.

    Args:
        config_dir: Override config directory (for testing).

    Returns:
        The bearer token string or None if not yet created.
    """
    if config_dir is None:
        config_dir = get_config_dir()

    token_file = config_dir / "auth_token"
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if token:
            return token
    return None
