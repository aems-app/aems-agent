# SPDX-License-Identifier: AGPL-3.0-or-later

"""
CLI entry point for the AEMS Local Bridge Agent.

Commands:
    aems-agent run [--port 61234] [--host 127.0.0.1] [--tray]  - Start the agent
    aems-agent token                                              - Display auth token
    aems-agent set-path <path>                                   - Set storage path
    aems-agent config-dir                                        - Show config directory
"""

import logging
import signal
import sys
from importlib.util import find_spec
import platform
import threading
from pathlib import Path
from typing import Any, Optional

import typer
from fastapi import FastAPI

logger = logging.getLogger(__name__)


def _ensure_stdio_streams() -> None:
    """Provide non-None stdio streams for windowed PyInstaller bundles.

    When the agent is launched from a URI handler or other GUI shell context,
    PyInstaller's windowed (``--noconsole``) mode sets ``sys.stdout`` and
    ``sys.stderr`` to ``None``. Downstream libraries (notably uvicorn's
    ``ColourizedFormatter`` which calls ``sys.stdout.isatty()``) then crash
    during logging configuration. Substituting a sink that satisfies the
    file-like protocol — ``isatty`` always False, ``write``/``flush`` no-ops
    — keeps the rest of the stack working without trying to redirect output
    anywhere users will see it.

    Idempotent and safe to call from any entrypoint.
    """

    class _NullStream:
        encoding = "utf-8"
        errors = "replace"

        def write(self, _data: str) -> int:
            return 0

        def flush(self) -> None:
            return None

        def isatty(self) -> bool:
            return False

        def fileno(self) -> int:  # noqa: D401 - file-like protocol
            raise OSError("no fileno for null stdio stream")

        def close(self) -> None:
            return None

    if sys.stdout is None:
        sys.stdout = _NullStream()  # type: ignore[assignment]
    if sys.stderr is None:
        sys.stderr = _NullStream()  # type: ignore[assignment]
    if sys.stdin is None:
        sys.stdin = _NullStream()  # type: ignore[assignment]


# Apply at import time so anything in the dependency tree that reaches for
# sys.stdout during its own import (uvicorn's logging config does this when
# instantiated) sees a usable stream.
_ensure_stdio_streams()

# E402: this import is deliberately below `_ensure_stdio_streams()` — config
# imports must observe the patched stdio streams before any module in their
# dependency chain calls `sys.stdout.isatty()` (uvicorn's logging-config path).
from .config import (  # noqa: E402
    AGENT_VERSION,
    AgentConfig,
    ensure_auth_token,
    get_auth_token,
    get_config_dir,
    load_config,
    save_config,
)


def _version_callback(value: bool) -> None:
    """Print the agent version and exit 0 when --version is passed."""
    if value:
        typer.echo(f"aems-agent {AGENT_VERSION}")
        raise typer.Exit(0)


app = typer.Typer(
    name="aems-agent",
    help="AEMS Local Bridge Agent - local filesystem access for exam PDFs",
)


@app.callback()
def _root(
    version: Optional[bool] = typer.Option(
        None,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Print the agent version and exit.",
    ),
) -> None:
    """Root callback wiring the --version flag."""
    return None


def _setup_signal_handlers() -> None:
    """Register signal handlers for graceful shutdown."""

    def _handle_signal(signum: int, frame: object) -> None:
        typer.echo(f"\nReceived signal {signum}, shutting down gracefully...")
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    # SIGTERM is not available on Windows
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)


