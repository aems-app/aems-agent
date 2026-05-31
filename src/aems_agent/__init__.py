# SPDX-License-Identifier: AGPL-3.0-or-later

"""
AEMS Local Bridge Agent.

A lightweight companion service running on localhost that provides REST API
access to the local filesystem, enabling any browser to read/write exam PDFs
to a user-chosen folder (e.g., D:\\Exams).

Usage:
    aems-agent run [--port 61234] [--host 127.0.0.1]
    aems-agent token
    aems-agent set-path <path>
    aems-agent config-dir
"""

from . import clipboard

try:
    from importlib.metadata import version as _pkg_version

    __version__ = _pkg_version("aems-agent")
except Exception:
    __version__ = "0.0.0-dev"

__all__ = ["__version__", "clipboard"]
