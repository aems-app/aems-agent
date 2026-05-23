# SPDX-License-Identifier: AGPL-3.0-or-later

"""
CLI entry point for the AEMS Local Bridge Agent.

Commands:
    aems-agent run [--port 61234] [--host 127.0.0.1] [--tray]  - Start the agent
    aems-agent token                                              - Display auth token
    aems-agent set-path <path>                                   - Set storage path
    aems-agent config-dir                                        - Show config directory
"""

import os
import signal
import sys
from pathlib import Path
from typing import Any, Optional

import typer
from fastapi import FastAPI


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

from .config import (
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
    try:
        import uvicorn  # type: ignore
    except ImportError:
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

    from .app import create_app

    agent_app = create_app(config_dir)

    # Start system tray in a separate thread if requested
    if tray:
        _start_tray(config_dir, agent_app)

    uvicorn.run(agent_app, host=host, port=port, log_level="info")


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
        from .tray import create_tray, run_icon_safely

        import threading

        icon: Any = create_tray(config_dir)

        # Wire PIN notifier into FastAPI app state if available
        notifier = getattr(icon, "_aems_pin_notifier", None)
        if notifier is not None and agent_app is not None:
            agent_app.state.tray_notifier = notifier

        if agent_app is not None:
            agent_app.state.tray_status = "starting"
            agent_app.state.tray_error = None

        thread = threading.Thread(
            target=run_icon_safely,
            args=(icon, agent_app),
            daemon=True,
            name="aems-tray",
        )
        thread.start()

        if agent_app is not None:
            agent_app.state.tray_status = "running"

        typer.echo("  System tray: enabled")
    except ImportError:
        if agent_app is not None:
            agent_app.state.tray_status = "unavailable"
            agent_app.state.tray_error = "pystray not installed"
        typer.echo(
            "  System tray: unavailable (install pystray: pip install pystray pillow)",
            err=True,
        )
    except Exception as e:
        if agent_app is not None:
            agent_app.state.tray_status = "failed"
            agent_app.state.tray_error = str(e)
        typer.echo(f"  System tray: failed to start ({e})", err=True)


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