@app.command()
def run(
    port: int = typer.Option(61234, "--port", "-p", help="Port to listen on"),
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host to bind to"),
    tray: bool = typer.Option(False, "--tray", help="Show system tray icon"),
    launch_from_uri: Optional[str] = typer.Option(
        None,
        "--launch-from-uri",
        help="Internal: launched via aems-agent:// protocol handler. The full URI is passed as the value.",
    ),
) -> None:
    """Start the AEMS Local Bridge Agent."""
    if find_spec("uvicorn") is None:
        typer.echo(
            "Error: uvicorn not installed. Run: pip install aems-agent",
            err=True,
        )
        raise typer.Exit(1)

    if launch_from_uri:
        typer.echo(f"Agent launched via URI: {launch_from_uri}")

    _setup_signal_handlers()

    config_dir = get_config_dir()
    config = load_config(config_dir)

    config_values = config.model_dump()
    config_values["port"] = port
    config_values["host"] = host

    config = AgentConfig(**config_values)
    save_config(config, config_dir)

    ensure_auth_token(config_dir)

    typer.echo(f"AEMS Local Bridge Agent v{AGENT_VERSION}")
    typer.echo(f"  Config dir:   {config_dir}")
    typer.echo(f"  Storage path: {config.storage_path or '(not configured)'}")
    typer.echo(f"  Listening on: http://{host}:{port}")
    typer.echo(f"  Token file:   {config_dir / 'auth_token'}")
    typer.echo("")

    # Pre-flight port check. uvicorn's bind failure on Windows in --noconsole
    # PyInstaller builds produces no visible output — the user sees the .exe
    # exit silently with no tray icon (Zohar 2026-05-25). Probe first so we
    # can show a tk dialog explaining what's wrong.
    #
    # The probe also HOLDS the bound socket and hands it to uvicorn so a
    # second process can't squat the port in the window between our
    # bind+close and uvicorn's own bind (TOCTOU). When `sock` is not None,
    # `_run_uvicorn_server` will call ``Server.serve(sockets=[sock])`` and
    # uvicorn skips its own bind entirely.
    sock = _preflight_port_or_die(host, port)

    from .app import create_app

    agent_app = create_app(config_dir)

    # pystray's Cocoa backend must own the main thread on macOS. Other
    # platforms keep uvicorn on the main thread and the tray in the
    # background as before.
    if tray:
        if _tray_requires_main_thread():
            _run_with_tray_on_main_thread(config_dir, agent_app, host, port, sock=sock)
            return
        _start_tray(config_dir, agent_app)

    _run_uvicorn_server(agent_app, host, port, sock=sock)


def _tray_requires_main_thread() -> bool:
    """Return whether the platform tray backend must own the main thread."""
    return platform.system() == "Darwin"


def _set_tray_state(agent_app: Optional[FastAPI], status: str, error: Optional[str] = None) -> None:
    """Update tray status fields when a FastAPI app is available."""
    if agent_app is None:
        return
    agent_app.state.tray_status = status
    agent_app.state.tray_error = error


def _prepare_tray_icon(config_dir: Path, agent_app: Optional[FastAPI]) -> Any:
    """Construct the tray icon and wire tray notification state into the app."""
    from .tray import create_tray

    icon: Any = create_tray(config_dir)
    notifier = getattr(icon, "_aems_pin_notifier", None)
    if notifier is not None and agent_app is not None:
        agent_app.state.tray_notifier = notifier
    _set_tray_state(agent_app, "starting")
    return icon


def _run_uvicorn_server(
    agent_app: FastAPI,
    host: str,
    port: int,
    *,
    sock: Optional[Any] = None,
) -> None:
    """Run uvicorn for the FastAPI app.

    If ``sock`` is provided it must be a ``socket.socket`` that was already
    bound to ``(host, port)`` by ``_preflight_port_or_die`` and is NOT in
    listening state. In that case we drop down to
    ``Config + Server.serve(sockets=[sock])`` so uvicorn re-uses the bound
    socket instead of binding the port a second time — that second bind
    is the TOCTOU window where another process can squat the port between
    our preflight close and uvicorn's own bind.
    """
    import asyncio

    import uvicorn  # type: ignore

    if sock is None:
        uvicorn.run(agent_app, host=host, port=port, log_level="info")
        return

    config = uvicorn.Config(agent_app, host=host, port=port, log_level="info")
    server = uvicorn.Server(config)
    # Server.serve() calls sock.listen(config.backlog) on each handed-in
    # socket, then drives the event loop. Run via asyncio.run so this
    # function still blocks like the uvicorn.run path it replaces.
    asyncio.run(server.serve(sockets=[sock]))


