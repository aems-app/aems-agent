# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Platform-aware configuration management for the AEMS Local Bridge Agent.

Config directory:
    - Windows: %APPDATA%\\AEMS\\agent\\
    - Linux/Mac: ~/.config/aems/agent/

Stores:
    - config.json: storage_path, port, allowed_origins
    - auth_token: bearer token for API authentication
"""

import json
import logging
import os
import platform
import secrets
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

try:
    from importlib.metadata import version as _pkg_version

    AGENT_VERSION = _pkg_version("aems-agent")
except Exception:
    AGENT_VERSION = "0.0.0-dev"
API_VERSION = "1.0.0"
MIN_CLIENT_API_VERSION = "1.0.0"


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
        default_factory=lambda: ["http://127.0.0.1:8080", "http://localhost:8080", "https://api.aems.app"],
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
    if config_file.exists():
        try:
            data = json.loads(config_file.read_text(encoding="utf-8"))
            return AgentConfig(**data)
        except Exception as e:
            logger.warning("Failed to load config from %s: %s", config_file, e)

    return AgentConfig()


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
    config_file.write_text(
        json.dumps(config.model_dump(mode="json"), indent=2),
        encoding="utf-8",
    )
    try:
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
            return token

    token = secrets.token_urlsafe(32)
    token_file.write_text(token, encoding="utf-8")

    # Restrict file permissions (owner-only on Unix)
    try:
        token_file.chmod(0o600)
    except OSError:
        pass  # Best-effort; Windows ACLs handled differently

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
