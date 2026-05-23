# SPDX-License-Identifier: AGPL-3.0-or-later

"""
System tray integration for the AEMS Local Bridge Agent.

Provides a tray icon with:
- Status indicator (green = running, yellow = no storage path)
- Menu: Open Settings, Set Storage Folder, Show Token, Quit

Requires: pystray, pillow (PIL)
"""

import logging
import threading
import webbrowser
from pathlib import Path
from typing import Any, Optional

from .icons import RUNTIME_ICON_SIZE, render_status_icon

logger = logging.getLogger(__name__)


def _create_icon_image(color: str = "green") -> Any:
    """Render the live tray icon at a native runtime size."""
    return render_status_icon(color, size=RUNTIME_ICON_SIZE)


def _open_folder_picker(config_dir: Path) -> None:
    """Open a native folder picker dialog to set the storage path."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        folder = filedialog.askdirectory(
            title="Select AEMS Exam Storage Folder",
            mustexist=False,
        )

        root.destroy()

        if folder:
            from .config import load_config, save_config

            config = load_config(config_dir)
            config.storage_path = str(Path(folder).resolve())
            save_config(config, config_dir)
            logger.info("Storage path set via tray: %s", folder)
    except Exception as e:
        logger.error("Folder picker failed: %s", e)


def create_tray(config_dir: Path) -> Any:
    """
    Create and return a pystray Icon instance.

    Args:
        config_dir: Agent config directory.

    Returns:
        Configured pystray Icon (call .run() to start).
    """
    import pystray  # type: ignore

    from .config import get_auth_token, load_config

    config = load_config(config_dir)
    icon_color = "green" if config.storage_path else "yellow"
    image = _create_icon_image(icon_color)

    def on_open_settings(icon: Any, item: Any) -> None:
        cfg = load_config(config_dir)
        # Prefer the AEMS instance the user has actually paired with: a hosted
        # https origin takes priority (api.aems.app is the canonical hosted
        # deployment), then any other paired origin, then any non-localhost
        # allowed origin, then the legacy http://127.0.0.1:8080 fallback for
        # local-only AEMS dev setups. Hard-coding port 8080 was wrong for
        # everyone running against the hosted app.
        target_url: Optional[str] = None
        candidates = list(cfg.paired_origins) + list(cfg.allowed_origins)
        # Hosted https first
        for origin in candidates:
            if origin.startswith("https://"):
                target_url = origin.rstrip("/") + "/settings#privacy"
                break
        # Then any other paired origin
        if not target_url:
            for origin in cfg.paired_origins:
                target_url = origin.rstrip("/") + "/settings#privacy"
                break
        # Fall back to local AEMS dev convention
        if not target_url:
            aems_host = cfg.host if cfg.host != "0.0.0.0" else "127.0.0.1"
            target_url = f"http://{aems_host}:8080/settings#privacy"
        webbrowser.open(target_url)

    def on_set_folder(icon: Any, item: Any) -> None:
        _open_folder_picker(config_dir)
        # Update icon color based on new config
        cfg = load_config(config_dir)
        new_color = "green" if cfg.storage_path else "yellow"
        icon.icon = _create_icon_image(new_color)

    def on_show_token(icon: Any, item: Any) -> None:
        token = get_auth_token(config_dir)
        if token:
            # Copy to clipboard if possible
            try:
                import tkinter as tk

                root = tk.Tk()
                root.withdraw()
                root.clipboard_clear()
                root.clipboard_append(token)
                root.update()
                root.destroy()
                logger.info("Token copied to clipboard")
            except Exception:
                logger.warning(
                    "Could not copy token to clipboard. Find it in: %s",
                    config_dir / "auth_token",
                )

    def on_quit(icon: Any, item: Any) -> None:
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Settings", on_open_settings, default=True),
        pystray.MenuItem("Set Storage Folder", on_set_folder),
        pystray.MenuItem("Copy Token", on_show_token),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon(
        name="aems-agent",
        icon=image,
        title="AEMS Local Bridge Agent",
        menu=menu,
    )

    def _notify_pairing_pin(pin: str, clipboard_ok: bool = False) -> None:
        """Show a tray notification with the pairing PIN.

        The tray toast itself is non-interactive on Windows -- the user
        cannot click to copy from it.  The agent puts the PIN on the OS
        clipboard before calling this so the user can simply paste; we
        mention that here so they know to Ctrl-V.
        """
        try:
            suffix = " (copied to clipboard - paste with Ctrl+V)" if clipboard_ok else ""
            icon.notify(f"Pairing PIN: {pin}{suffix}", "AEMS Agent Pairing")
        except Exception as e:
            logger.debug("Tray PIN notification failed: %s", e)

    icon._aems_pin_notifier = _notify_pairing_pin  # type: ignore[attr-defined]

    return icon


def run_icon_safely(icon: Any, app: Any | None = None) -> None:
    """Run pystray's icon, capture failures so /status can surface them.

    The icon.run() call blocks the daemon thread until stop() is called.
    Without this wrapper, any exception inside the thread vanishes silently
    and the agent reports tray status as 'running' forever — even though
    no icon ever appeared in the system tray (audit defect #3).

    Args:
        icon: A configured pystray Icon (returned by ``create_tray``).
        app: Optional FastAPI app whose ``state.tray_status`` will be
            updated to ``"failed"`` (with ``state.tray_error`` capturing
            the exception message) when ``icon.run()`` raises.
    """
    try:
        icon.run()
    except Exception as e:
        logger.error("Tray icon failed: %s", e, exc_info=True)
        if app is not None and hasattr(app, "state"):
            app.state.tray_status = "failed"
            app.state.tray_error = str(e)


def start_tray_thread(config_dir: Path) -> threading.Thread:
    """
    Start the system tray in a background daemon thread.

    Args:
        config_dir: Agent config directory.

    Returns:
        The thread running the tray icon.
    """
    icon = create_tray(config_dir)

    thread = threading.Thread(
        target=run_icon_safely, args=(icon,), daemon=True, name="aems-tray"
    )
    thread.start()

    return thread