def _run_with_tray_on_main_thread(
    config_dir: Path,
    agent_app: FastAPI,
    host: str,
    port: int,
    *,
    sock: Optional[Any] = None,
) -> None:
    """Start uvicorn in a worker thread so macOS can run the tray on main.

    If the tray fails to construct (missing pystray, AppKit init error, etc.)
    we still want the agent serving — matching the Windows/Linux behaviour
    where `_start_tray` failures are non-fatal. In that case we fall back to
    running uvicorn directly on the main thread.

    ``sock`` is the pre-bound socket from ``_preflight_port_or_die``; passed
    through so the worker thread serves on the same socket the preflight
    held, closing the TOCTOU window between preflight and uvicorn's bind.
    """
    try:
        from .tray import run_icon_safely

        icon = _prepare_tray_icon(config_dir, agent_app)
    except ImportError:
        _set_tray_state(agent_app, "unavailable", "pystray not installed")
        typer.echo(
            "  System tray: unavailable (install pystray: pip install pystray pillow)",
            err=True,
        )
        _run_uvicorn_server(agent_app, host, port, sock=sock)
        return
    except Exception as exc:
        _set_tray_state(agent_app, "failed", str(exc))
        typer.echo(f"  System tray: failed to start ({exc})", err=True)
        _run_uvicorn_server(agent_app, host, port, sock=sock)
        return

    server_thread = threading.Thread(
        target=_run_uvicorn_server,
        args=(agent_app, host, port),
        kwargs={"sock": sock},
        daemon=False,
        name="aems-uvicorn",
    )
    server_thread.start()
    _set_tray_state(agent_app, "running")
    typer.echo("  System tray: enabled")
    run_icon_safely(icon, agent_app)


def _preflight_port_or_die(host: str, port: int) -> Any:
    """Verify the agent can bind ``port`` before uvicorn tries.

    On success returns a ``socket.socket`` that is already bound to
    ``(host, port)`` but NOT yet in listening state. The caller hands this
    to ``_run_uvicorn_server(..., sock=sock)`` so uvicorn re-uses the
    bound socket instead of binding the port a second time — closing the
    TOCTOU window where another process could grab the port between our
    bind+close and uvicorn's own bind.

    If the port is in use we attempt to identify whether the squatter is
    another AEMS Agent (responds to ``GET /status``). If yes, we show a
    "Agent already running" dialog and exit 0 — the user just launched it
    twice, no error needed. If not, we show "Port 61234 is in use by
    something else" and exit 1.

    Retry behaviour: when this process is launched right after an
    ``aems-agent.exe`` was taskkill'd (silent NSIS upgrade, /self-update
    flow) the previous socket can still be in TIME_WAIT for a couple of
    seconds. Retry the bind a few times with backoff *before* deciding
    the port is occupied so we don't false-positive a "port in use"
    failure on the legitimate restart path.
    """
    import socket
    import time

    # Resolve the bind family from the host so IPv6 hosts (e.g. ::1) probe
    # correctly instead of always failing the AF_INET bind.
    family = socket.AF_INET
    sockaddr: Any = (host, port)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if infos:
            family, _socktype, _proto, _canonname, sockaddr = infos[0]
    except (socket.gaierror, OSError):
        pass

    # Total retry budget ~6 s — comfortably above the kernel's loopback
    # listener-socket cleanup window after a taskkill /F without making
    # twice-launched users wait long.
    #
    # We deliberately do NOT set SO_REUSEADDR on this probe socket. On
    # Windows, SO_REUSEADDR is the ghost-bind flag: if the squatter happens
    # to also have SO_REUSEADDR set (which Python's http.server and many
    # other servers do by default), the kernel happily lets us "bind" the
    # same port without an error, and we then fail to detect that another
    # process is genuinely listening. The retry-with-backoff loop is what
    # closes the race after a taskkill /F: each fresh bind attempt tests
    # whether the kernel has finished tearing down the dead listener, and
    # we retry up to ~6 s before deciding the port is really in use by
    # someone else. A genuine squatter is correctly surfaced because every
    # one of the 6 bind attempts will fail.
    backoffs = (0.0, 0.5, 1.0, 1.5, 1.5, 1.5)
    bind_err: Optional[OSError] = None
    for sleep in backoffs:
        if sleep:
            time.sleep(sleep)
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            sock.bind(sockaddr)
            # Hand the bound socket back to the caller so uvicorn re-uses
            # it instead of binding a second time. Do NOT call .close()
            # here — that would reopen the TOCTOU window this function
            # exists to close.
            return sock
        except OSError as e:
            bind_err = e
            sock.close()

    # Port is *still* taken after the retry budget — is it another AEMS Agent?
    already_aems = False
    probe_host = host
    if probe_host in ("0.0.0.0", "::", "[::]"):
        # Wildcard binds answer on loopback; connecting to the wildcard
        # address itself fails on Windows.
        probe_host = "127.0.0.1"
    if ":" in probe_host and not probe_host.startswith("["):
        probe_host = f"[{probe_host}]"
    try:
        import urllib.request

        # /status is the unauthenticated liveness endpoint. /health
        # requires a bearer token, so probing it always raised and the
        # "agent already running" dialog could never appear.
        with urllib.request.urlopen(  # noqa: S310 - localhost only
            f"http://{probe_host}:{port}/status", timeout=1.0
        ) as resp:
            if resp.status == 200:
                body = resp.read(512).decode("utf-8", errors="ignore").lower()
                if "aems-agent" in body:
                    already_aems = True
    except Exception:
        pass

    msg = (
        "Another AEMS Agent is already running on this computer.\n\n"
        "Look for an existing tray icon, or stop the previous agent process\n"
        "via Task Manager (search 'aems-agent') before launching again."
        if already_aems
        else (
            f"AEMS Agent cannot start: port {port} is already in use by\n"
            "another program on this computer.\n\n"
            "Stop the process holding the port, or change the agent's port\n"
            "via 'aems-agent run --port <other>'."
        )
    )
    logger.error("Preflight failed after retries: %s (last bind error: %s)", msg, bind_err)
    _show_startup_error_dialog(msg)
    sys.exit(0 if already_aems else 1)


