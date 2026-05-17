"""Tests for agent icon generation and packaging assets."""

from __future__ import annotations

import importlib
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
