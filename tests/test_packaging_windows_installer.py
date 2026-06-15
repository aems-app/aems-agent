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
    silent upgrade. v0.4.22 shipped this gap; v0.4.23 closed it with a bare
    NSIS ``Exec``; v0.4.28 switched to ``ExecShell`` because the
    detached-spawn parent chain from POST /self-update broke the bare-Exec
    form on some hardened Windows builds. Keep this test so neither gap
    re-opens unobserved.
    """
    src = INSTALLER_PATH.read_text(encoding="utf-8")

    assert "IfSilent" in src, "installer must branch on Silent mode"
    # The relaunch instruction must appear AFTER IfSilent so attended installs
    # keep relying on MUI_FINISHPAGE_RUN. From v0.4.28 we use ExecShell rather
    # than bare Exec — see commit history for the why.
    if_silent_pos = src.find("IfSilent")
    relaunch_pos = src.find('ExecShell "open" "$INSTDIR\\aems-agent.exe" "run --tray"')
    assert relaunch_pos > if_silent_pos, (
        "silent-relaunch ExecShell must follow the IfSilent gate so attended"
        " installs keep relying on MUI_FINISHPAGE_RUN"
    )


def test_windows_installer_waits_for_port_release_after_taskkill() -> None:
    """The Sleep after taskkill must be long enough that the new agent's
    SO_REUSEADDR + retry preflight has time to grab the loopback port.

    v0.4.22 shipped a 1-second wait, which was fine for cleanly-shut-down
    agents but left a race window for taskkill-killed sockets in TIME_WAIT.
    v0.4.28 raises the floor to 3 seconds; below that the test fails so we
    notice if someone shortens it later for "snappier installs".
    """
    src = INSTALLER_PATH.read_text(encoding="utf-8")

    # Find the Sleep that follows the *install*-section taskkill (there is a
    # second taskkill in the Uninstall section that we don't care about here).
    install_section = src.split('Section "Uninstall"', 1)[0]
    assert "taskkill /IM aems-agent.exe" in install_section
    import re

    sleeps = [
        int(m.group(1))
        for m in re.finditer(r"^\s*Sleep\s+(\d+)\s*$", install_section, re.MULTILINE)
    ]
    assert sleeps, "Install section must Sleep after taskkill"
    assert max(sleeps) >= 3000, (
        f"Sleep after taskkill is {max(sleeps)} ms; needs >= 3000 to give the"
        " killed agent's socket time to leave TIME_WAIT on Windows"
    )
