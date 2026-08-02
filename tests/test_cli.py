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

    def _capture_main_thread(
        config_dir: object,
        agent_app: object,
        host: object,
        port: object,
        **kwargs: object,
    ) -> None:
        called.setdefault(
            "main_thread",
            (config_dir, agent_app, host, port, kwargs.get("sock")),
        )

    monkeypatch.setattr(
        cli_module,
        "_run_with_tray_on_main_thread",
        _capture_main_thread,
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

    # 5th element is the pre-bound socket from _preflight_port_or_die; the
    # patched preflight in this test returns None, so we assert that and
    # confirm _run_with_tray_on_main_thread was handed the keyword arg.
    assert called.get("main_thread") == (tmp_path, fake_app, "127.0.0.1", 61234, None)
    assert "threaded_tray" not in called


def test_run_starts_recovery_app_without_rewriting_invalid_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executable must still start when strict config loading fails."""
    config_file = tmp_path / "config.json"
    invalid_config = b"{not valid json"
    config_file.write_bytes(invalid_config)
    fake_app = SimpleNamespace(state=SimpleNamespace())
    called: dict[str, object] = {}

    monkeypatch.setattr(cli_module, "_setup_signal_handlers", lambda: None)
    monkeypatch.setattr(cli_module, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(
        cli_module,
        "save_config",
        lambda *_args, **_kwargs: pytest.fail("invalid config must not be overwritten"),
    )
    monkeypatch.setattr(cli_module, "_acquire_single_instance_lock", lambda _: True)
    monkeypatch.setattr(cli_module, "ensure_auth_token", lambda _: "token")
    monkeypatch.setattr(cli_module, "_preflight_port_or_die", lambda *_: None)
    monkeypatch.setattr(cli_module, "find_spec", lambda _: object())
    monkeypatch.setattr(
        cli_module,
        "_run_uvicorn_server",
        lambda agent_app, host, port, sock=None: called.setdefault(
            "server", (agent_app, host, port, sock)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "aems_agent.app",
        SimpleNamespace(create_app=lambda _, fallback_config=None: fake_app),
    )

    cli_module.run(port=61234, host="127.0.0.1", tray=False, launch_from_uri=None)

    assert called["server"] == (fake_app, "127.0.0.1", 61234, None)
    assert config_file.read_bytes() == invalid_config


class TestPreflightPortCheck:
    """_preflight_port_or_die: free port passes; busy port dies informatively."""

    def test_free_port_passes(self) -> None:
        # Port 0 binds an ephemeral free port on every platform. Preflight
        # returns the bound socket; close it so the test doesn't leak fds.
        sock = cli_module._preflight_port_or_die("127.0.0.1", 0)
        try:
            assert sock is not None, "preflight must return a bound socket on success"
            # The returned socket must already be bound (TOCTOU fix).
            assert (
                sock.getsockname()[1] != 0
            ), "preflight socket should be bound to a concrete port, not 0"
        finally:
            sock.close()

    def test_preflight_holds_port_until_caller_closes_socket(self) -> None:
        """TOCTOU regression: after preflight returns, a second process
        cannot bind the same port — the returned socket is still holding it.

        Before the fix, ``_preflight_port_or_die`` did
        ``sock.bind(...); sock.close(); return`` and uvicorn then re-bound
        the same port. A racing process could grab the port in that gap,
        silently killing the new tray with no user-visible message.
        Now preflight hands the bound socket back so uvicorn re-uses it.
        """
        import socket

        sock = cli_module._preflight_port_or_die("127.0.0.1", 0)
        try:
            assert sock is not None
            bound_port = sock.getsockname()[1]
            # Attempt to bind a second socket to the same port; this MUST
            # fail because the preflight socket is still holding it.
            squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                with pytest.raises(OSError):
                    squatter.bind(("127.0.0.1", bound_port))
            finally:
                squatter.close()
        finally:
            sock.close()

    def test_run_uvicorn_server_hands_socket_to_server_serve(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When ``sock`` is provided, ``_run_uvicorn_server`` must drive
        ``Server.serve(sockets=[sock])`` — NOT ``uvicorn.run`` which would
        bind the port a second time and reopen the TOCTOU window.
        """
        import socket as _socket

        captured: dict[str, object] = {}

        class _FakeServer:
            def __init__(self, _config: object) -> None:
                captured["config_seen"] = True

            async def serve(self, sockets: object = None) -> None:
                captured["sockets"] = sockets

        class _FakeConfig:
            def __init__(self, app: object, *, host: str, port: int, log_level: str) -> None:
                captured["host"] = host
                captured["port"] = port
                captured["log_level"] = log_level

        def _no_run(*args: object, **kwargs: object) -> None:
            captured["uvicorn_run_called"] = True

        fake_uvicorn = SimpleNamespace(Server=_FakeServer, Config=_FakeConfig, run=_no_run)
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            fake_app = SimpleNamespace()
            cli_module._run_uvicorn_server(
                fake_app,  # type: ignore[arg-type]
                "127.0.0.1",
                sock.getsockname()[1],
                sock=sock,
            )
        finally:
            sock.close()

        assert captured.get("sockets") == [
            sock
        ], "Server.serve must receive the pre-bound socket as sockets=[sock]"
        assert (
            captured.get("uvicorn_run_called") is not True
        ), "When a pre-bound socket is provided, uvicorn.run must NOT be called"

    def test_run_uvicorn_server_falls_back_to_uvicorn_run_without_socket(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Backward-compat: with ``sock=None`` the old code path stays put."""
        captured: dict[str, object] = {}

        def _capture_run(*args: object, **kwargs: object) -> None:
            captured["called"] = (args, kwargs)

        fake_uvicorn = SimpleNamespace(run=_capture_run)
        monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)

        fake_app = SimpleNamespace()
        cli_module._run_uvicorn_server(
            fake_app,  # type: ignore[arg-type]
            "127.0.0.1",
            61234,
            sock=None,
        )

        assert "called" in captured, "uvicorn.run must run when sock is None"

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
        with SO_REUSEADDR. On Windows it lets the bind() succeed against
        a real listening squatter that also has SO_REUSEADDR (Python's
        http.server defaults that way), which is exactly the v0.4.28
        regression that broke ``test_busy_port_aems_agent_exits_0`` on
        Windows CI. Keep this static check so the flag cannot quietly
        come back.

        Strips comments and docstrings from the source first so the
        explanation of *why* the flag is forbidden doesn't false-positive
        the check.
        """
        import inspect
        import io
        import token
        import tokenize

        full_src = inspect.getsource(cli_module._preflight_port_or_die)
        code_tokens: list[str] = []
        for tok in tokenize.generate_tokens(io.StringIO(full_src).readline):
            if tok.type in (token.COMMENT, token.STRING):
                continue
            code_tokens.append(tok.string)
        code_only = " ".join(code_tokens)

        assert "SO_REUSEADDR" not in code_only, (
            "SO_REUSEADDR must not be set on the preflight probe socket on Windows;"
            " it would let the bind succeed against a real listener and false-pass."
        )
        assert "setsockopt" not in code_only, (
            "No setsockopt call expected in _preflight_port_or_die — the retry"
            " loop is what closes the race after taskkill /F."
        )


class TestSingleInstanceLock:
    """The single-instance guard must make a duplicate launch a silent no-op.

    Regression for the macOS port-conflict defect: a second AEMS Agent
    (Finder double-click, launchd respawn, self-update relaunch) used to hit
    the port preflight and show the scary "port in use by another program,
    change the port" dialog. The lock now arbitrates *before* the preflight,
    so the second launch detects the first and exits 0 with no dialog.
    """

    def teardown_method(self) -> None:
        # Release any lock the test left held so the next test starts clean.
        handle = getattr(cli_module, "_single_instance_handle", None)
        if handle is not None:
            try:
                handle.close()
            finally:
                cli_module._single_instance_handle = None

    def test_first_acquire_succeeds_second_fails(self, tmp_path: Path) -> None:
        """The first caller owns the lock; a second concurrent caller does not."""
        assert cli_module._acquire_single_instance_lock(tmp_path) is True
        # A second attempt while the first handle is still open (same machine,
        # different open-file description) must observe the lock as held.
        assert cli_module._acquire_single_instance_lock(tmp_path) is False

    def test_lock_releases_on_close(self, tmp_path: Path) -> None:
        """Closing the handle frees the lock so a relaunch can re-acquire it."""
        assert cli_module._acquire_single_instance_lock(tmp_path) is True
        handle = cli_module._single_instance_handle
        assert handle is not None
        handle.close()
        cli_module._single_instance_handle = None
        # The previous owner is gone; a fresh launch acquires cleanly.
        assert cli_module._acquire_single_instance_lock(tmp_path) is True

    def test_lockfile_error_fails_open(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A lock-file I/O error must never block a legitimate launch."""

        def _boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("disk gone")

        monkeypatch.setattr("builtins.open", _boom)
        assert cli_module._acquire_single_instance_lock(tmp_path) is True
