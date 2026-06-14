"""Static checks for the Windows portable-bundle install/upgrade script."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "packaging" / "windows" / "install.ps1"


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def test_install_script_exists() -> None:
    assert SCRIPT_PATH.exists(), (
        "packaging/windows/install.ps1 must ship with the portable bundle so "
        "users can upgrade without tripping the 'agent already running' dialog."
    )


def test_install_script_stops_running_agent_before_overwrite(script_text: str) -> None:
    """The portable installer must kill the existing tray before copying files."""
    assert "Stop-RunningAgent" in script_text
    # PyInstaller's onedir layout holds locks on aems-agent.exe and the DLLs
    # under _internal/ while the tray is up. Without an explicit stop the
    # Copy-Item silently fails or leaves a mixed-version directory.
    assert "Stop-Process" in script_text
    assert "aems-agent" in script_text


def test_install_script_waits_for_port_to_free(script_text: str) -> None:
    """After Stop-Process there is a brief window where the listener still holds 61234."""
    assert "61234" in script_text
    # Either a TcpListener probe or Test-NetConnection — anything that proves
    # the port is free before launching the new tray (which would otherwise
    # trip _preflight_port_or_die's "already running" dialog).
    assert "TcpListener" in script_text or "Test-NetConnection" in script_text


def test_install_script_starts_new_tray(script_text: str) -> None:
    """The default behaviour is to install AND start the tray in one step."""
    assert "Start-Tray" in script_text
    assert "--tray" in script_text or "'run','--tray'" in script_text


def test_install_script_registers_autostart(script_text: str) -> None:
    """install.ps1 must mirror what installer.nsi writes to HKCU\\Run.

    Without this, users who installed from the portable bundle would keep
    needing to relaunch the tray manually after every sign-in, and the
    settings page's "Does not auto-start" caveat would stay accurate for
    them even though it is hidden on Windows.
    """
    assert "Register-Autostart" in script_text
    assert "HKCU" in script_text
    assert "Software\\Microsoft\\Windows\\CurrentVersion\\Run" in script_text
    assert "AEMS Agent" in script_text


def test_install_script_supports_no_start(script_text: str) -> None:
    """`-NoStart` is the documented escape hatch for fully scripted reinstall flows."""
    assert "-NoStart" in script_text
    assert "$NoStart" in script_text


def test_install_script_shipped_by_build(tmp_path: Path) -> None:
    """`build._ship_windows_install_script` copies the script into dist/aems-agent/."""
    import sys

    sys.path.insert(0, str(REPO_ROOT / "packaging"))
    try:
        import build  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    dist = tmp_path / "aems-agent"
    dist.mkdir()
    build._ship_windows_install_script(dist)

    shipped = dist / "install.ps1"
    assert shipped.exists()
    assert shipped.read_text(encoding="utf-8").startswith("# AEMS Agent")
