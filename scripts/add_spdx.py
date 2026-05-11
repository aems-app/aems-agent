#!/usr/bin/env python3
"""Idempotently add SPDX-License-Identifier headers to src/aems_agent/**/*.py.

Contract (matches docs/superpowers/specs/2026-05-10-personal-plan-free-launch-design.md section 5.2):

- Skip files that already contain `SPDX-License-Identifier:` in the first 5
  non-blank lines (whether AGPL or otherwise).
- Insert position: immediately after the shebang (`#!`) line if present, else
  immediately after the encoding declaration (`# -*- coding:`) if present,
  else as the very first line.
- Insert format: a single comment line `# SPDX-License-Identifier: AGPL-3.0-or-later`
  followed by a blank line.
- Dry-run flag (`--dry-run`) prints planned edits without writing.
- Targets `src/aems_agent/**/*.py` only; never touches `tests/` or `scripts/`.

Usage:
    python scripts/add_spdx.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SPDX_LINE = "# SPDX-License-Identifier: AGPL-3.0-or-later"


def _has_spdx(lines: list[str]) -> bool:
    """Return True if any of the first 5 non-blank lines contains an SPDX identifier."""
    non_blank = 0
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if "SPDX-License-Identifier:" in stripped:
            return True
        non_blank += 1
        if non_blank >= 5:
            break
    return False


def _insert_index(lines: list[str]) -> int:
    """Pick the insertion index per the documented contract."""
    if not lines:
        return 0
    idx = 0
    # Shebang
    if lines[0].startswith("#!"):
        idx = 1
    # Encoding declaration (PEP 263) — must be on line 1 or 2
    if idx < len(lines) and lines[idx].lstrip().startswith("# -*-") and "coding:" in lines[idx]:
        idx += 1
    elif idx == 0 and len(lines) > 1 and lines[1].lstrip().startswith("# -*-") and "coding:" in lines[1]:
        # Edge case: no shebang but encoding on line 2; not standard, but be safe.
        idx = 2
    return idx


def add_spdx(path: Path, dry_run: bool = False) -> bool:
    """Add the SPDX header to `path`. Return True if a change was made."""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)

    if _has_spdx(lines):
        return False

    idx = _insert_index(lines)
    insertion = [SPDX_LINE + "\n", "\n"]
    new_lines = lines[:idx] + insertion + lines[idx:]
    new_text = "".join(new_lines)

    if dry_run:
        print(f"[dry-run] would patch: {path}")
    else:
        path.write_text(new_text, encoding="utf-8")
        print(f"patched: {path}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Add SPDX headers to src/aems_agent/**/*.py.")
    parser.add_argument("--dry-run", action="store_true", help="Show planned edits, do not write.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    target_root = repo_root / "src" / "aems_agent"

    if not target_root.exists():
        print(f"error: {target_root} not found", file=sys.stderr)
        return 1

    py_files = sorted(target_root.rglob("*.py"))
    patched = 0
    skipped = 0
    for path in py_files:
        if add_spdx(path, dry_run=args.dry_run):
            patched += 1
        else:
            skipped += 1

    print(f"\nDone: {patched} patched, {skipped} already had headers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
