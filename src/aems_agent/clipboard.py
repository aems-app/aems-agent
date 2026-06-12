# SPDX-License-Identifier: AGPL-3.0-or-later

"""Native clipboard helpers shared by tray notifications and pairing flows."""

from __future__ import annotations

import logging
import os
import platform
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def windows_system32_dir() -> Path:
    """Return the Windows System32 directory.

    System binaries are invoked by absolute path: on Windows,
    ``CreateProcess`` resolves bare executable names against the current
    working directory before PATH, so a planted ``clip.exe`` /
    ``powershell.exe`` next to wherever the agent was launched from would
    otherwise be executed.
    """
    return Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"


def copy_text_to_clipboard(text: str) -> bool:
    """Best-effort: place ``text`` on the user's clipboard."""
    system = platform.system()
    try:
        if system == "Windows":
            clip_exe = str(windows_system32_dir() / "clip.exe")
            subprocess.run([clip_exe], input=text, text=True, check=True, timeout=5)
            return True
        if system == "Darwin":
            subprocess.run(["/usr/bin/pbcopy"], input=text, text=True, check=True, timeout=5)
            return True
        if system == "Linux":
            for argv in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
                try:
                    subprocess.run(argv, input=text, text=True, check=True, timeout=5)
                    return True
                except (FileNotFoundError, subprocess.CalledProcessError):
                    continue
    except Exception as exc:  # pragma: no cover - platform-dependent
        logger.debug("Clipboard copy failed: %s", exc)
    return False
