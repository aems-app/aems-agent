"""Shared fixtures for AEMS Local Bridge Agent tests."""

import pytest
from pathlib import Path
from typing import Generator

from aems_agent.config import AgentConfig, save_config, ensure_auth_token


@pytest.fixture(autouse=True)
def _reset_route_state() -> Generator:
    """Reset module-level pairing state, rate limiters, and download jobs between tests."""
    from aems_agent import routes
    from aems_agent.canvas_download import _download_jobs

    routes._pairing_challenge = None
    routes._pairing_lockout_until = 0.0
    routes._pairing_failed_pin_count = 0
    routes._pairing_failed_pin_window_started_at = 0.0
    routes._rate_limiter.reset()
    routes._pairing_rate_limiter.reset()
    _download_jobs.clear()
    yield
    routes._pairing_challenge = None
    routes._pairing_lockout_until = 0.0
    routes._pairing_failed_pin_count = 0
    routes._pairing_failed_pin_window_started_at = 0.0
    routes._rate_limiter.reset()
    routes._pairing_rate_limiter.reset()
    _download_jobs.clear()


@pytest.fixture
def tmp_storage_path(tmp_path: Path) -> Path:
    """Create a temporary storage directory."""
    storage = tmp_path / "exams"
    storage.mkdir()
    return storage


@pytest.fixture
def agent_config(tmp_path: Path, tmp_storage_path: Path) -> AgentConfig:
    """Create an agent config with temporary paths."""
    config = AgentConfig(
        storage_path=str(tmp_storage_path),
        port=61234,
        host="127.0.0.1",
        allowed_origins=["http://127.0.0.1:8080"],
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    save_config(config, config_dir)
    return config


@pytest.fixture
def agent_config_dir(tmp_path: Path, agent_config: AgentConfig) -> Path:
    """Return the config directory path."""
    config_dir = tmp_path / "config"
    return config_dir


@pytest.fixture
def agent_token(agent_config_dir: Path) -> str:
    """Ensure and return the auth token."""
    return ensure_auth_token(agent_config_dir)


@pytest.fixture
def agent_client(agent_config_dir: Path, agent_token: str) -> Generator:
    """Create a FastAPI TestClient for the agent."""
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        pytest.skip("httpx/fastapi not installed (install with: pip install aems-agent)")
        return

    from aems_agent.app import create_app

    app = create_app(config_dir=agent_config_dir)
    client = TestClient(app, base_url="http://127.0.0.1:61234")
    yield client


@pytest.fixture
def auth_headers(agent_token: str) -> dict:
    """Return authorization headers with the agent token."""
    return {"Authorization": f"Bearer {agent_token}"}


@pytest.fixture
def sample_pdf() -> bytes:
    """Return minimal valid PDF bytes for testing."""
    return b"%PDF-1.4 minimal test PDF content for AEMS agent testing"
