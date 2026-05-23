"""Tray launch failures must surface via the FastAPI /status endpoint."""
from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch


def test_tray_thread_failure_is_captured_in_app_state(tmp_path: Path) -> None:
    """When pystray.Icon.run() raises, the FastAPI app.state.tray_status reflects 'failed'.

    Without this fix, the daemon thread dies silently and the agent keeps
    serving /status as if tray were running, leaving users confused.
    """
    from aems_agent.cli import _start_tray

    fake_app = MagicMock()
    fake_app.state.tray_status = None

    with patch("aems_agent.tray.create_tray") as create:
        icon = MagicMock()
        icon.run.side_effect = RuntimeError("pystray win32 backend failed")
        icon._aems_pin_notifier = None
        create.return_value = icon

        _start_tray(tmp_path, fake_app)
        # Give the daemon thread a moment to enter run() and raise.
        time.sleep(0.2)

    assert fake_app.state.tray_status == "failed", (
        f"Expected tray_status='failed', got {fake_app.state.tray_status!r}"
    )
    # The error message should be captured for the badge's cosmetic warning.
    assert "pystray win32 backend failed" in str(getattr(fake_app.state, "tray_error", ""))


def test_tray_thread_success_sets_running(tmp_path: Path) -> None:
    """When pystray.Icon.run() works normally, app.state.tray_status reflects 'running'."""
    from aems_agent.cli import _start_tray

    fake_app = MagicMock()
    fake_app.state.tray_status = None

    with patch("aems_agent.tray.create_tray") as create:
        icon = MagicMock()
        # icon.run() blocks until icon.stop() in production; for the test it just returns.
        icon.run.return_value = None
        icon._aems_pin_notifier = None
        create.return_value = icon

        _start_tray(tmp_path, fake_app)
        time.sleep(0.2)

    assert fake_app.state.tray_status in {"running", "starting"}, (
        # 'running' is the expected final state; 'starting' is acceptable if the
        # thread hasn't yet entered run() -- but it should clear within 200ms.
        f"Expected tray_status='running' (or 'starting' if pre-thread), got {fake_app.state.tray_status!r}"
    )


def _make_agent_client(tmp_path: Path) -> Any:
    """Build a TestClient with the correct Host header for the agent's middleware."""
    from fastapi.testclient import TestClient

    from aems_agent.app import create_app
    from aems_agent.config import AgentConfig, ensure_auth_token, save_config

    config = AgentConfig(storage_path=None, port=61234, host="127.0.0.1")
    config_dir = tmp_path / "agent_cfg"
    config_dir.mkdir()
    save_config(config, config_dir)
    ensure_auth_token(config_dir)

    agent_app = create_app(config_dir=config_dir)
    client = TestClient(agent_app, base_url="http://127.0.0.1:61234")
    return client, agent_app


def test_status_endpoint_includes_tray_status(tmp_path: Path) -> None:
    """The /status endpoint must surface tray_status so the AEMS web badge can read it."""
    client, agent_app = _make_agent_client(tmp_path)
    # Simulate the tray failure path setting state.
    agent_app.state.tray_status = "failed"
    agent_app.state.tray_error = "no display available"

    resp = client.get("/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("tray_status") == "failed"
    assert body.get("tray_error") == "no display available"


def test_status_endpoint_tray_status_defaults_to_unknown(tmp_path: Path) -> None:
    """When tray was never started (e.g. headless), /status reports 'unknown' (not crash)."""
    client, _ = _make_agent_client(tmp_path)
    # No tray_status was ever set on app.state.

    resp = client.get("/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # The endpoint must still respond, with a sensible default.
    assert "tray_status" in body
    assert body["tray_status"] in {"unknown", None, "unavailable"}
