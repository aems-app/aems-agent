# SPDX-License-Identifier: AGPL-3.0-or-later

"""
Top-level PyInstaller entry point for the AEMS Local Bridge Agent.

PyInstaller invokes the entry script as ``__main__``, which strips the
package context.  If we pointed the spec at ``src/aems_agent/cli.py``,
its ``from .config import ...`` relative imports would blow up at
startup with ``ImportError: attempted relative import with no known
parent package`` (which is exactly the v0.3.2 release defect).

This wrapper imports the CLI through its fully-qualified package name
so the relative imports inside the package work normally.
"""

import platform
import sys

from aems_agent.cli import main


def _is_frozen_macos_finder_launch() -> bool:
    """Return True when the frozen .app was launched by Finder with no args.

    The bundle declares ``LSUIElement=True``, which means a Finder
    double-click on ``AEMS Agent.app`` invokes ``Contents/MacOS/aems-agent``
    directly with no subcommand. The Typer CLI requires a subcommand, so
    without this guard the process exits immediately with "Missing
    command." and the user sees no window, no tray icon, and no error
    dialog (because we are LSUIElement). That is exactly the
    "double-click does nothing" symptom Apple testers reported.

    The PyInstaller spec sets ``argv_emulation=False``, so Finder
    double-clicks land here with ``sys.argv == [executable_path]`` — no
    ``-psn_X_Y`` stragglers to strip. Anything longer means the user
    invoked the binary from Terminal with arguments, which we leave
    alone.
    """
    return getattr(sys, "frozen", False) and platform.system() == "Darwin" and len(sys.argv) <= 1


if __name__ == "__main__":
    if _is_frozen_macos_finder_launch():
        # Default a no-arg macOS Finder launch into `run --tray` so the
        # bundle boots the menu-bar agent. This mirrors what
        # ``com.aems.agent.plist`` LaunchAgent does at login.
        sys.argv.extend(["run", "--tray"])
    main()
