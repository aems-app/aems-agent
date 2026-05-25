"""Static checks for the Windows installer script."""

from __future__ import annotations

from pathlib import Path


INSTALLER_PATH = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "installer.nsi"


def test_windows_installer_stops_running_agent_before_overwrite() -> None:
    """Upgrades must replace a running aems-agent.exe instead of failing on write."""
    src = INSTALLER_PATH.read_text(encoding="utf-8")

    assert "taskkill" in src
    assert "aems-agent.exe" in src
    assert "/F" in src
