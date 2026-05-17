# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared icon rendering for the AEMS Local Bridge Agent."""

from __future__ import annotations

from pathlib import Path
from typing import Any

STATUS_COLORS: dict[str, tuple[int, int, int, int]] = {
    "green": (46, 160, 67, 255),
    "yellow": (210, 158, 14, 255),
    "red": (207, 34, 46, 255),
}
RUNTIME_ICON_SIZE = 64
WINDOWS_ICON_SIZES: tuple[int, ...] = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def render_status_icon(color: str = "green", size: int = RUNTIME_ICON_SIZE) -> Any:
    """Render the tray/taskbar icon at the requested size."""
    from PIL import Image, ImageDraw

    bg = STATUS_COLORS.get(color, STATUS_COLORS["green"])
    inset = max(2, round(size * 0.08))
    radius = max(4, round(size * 0.18))
    stroke_w = max(3, round(size * 0.14))

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (inset, inset, size - inset - 1, size - inset - 1),
        radius=radius,
        fill=bg,
    )

    pts = [
        (size * 0.24, size * 0.54),
        (size * 0.42, size * 0.71),
        (size * 0.76, size * 0.34),
    ]
    mask = Image.new("L", (size, size), 0)
    mask_draw = ImageDraw.Draw(mask)
    mask_draw.line(pts, fill=255, width=stroke_w, joint="curve")
    cap_radius = max(2, stroke_w // 2)
    for x, y in pts:
        mask_draw.ellipse(
            [(x - cap_radius, y - cap_radius), (x + cap_radius, y + cap_radius)],
            fill=255,
        )

    white = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    img.paste(white, (0, 0), mask)
    return img


def ensure_windows_icon(path: Path) -> Path:
    """Generate the Windows ICO asset with multiple embedded sizes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    largest = max(WINDOWS_ICON_SIZES)
    base = render_status_icon("green", size=largest)
    base.save(path, format="ICO", sizes=[(size, size) for size in WINDOWS_ICON_SIZES])
    return path
