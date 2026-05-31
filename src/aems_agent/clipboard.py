# SPDX-License-Identifier: AGPL-3.0-or-later

"""Native clipboard helpers shared by tray notifications and pairing flows."""

from __future__ import annotations

import logging
import platform
import subprocess

logger = logging.getLogger(__name__)


def copy_text_to_clipboard(text: str) -> bool:
    """Best-effort: place ``text`` on the user's clipboard."""
    system = platform.system()
    try:
        if system == "Windows":
            subprocess.run(["clip"], input=text, text=True, check=True, timeout=5)
            return True
        if system == "Darwin":
            subprocess.run(["pbcopy"], input=text, text=True, check=True, timeout=5)
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
