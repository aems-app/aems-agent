# SPDX-License-Identifier: AGPL-3.0-or-later

"""
System tray integration for the AEMS Local Bridge Agent.

Provides a tray icon with:
- Status indicator at startup: green = running with storage path set,
  yellow = running without a storage path. The icon does NOT currently
  repaint red at runtime — failure paths (`tray_status = "failed" /
  "unavailable"`) mean the tray thread is already dead, so there is no
  live icon to recolour. The red variant exists in ``icons.py`` and is
  available for a future "storage folder disappeared" watcher, but is
  not wired up yet.
- Menu: Open Settings, Set Storage Folder, Show Token, Quit

Requires: pystray, pillow (PIL)
"""

import logging
import platform
import subprocess
import threading
import webbrowser
from pathlib import Path
from typing import Any, Optional

from .clipboard import copy_text_to_clipboard, windows_system32_dir
from .config import AgentConfig, ConfigLoadError, get_auth_token, load_config, save_config
from .icons import RUNTIME_ICON_SIZE, render_status_icon

logger = logging.getLogger(__name__)


def _create_icon_image(color: str = "green") -> Any:
    """Render the live tray icon at a native runtime size."""
    return render_status_icon(color, size=RUNTIME_ICON_SIZE)


def _load_config_for_tray(config_dir: Path) -> AgentConfig:
    """Use safe in-memory defaults for read-only tray behavior during recovery."""
    try:
        return load_config(config_dir)
    except ConfigLoadError as exc:
        logger.error(
            "Tray is using safe defaults because config.json is invalid and was not changed: %s",
            exc,
        )
        return AgentConfig()


