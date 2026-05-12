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

from aems_agent.cli import main

if __name__ == "__main__":
    main()
