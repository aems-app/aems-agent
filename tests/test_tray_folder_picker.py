"""Regression tests for the Windows tray folder picker."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from aems_agent.config import AgentConfig, load_config, save_config


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