def _pick_folder_windows() -> Optional[str]:
    """Use a dedicated STA PowerShell process for the Windows folder picker.

    Tk dialogs launched from the tray callback thread are unreliable on
    Windows; users can get a folder window that won't close or confirm.
    Launching the picker in a separate GUI-capable process avoids that
    thread-affinity problem and lets upgrades keep the agent itself
    windowless.
    """
    # FolderBrowserDialog has no native TopMost / activate-on-show. Without an
    # owner the dialog can appear behind the currently focused window (any
    # full-screen app, or just whatever the user was looking at when they
    # clicked the tray menu), and the click looks like a no-op. Create a
    # transient invisible Form with TopMost=$true and pass it as the
    # ShowDialog owner so the dialog inherits z-order and steals focus.
    script = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Windows.Forms
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::None
$owner.StartPosition = [System.Windows.Forms.FormStartPosition]::Manual
$owner.Location = New-Object System.Drawing.Point -10000,-10000
$owner.Size = New-Object System.Drawing.Size 1,1
$owner.Show()
$owner.Activate()
try {
    $dialog = New-Object System.Windows.Forms.FolderBrowserDialog
    $dialog.Description = 'Select AEMS Exam Storage Folder'
    $dialog.ShowNewFolderButton = $true
    if ($dialog.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) {
        Write-Output $dialog.SelectedPath
    }
} finally {
    $owner.Close()
    $owner.Dispose()
}
"""
    # CREATE_NO_WINDOW (0x08000000) keeps Windows from allocating a visible
    # console window for the PowerShell helper. Without this, every "Set
    # Storage Folder" click flashes a black shell window next to the folder
    # dialog, which looks alarming to non-technical users.
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    # Absolute path: bare "powershell" resolves against the CWD before PATH
    # on Windows, which would execute a planted powershell.exe.
    powershell_exe = str(windows_system32_dir() / "WindowsPowerShell" / "v1.0" / "powershell.exe")
    try:
        completed = subprocess.run(
            [
                powershell_exe,
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Windows folder picker process failed: %s", exc)
        return None

    if completed.returncode != 0:
        logger.warning(
            "Windows folder picker exited with %s: %s",
            completed.returncode,
            (completed.stderr or "").strip(),
        )
        return None

    return completed.stdout.strip()


def _pick_folder_macos() -> Optional[str]:
    """Native macOS folder picker via ``osascript`` (out-of-process).

    DO NOT use ``_pick_folder_tk`` on macOS. On Darwin the tray runs on the
    AppKit main thread that ``NSApplication`` (pystray's Cocoa backend)
    already owns; constructing a Tk root there initialises a *second* Cocoa
    event loop on the same thread, which aborts the whole process. When the
    process dies the agent's HTTP server (a background thread) dies with it,
    the ``/status`` probe stops answering, and the AEMS web badge flips to
    "Installed — not running" — exactly the reported "Set Storage Folder
    breaks the agent" symptom. ``osascript`` runs the native chooser in a
    separate process, off our main thread, so it can never crash the agent.
    """
    # ``choose folder`` returns an alias; ``POSIX path of`` yields the path.
    script = 'POSIX path of (choose folder with prompt "Select AEMS Exam Storage Folder")'
    try:
        completed = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("macOS folder picker process failed: %s", exc)
        return None

    if completed.returncode != 0:
        # A non-zero exit is the normal "user clicked Cancel" path
        # (AppleScript error -128) as well as any genuine error; either way
        # there is no folder to set. We deliberately do NOT fall back to Tk
        # here — that is the crash this function exists to avoid.
        logger.debug(
            "macOS folder picker exited with %s: %s",
            completed.returncode,
            (completed.stderr or "").strip(),
        )
        return None

    return completed.stdout.strip() or None


def _pick_folder_tk() -> Optional[str]:
    """Fallback cross-platform folder picker.

    NOTE: never call this on macOS — see ``_pick_folder_macos`` for why Tk on
    the Cocoa main thread aborts the process.
    """
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    root.update_idletasks()
    folder = filedialog.askdirectory(
        title="Select AEMS Exam Storage Folder",
        mustexist=False,
        parent=root,
    )
    root.destroy()
    return folder or None


def _open_folder_picker(config_dir: Path) -> None:
    """Open a native folder picker dialog to set the storage path."""
    try:
        folder: Optional[str]
        if platform.system() == "Windows":
            folder = _pick_folder_windows()
            if folder is None:
                folder = _pick_folder_tk()
        elif platform.system() == "Darwin":
            # macOS MUST use the out-of-process osascript chooser; Tk on the
            # Cocoa main thread crashes the agent (see _pick_folder_macos).
            folder = _pick_folder_macos()
        else:
            folder = _pick_folder_tk()

        if folder:
            config = load_config(config_dir)
            config.storage_path = str(Path(folder).resolve())
            save_config(config, config_dir)
            logger.info("Storage path set via tray: %s", folder)
    except ConfigLoadError as exc:
        logger.error(
            "Storage folder was not changed because config.json is invalid: %s",
            exc,
        )
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

    config = _load_config_for_tray(config_dir)
    icon_color = "green" if config.storage_path else "yellow"
    image = _create_icon_image(icon_color)

    def on_open_settings(icon: Any, item: Any) -> None:
        cfg = _load_config_for_tray(config_dir)
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
        cfg = _load_config_for_tray(config_dir)
        new_color = "green" if cfg.storage_path else "yellow"
        icon.icon = _create_icon_image(new_color)

    def on_show_token(icon: Any, item: Any) -> None:
        token = get_auth_token(config_dir)
        if token:
            if copy_text_to_clipboard(token):
                logger.info("Token copied to clipboard")
            else:
                logger.warning(
                    "Could not copy token to clipboard. Find it in: %s",
                    config_dir / "auth_token",
                )

    def on_quit(icon: Any, item: Any) -> None:
        # Stop the tray loop first so the icon disappears immediately, then
        # force-exit the whole process. Without the os._exit() the uvicorn
        # main thread keeps running headlessly with no tray icon, the port
        # stays bound, and the next launch of the .exe silently fails to
        # bind 127.0.0.1:61234 (Zohar's "tray icon doesn't come back" bug
        # — 2026-05-25). os._exit is intentionally hard-kill: the agent has
        # no critical async state to flush, config writes are synchronous.
        import os as _os

        try:
            icon.stop()
        finally:
            _os._exit(0)

    def _paste_keystroke() -> str:
        """Return the platform-correct paste shortcut label.

        The pairing PIN must be pasted into the AEMS browser tab. macOS uses
        Cmd+V, not Ctrl+V — hard-coding Ctrl+V told Mac users the wrong key.
        """
        return "Cmd+V" if platform.system() == "Darwin" else "Ctrl+V"

    def on_copy_pin(icon: Any, item: Any) -> None:
        """Re-surface the most recent pairing PIN on demand.

        The pairing toast is a transient OS notification that auto-dismisses
        within seconds, and on macOS it never said *where* to paste the PIN.
        This menu item is the persistent recovery channel: it re-copies the
        last PIN to the clipboard and reminds the user where it goes, so a
        vanished toast no longer strands the pairing flow.
        """
        pin = getattr(icon, "_aems_last_pin", None)
        try:
            if not pin:
                icon.notify(
                    "No pairing PIN yet. Click Connect in AEMS Settings, then use this menu.",
                    "AEMS Agent Pairing",
                )
                return
            copied = copy_text_to_clipboard(pin)
            if copied:
                icon.notify(
                    f"Pairing PIN {pin} copied - paste it into the AEMS browser tab "
                    f"with {_paste_keystroke()}.",
                    "AEMS Agent Pairing",
                )
            else:
                icon.notify(f"Pairing PIN: {pin}", "AEMS Agent Pairing")
        except Exception as e:
            logger.debug("Tray PIN re-surface failed: %s", e)

    menu = pystray.Menu(
        pystray.MenuItem("Open Settings", on_open_settings, default=True),
        pystray.MenuItem("Set Storage Folder", on_set_folder),
        pystray.MenuItem("Copy Pairing PIN", on_copy_pin),
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
    # Remember the last pairing PIN so "Copy Pairing PIN" can re-surface it
    # after the transient toast disappears.
    icon._aems_last_pin = None  # type: ignore[attr-defined]

    def _notify_pairing_pin(pin: str, clipboard_ok: bool = False) -> None:
        """Show a tray notification with the pairing PIN.

        The tray toast itself is non-interactive -- the user cannot click to
        copy from it -- and on macOS it auto-dismisses within seconds. The
        agent puts the PIN on the OS clipboard before calling this, and the
        PIN is also stashed on the icon so the "Copy Pairing PIN" menu item
        can re-surface it after the toast fades. We name the platform-correct
        paste key AND the destination (the AEMS browser tab) so the user
        knows exactly what to do.
        """
        # Stash for the persistent "Copy Pairing PIN" menu recovery path.
        icon._aems_last_pin = pin  # type: ignore[attr-defined]
        try:
            if clipboard_ok:
                suffix = (
                    f" (copied to clipboard - paste into the AEMS browser tab "
                    f"with {_paste_keystroke()})"
                )
            else:
                suffix = " - type it into the AEMS browser tab"
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

    thread = threading.Thread(target=run_icon_safely, args=(icon,), daemon=True, name="aems-tray")
    thread.start()

    return thread
