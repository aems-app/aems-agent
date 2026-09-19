"""A user launch must not silently run without its tray icon."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from aems_agent import cli
from aems_agent.config import AgentConfig


@pytest.mark.parametrize("arguments,expected_tray", [([], True), (["--tray"], True), (["--no-tray"], False)])
def test_run_defaults_to_tray_with_explicit_headless_opt_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    arguments: list[str], expected_tray: bool,
) -> None:
    """Exercise the actual Typer parser; direct calls bypass option defaults."""
    calls: list[str] = []
    agent_app = SimpleNamespace(state=SimpleNamespace())
    monkeypatch.setattr(cli, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "load_config", lambda _: AgentConfig())
    monkeypatch.setattr(cli, "save_config", lambda *_: None)
    monkeypatch.setattr(cli, "ensure_auth_token", lambda _: "fixture-token")
    monkeypatch.setattr(cli, "_setup_signal_handlers", lambda: None)
    monkeypatch.setattr(cli, "_acquire_single_instance_lock", lambda _: True)
    monkeypatch.setattr(cli, "_preflight_port_or_die", lambda *_: None)
    monkeypatch.setattr(cli, "_tray_requires_main_thread", lambda: False)
    monkeypatch.setattr(cli, "_start_tray", lambda *_: calls.append("tray"))
    monkeypatch.setattr(cli, "_run_uvicorn_server", lambda *_, **__: calls.append("server"))
    monkeypatch.setitem(sys.modules, "aems_agent.app", SimpleNamespace(create_app=lambda _: agent_app))

    result = CliRunner().invoke(cli.app, ["run", *arguments])

    assert result.exit_code == 0, result.output
    assert calls == (["tray", "server"] if expected_tray else ["server"])


@pytest.mark.parametrize(
    "system,frozen,arguments,expected",
    [
        ("Windows", True, [], ["run", "--tray"]),
        ("Darwin", True, [], ["run", "--tray"]),
        ("Windows", True, ["run", "--no-tray"], ["run", "--no-tray"]),
        ("Windows", True, ["--version"], ["--version"]),
        ("Windows", False, [], []),
        ("Linux", True, [], []),
    ],
)
def test_packaged_entrypoint_handles_windows_double_click(
    monkeypatch: pytest.MonkeyPatch, system: str, frozen: bool,
    arguments: list[str], expected: list[str],
) -> None:
    """Run the entrypoint body, including its call to main, without a server."""
    import platform

    captured: list[list[str]] = []
    monkeypatch.setattr(platform, "system", lambda: system)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "argv", ["aems-agent.exe", *arguments])
    monkeypatch.setattr(cli, "main", lambda: captured.append(sys.argv[1:]))
    launcher = Path(__file__).resolve().parents[1] / "packaging" / "launcher.py"
    runpy.run_path(str(launcher), run_name="__main__")
    assert captured == [expected]


def test_tray_worker_stays_alive_until_stopped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Windows worker must retain the blocking icon loop; no fixed sleeps."""
    import threading
    from aems_agent import tray

    entered = threading.Event()
    stop = threading.Event()
    exited = threading.Event()
    worker: list[threading.Thread] = []

    class BlockingIcon:
        _aems_pin_notifier = None

        def run(self) -> None:
            worker.append(threading.current_thread())
            entered.set()
            try:
                assert stop.wait(5), "fixture failed to release tray loop"
            finally:
                exited.set()

    monkeypatch.setattr(tray, "create_tray", lambda _: BlockingIcon())
    agent_app: Any = SimpleNamespace(state=SimpleNamespace())
    try:
        cli._start_tray(tmp_path, agent_app)
        assert entered.wait(5), "tray thread never entered icon.run"
        assert worker[0].is_alive()
        assert agent_app.state.tray_status == "running"
        assert not exited.is_set()
    finally:
        stop.set()
        if worker:
            worker[0].join(5)
    assert exited.is_set()
    assert not worker[0].is_alive()
