"""Regression tests for the Windows tray folder picker."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import logging

from aems_agent.config import AgentConfig, load_config, save_config


def test_tray_uses_safe_defaults_without_rewriting_invalid_config(tmp_path: Path) -> None:
    """Tray construction must remain available during config recovery."""
    from aems_agent import tray

    config_dir = tmp_path / "invalid_tray_config"
    config_dir.mkdir()
    config_file = config_dir / "config.json"
    invalid_config = b"{not valid json"
    config_file.write_bytes(invalid_config)

    config = tray._load_config_for_tray(config_dir)

    assert config == AgentConfig()
    assert config_file.read_bytes() == invalid_config


def test_folder_selection_explains_invalid_config_without_rewriting(
    tmp_path: Path,
    monkeypatch,
    caplog,
) -> None:
    """Recovery mode must explain why a chosen folder was not persisted."""
    from aems_agent import tray

    config_dir = tmp_path / "invalid_folder_config"
    config_dir.mkdir()
    config_file = config_dir / "config.json"
    invalid_config = b"{not valid json"
    config_file.write_bytes(invalid_config)
    selected = str((tmp_path / "Chosen Storage").resolve())
    monkeypatch.setattr(tray.platform, "system", lambda: "Windows")
    monkeypatch.setattr(tray, "_pick_folder_windows", lambda: selected)

    with caplog.at_level(logging.ERROR, logger="aems_agent.tray"):
        tray._open_folder_picker(config_dir)

    assert "Storage folder was not changed because config.json is invalid" in caplog.text
    assert config_file.read_bytes() == invalid_config


def test_pick_folder_windows_reads_selected_path_from_powershell(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The Windows picker should run out-of-process and return the chosen folder."""
    from aems_agent import tray

    selected = str((tmp_path / "Exam Storage").resolve())
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return SimpleNamespace(returncode=0, stdout=selected + "\n", stderr="")

    monkeypatch.setattr(tray.subprocess, "run", fake_run)

    result = tray._pick_folder_windows()

    assert result == selected
    assert "-STA" in captured["argv"]
    # CREATE_NO_WINDOW (0x08000000) must be set so users don't see a flashing
    # console window next to the folder dialog.
    creationflags = captured["kwargs"].get("creationflags", 0)
    assert creationflags & 0x08000000 == 0x08000000


def test_open_folder_picker_persists_selected_windows_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Selecting a folder from the tray should save it into the agent config."""
    from aems_agent import tray

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    save_config(
        AgentConfig(storage_path=None, port=61234, host="127.0.0.1"),
        config_dir,
    )
    selected = str((tmp_path / "Chosen Storage").resolve())

    monkeypatch.setattr(tray.platform, "system", lambda: "Windows")
    monkeypatch.setattr(tray, "_pick_folder_windows", lambda: selected)

    tray._open_folder_picker(config_dir)

    assert load_config(config_dir).storage_path == selected


def test_pick_folder_macos_reads_selected_path_from_osascript(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """The macOS picker must run osascript out-of-process and return the path."""
    from aems_agent import tray

    selected = str((tmp_path / "Exam Storage").resolve())
    captured: dict[str, object] = {}

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        captured["argv"] = argv
        return SimpleNamespace(returncode=0, stdout=selected + "\n", stderr="")

    monkeypatch.setattr(tray.subprocess, "run", fake_run)

    result = tray._pick_folder_macos()

    assert result == selected
    argv = captured["argv"]
    assert isinstance(argv, list)
    # Must invoke the native chooser via osascript, never Tk on the Cocoa
    # main thread (that aborts the whole agent process).
    assert str(argv[0]).endswith("osascript")
    assert any("choose folder" in str(part) for part in argv)


def test_pick_folder_macos_returns_none_on_cancel(
    monkeypatch,
) -> None:
    """User cancelling the dialog (osascript exit -128/non-zero) yields None."""
    from aems_agent import tray

    def fake_run(argv, **kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(returncode=1, stdout="", stderr="User canceled. (-128)")

    monkeypatch.setattr(tray.subprocess, "run", fake_run)

    assert tray._pick_folder_macos() is None


def test_open_folder_picker_uses_osascript_on_macos_never_tk(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """On macOS the picker MUST go through osascript, never Tk.

    Regression guard for the reported defect: "Set Storage Folder" from the
    menu bar made the agent report "Installed - not running". Root cause was
    ``_pick_folder_tk`` constructing a Tk root on the pystray Cocoa main
    thread, which aborts the process (taking the HTTP server down with it).
    """
    from aems_agent import tray

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    save_config(
        AgentConfig(storage_path=None, port=61234, host="127.0.0.1"),
        config_dir,
    )
    selected = str((tmp_path / "Mac Storage").resolve())

    def _tk_must_not_run() -> str:
        raise AssertionError("_pick_folder_tk must never be called on macOS")

    monkeypatch.setattr(tray.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(tray, "_pick_folder_tk", _tk_must_not_run)
    monkeypatch.setattr(tray, "_pick_folder_macos", lambda: selected)

    tray._open_folder_picker(config_dir)

    assert load_config(config_dir).storage_path == selected
