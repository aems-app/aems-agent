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


def _macos_launch_uri_arg() -> str | None:
    """Return the ``aems-agent://`` URI if the frozen app was launched via the
    custom URL scheme with the URI placed in ``argv``.

    The web app's "Launch agent" button fires ``aems-agent://launch`` to ask
    macOS Launch Services to start the installed agent. With
    ``argv_emulation=False`` macOS normally delivers that URL as a
    ``kAEGetURL`` Apple Event (so ``argv`` stays bare and
    ``_is_frozen_macos_finder_launch`` already handles it). This is a
    belt-and-suspenders guard for macOS/PyInstaller configurations that pass
    the URL through ``argv`` instead — without it Typer would try to parse
    ``aems-agent://launch`` as a subcommand and exit with "No such command".
    """
    if not (getattr(sys, "frozen", False) and platform.system() == "Darwin"):
        return None
    for arg in sys.argv[1:]:
        if arg.startswith("aems-agent://"):
            return arg
    return None


if __name__ == "__main__":
    if _is_frozen_macos_finder_launch() or _macos_launch_uri_arg() is not None:
        # Default a no-arg macOS Finder launch -- or an aems-agent:// custom
        # scheme launch -- into `run --tray` so the bundle boots the menu-bar
        # agent. This mirrors what ``com.aems.agent.plist`` LaunchAgent does
        # at login. Rebuild argv from scratch so any URL argument is dropped
        # (Typer must not see it as a subcommand).
        sys.argv = [sys.argv[0], "run", "--tray"]
    main()
