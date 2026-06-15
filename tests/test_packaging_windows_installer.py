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


def test_windows_installer_relaunches_tray_in_silent_mode() -> None:
    """Silent installs (/S) skip MUI_PAGE_FINISH, so MUI_FINISHPAGE_RUN never fires.

    The install section killed the existing tray to free aems-agent.exe for
    overwrite; without an explicit relaunch the agent stays down after a
    silent upgrade. v0.4.22 shipped this gap; v0.4.23 closes it. Keep this
    test so the gap cannot reopen unobserved.
    """
    src = INSTALLER_PATH.read_text(encoding="utf-8")

    assert "IfSilent" in src, "installer must branch on Silent mode"
    # The Exec line must be inside the silent branch (i.e., appear AFTER IfSilent)
    if_silent_pos = src.find("IfSilent")
    exec_pos = src.find('Exec \'"$INSTDIR\\aems-agent.exe" run --tray\'')
    assert exec_pos > if_silent_pos, (
        "silent-relaunch Exec must follow the IfSilent gate so attended installs"
        " keep relying on MUI_FINISHPAGE_RUN"
    )
