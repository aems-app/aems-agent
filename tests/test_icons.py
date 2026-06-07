"""Tests for agent icon generation and packaging assets."""

from __future__ import annotations

import importlib
import struct
from pathlib import Path

from PIL import Image

from aems_agent import tray


def test_tray_icon_renders_native_runtime_size() -> None:
    """The live tray icon should be rendered near tray size, not full-canvas."""
    icon = tray._create_icon_image("green")
    assert icon.size == (64, 64)


def test_ensure_windows_icon_creates_multires_ico(tmp_path: Path) -> None:
    """The packaged Windows app should have a real multi-resolution icon asset."""
    icons = importlib.import_module("aems_agent.icons")
    icon_path = tmp_path / "icon.ico"

    icons.ensure_windows_icon(icon_path)

    assert icon_path.exists()
    with Image.open(icon_path) as image:
        assert image.format == "ICO"
        assert set(image.ico.sizes()) >= {(size, size) for size in icons.WINDOWS_ICON_SIZES}


def test_ensure_macos_icns_creates_multires_icns(tmp_path: Path) -> None:
    """The packaged macOS app must have a real multi-resolution .icns asset.

    Apple's Finder picks an icon variant by size; a single-size .icns
    renders blurry in column view and on the dock. We assert the file
    is a real IconFamily container with PNG payloads at every standard
    retina + non-retina slot.
    """
    icons = importlib.import_module("aems_agent.icons")
    icon_path = tmp_path / "aems-agent.icns"

    icons.ensure_macos_icns(icon_path)

    assert icon_path.exists()
    raw = icon_path.read_bytes()
    # Apple IconFamily magic + total size header.
    assert raw[:4] == b"icns", "missing icns magic bytes"
    total_size = struct.unpack(">I", raw[4:8])[0]
    assert total_size == len(raw), "icns total-size header disagrees with file size"

    # Walk the IconFamily entries and confirm every expected slot is present.
    seen: set[bytes] = set()
    offset = 8
    while offset < len(raw):
        type_code = raw[offset : offset + 4]
        entry_size = struct.unpack(">I", raw[offset + 4 : offset + 8])[0]
        assert entry_size >= 8, "icns entry header smaller than 8 bytes"
        # Payload must be a PNG (10.7+ icon family).
        payload = raw[offset + 8 : offset + entry_size]
        assert payload[:8] == b"\x89PNG\r\n\x1a\n", f"entry {type_code!r} payload is not PNG"
        seen.add(type_code)
        offset += entry_size
    assert offset == len(raw), "icns walker overran the buffer"

    required = {
        b"icp4",
        b"icp5",
        b"ic07",
        b"ic08",
        b"ic09",
        b"ic10",
        b"ic11",
        b"ic12",
        b"ic13",
        b"ic14",
    }
    missing = required - seen
    assert not missing, f"icns missing required entries: {sorted(missing)}"
