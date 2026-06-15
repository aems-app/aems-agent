"""Tests for CLI commands."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from aems_agent import cli as cli_module
from aems_agent.config import AgentConfig, load_config


def test_token_command_displays_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config_dir", lambda: tmp_path)
    from aems_agent.config import ensure_auth_token

    token = ensure_auth_token(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["token"])
    assert result.exit_code == 0
    assert token in result.output


def test_set_path_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config_dir", lambda: tmp_path)
    storage = tmp_path / "exam_storage"
    storage.mkdir()
    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["set-path", str(storage)])
    assert result.exit_code == 0
    config = load_config(tmp_path)
    assert config.storage_path == str(storage.resolve())


def test_config_dir_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "get_config_dir", lambda: tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["config-dir"])
    assert result.exit_code == 0
    assert str(tmp_path) in result.output


def test_ensure_stdio_streams_substitutes_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windowed PyInstaller bundles set sys.stdout=None; verify we patch it.

    Reproduces the v0.4.0 URI-launch crash (uvicorn ColourizedFormatter calls
    sys.stdout.isatty() during logging config and trips AttributeError).
    """
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    cli_module._ensure_stdio_streams()

    assert sys.stdout is not None
    assert sys.stderr is not None
    assert sys.stdout.isatty() is False
    assert sys.stderr.isatty() is False
    # write/flush are no-ops that don't raise
    sys.stdout.write("ignored")
    sys.stdout.flush()
    sys.stderr.write("ignored")
    sys.stderr.flush()


def test_ensure_stdio_streams_idempotent_when_streams_exist(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Don't clobber the real streams when they're already valid."""
    cli_module._ensure_stdio_streams()
    # The real (or pytest-captured) stream should still be in place.
    print("hello", flush=True)
    captured = capsys.readouterr()
    assert "hello" in captured.out


def test_run_uses_main_thread_tray_helper_on_darwin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """macOS tray mode must invert control so pystray owns the main thread."""
    fake_app = SimpleNamespace(state=SimpleNamespace())
    called: dict[str, object] = {}

    monkeypatch.setattr(cli_module, "_setup_signal_handlers", lambda: None)
    monkeypatch.setattr(cli_module, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(cli_module, "load_config", lambda _: AgentConfig())
    monkeypatch.setattr(cli_module, "save_config", lambda config, config_dir: None)
    monkeypatch.setattr(cli_module, "ensure_auth_token", lambda config_dir: "token")
    monkeypatch.setattr(cli_module, "_preflight_port_or_die", lambda host, port: None)
    monkeypatch.setattr(cli_module, "find_spec", lambda name: object())
    monkeypatch.setattr(
        cli_module,
        "platform",
        SimpleNamespace(system=lambda: "Darwin"),
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "_run_with_tray_on_main_thread",
        lambda config_dir, agent_app, host, port: called.setdefault(
            "main_thread",
            (config_dir, agent_app, host, port),
        ),
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "_start_tray",
        lambda config_dir, agent_app=None: called.setdefault("threaded_tray", True),
    )
    monkeypatch.setitem(
        sys.modules,
        "uvicorn",
        SimpleNamespace(run=lambda *args, **kwargs: called.setdefault("uvicorn", True)),
    )
    monkeypatch.setitem(
        sys.modules,
        "aems_agent.app",
        SimpleNamespace(create_app=lambda config_dir: fake_app),
    )

    cli_module.run(port=61234, host="127.0.0.1", tray=True, launch_from_uri=None)

    assert called.get("main_thread") == (tmp_path, fake_app, "127.0.0.1", 61234)
    assert "threaded_tray" not in called


class TestPreflightPortCheck:
    """_preflight_port_or_die: free port passes; busy port dies informatively."""

    def test_free_port_passes(self) -> None:
        # Port 0 binds an ephemeral free port on every platform.
        cli_module._preflight_port_or_die("127.0.0.1", 0)

    def test_busy_port_non_aems_exits_1(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import socket

        shown: list[str] = []
        monkeypatch.setattr(cli_module, "_show_startup_error_dialog", shown.append)

        squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        squatter.bind(("127.0.0.1", 0))
        squatter.listen(1)
        port = squatter.getsockname()[1]
        try:
            with pytest.raises(SystemExit) as excinfo:
                cli_module._preflight_port_or_die("127.0.0.1", port)
        finally:
            squatter.close()

        assert excinfo.value.code == 1
        assert shown and "in use" in shown[0]

    def test_busy_port_aems_agent_exits_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An already-running agent is detected via the unauthenticated /status."""
        import http.server
        import json
        import threading

        shown: list[str] = []
        monkeypatch.setattr(cli_module, "_show_startup_error_dialog", shown.append)

        class _StatusHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                body = json.dumps({"status": "ok", "service": "aems-agent"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), _StatusHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with pytest.raises(SystemExit) as excinfo:
                cli_module._preflight_port_or_die("127.0.0.1", port)
        finally:
            server.shutdown()
            server.server_close()

        assert excinfo.value.code == 0
        assert shown and "already running" in shown[0]

    def test_preflight_does_not_set_so_reuseaddr_on_probe_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Source-level guard: the probe loop must never call setsockopt
        with SO_REUSEADDR on Windows it lets the bind() succeed against a
        real listening squatter that also has SO_REUSEADDR (Python's
        http.server defaults that way), which is exactly the v0.4.28
        regression that broke ``test_busy_port_aems_agent_exits_0`` on
        Windows CI. Keep this static check so the flag cannot quietly come
        back."""
        import inspect

        src = inspect.getsource(cli_module._preflight_port_or_die)
        # Allow setsockopt for other options (SO_LINGER etc.), but block
        # the one that creates the ghost-bind hazard.
        assert "SO_REUSEADDR" not in src, (
            "SO_REUSEADDR must not be set on the preflight probe socket on Windows;"
            " it would let the bind succeed against a real listener and false-pass."
        )