def _show_startup_error_dialog(message: str) -> None:
    """Pop a native tk message box. Best effort — falls back to stderr."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo("AEMS Agent", message)
        root.destroy()
    except Exception:
        # tk not available (headless / no display) — log only.
        typer.echo(message, err=True)


def _start_tray(config_dir: Path, agent_app: Optional[FastAPI] = None) -> None:
    """Start the system tray icon in a background thread.

    Sets ``agent_app.state.tray_status`` so the agent's ``/status`` endpoint
    can surface tray health to the AEMS web Settings badge.  Transitions:

    * ``"starting"`` — icon constructed, daemon thread about to start.
    * ``"running"`` — daemon thread launched (no exception synchronously).
    * ``"failed"`` — exception raised either during setup or inside the
      daemon thread (the latter is captured by ``run_icon_safely``).
    * ``"unavailable"`` — ``pystray`` not installed.
    """
    try:
        from .tray import run_icon_safely

        icon = _prepare_tray_icon(config_dir, agent_app)

        thread = threading.Thread(
            target=run_icon_safely,
            args=(icon, agent_app),
            daemon=True,
            name="aems-tray",
        )
        thread.start()

        _set_tray_state(agent_app, "running")
        typer.echo("  System tray: enabled")
    except ImportError:
        _set_tray_state(agent_app, "unavailable", "pystray not installed")
        typer.echo(
            "  System tray: unavailable (install pystray: pip install pystray pillow)",
            err=True,
        )
    except Exception as exc:
        _set_tray_state(agent_app, "failed", str(exc))
        typer.echo(f"  System tray: failed to start ({exc})", err=True)


@app.command()
def token() -> None:
    """Display the current authentication token."""
    config_dir = get_config_dir()
    existing_token = get_auth_token(config_dir)

    if existing_token:
        typer.echo(existing_token)
    else:
        new_token = ensure_auth_token(config_dir)
        typer.echo(f"Generated new token: {new_token}")


@app.command("set-path")
def set_path(
    path: str = typer.Argument(..., help="Absolute path to storage directory"),
) -> None:
    """Set the local storage directory path."""
    target = Path(path)

    if not target.is_absolute():
        typer.echo(f"Error: Path must be absolute: {path}", err=True)
        raise typer.Exit(1)

    if not target.exists():
        try:
            target.mkdir(parents=True, exist_ok=True)
            typer.echo(f"Created directory: {target}")
        except OSError as e:
            typer.echo(f"Error: Cannot create directory: {e}", err=True)
            raise typer.Exit(1)

    if not target.is_dir():
        typer.echo(f"Error: Not a directory: {target}", err=True)
        raise typer.Exit(1)

    config_dir = get_config_dir()
    config = load_config(config_dir)
    config.storage_path = str(target.resolve())
    save_config(config, config_dir)

    typer.echo(f"Storage path set to: {target.resolve()}")


@app.command("config-dir")
def config_dir() -> None:
    """Show the configuration directory path."""
    typer.echo(str(get_config_dir()))


def main() -> None:
    """Main entry point for the aems-agent CLI."""
    app()


if __name__ == "__main__":
    main()
