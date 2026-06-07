# SPDX-License-Identifier: AGPL-3.0-or-later

"""Shared icon rendering for the AEMS Local Bridge Agent.

Two icon treatments intentionally exist:

- The live tray icon is a rounded-rectangle status badge in green,
  yellow, or red with the AEMS glyph overlaid in white.
- The packaged macOS / Windows app icon uses the same glyph on the
  brand-navy product badge so Finder, Spotlight, Start, and the taskbar
  read it as app identity rather than runtime state.

The glyph mask is the AEMS website favicon rasterised at 512x512 and
shipped as ``assets/aems-logo-mask.png``. We load only its alpha
channel, resize to the requested target size, and composite it onto the
relevant badge fill.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

STATUS_COLORS: dict[str, tuple[int, int, int, int]] = {
    "green": (46, 160, 67, 255),
    "yellow": (210, 158, 14, 255),
    "red": (207, 34, 46, 255),
}
RUNTIME_ICON_SIZE = 64
WINDOWS_ICON_SIZES: tuple[int, ...] = (16, 20, 24, 32, 40, 48, 64, 128, 256)

# AEMS brand navy — matches `--brand-primary: #004791` from the aems-web
# design tokens (src/aems/web/static/css/design-system/tokens.css).
# Used for the macOS / Finder / Applications / Spotlight app icon so it
# reads as product identity, NOT as the tray status badge whose green
# variant means "agent is running".
APP_ICON_BRAND_COLOR: tuple[int, int, int, int] = (0, 71, 145, 255)

# The glyph occupies ~75% of the favicon viewBox. Inset the rendered
# mask further inside the badge so it doesn't kiss the rounded edges.
_GLYPH_INSET_RATIO = 0.18

# App icon — a touch more padding around the glyph than the tray
# badge, matching Apple HIG guidance that the central subject of a
# macOS app icon occupy ~62% of the bounds.
_APP_ICON_GLYPH_INSET_RATIO = 0.19

# Pre-rendered SVG at 512x512 — high enough that downscaling to 16x16
# tray icons stays crisp under Pillow's LANCZOS resampler.
_LOGO_MASK_FILENAME = "aems-logo-mask.png"


@lru_cache(maxsize=1)
def _load_logo_alpha() -> Any:
    """Return the AEMS glyph as an L-mode Pillow image (alpha only).

    Cached: every tray re-render at the same size pays one resize cost,
    not a full PNG decode.
    """
    from PIL import Image

    # importlib.resources works under both source installs and frozen
    # PyInstaller bundles. The PyInstaller spec must include
    # ``assets/aems-logo-mask.png`` via the ``datas=`` argument.
    try:
        ref = resources.files("aems_agent.assets").joinpath(_LOGO_MASK_FILENAME)
        with resources.as_file(ref) as path:
            img = Image.open(path).convert("RGBA")
    except (FileNotFoundError, ModuleNotFoundError):
        # Fallback for unusual layouts — look adjacent to this module.
        here = Path(__file__).resolve().parent / "assets" / _LOGO_MASK_FILENAME
        img = Image.open(here).convert("RGBA")

    # We only need the alpha channel — colour will come from the badge.
    return img.split()[-1]


def render_status_icon(color: str = "green", size: int = RUNTIME_ICON_SIZE) -> Any:
    """Render the tray/taskbar icon at the requested size.

    Args:
        color: one of ``"green"``, ``"yellow"``, ``"red"`` (other values
            fall back to green to match the legacy behaviour).
        size: edge length in pixels.

    Returns:
        A Pillow :class:`Image.Image` in ``RGBA`` mode.
    """
    from PIL import Image, ImageDraw

    bg = STATUS_COLORS.get(color, STATUS_COLORS["green"])
    inset = max(2, round(size * 0.08))
    radius = max(4, round(size * 0.18))

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (inset, inset, size - inset - 1, size - inset - 1),
        radius=radius,
        fill=bg,
    )

    # Composite the AEMS glyph in white on top of the badge.
    glyph_size = max(1, round(size * (1 - 2 * _GLYPH_INSET_RATIO)))
    glyph_origin = (size - glyph_size) // 2
    alpha = _load_logo_alpha().resize((glyph_size, glyph_size), Image.Resampling.LANCZOS)

    white = Image.new("RGBA", (glyph_size, glyph_size), (255, 255, 255, 255))
    img.paste(white, (glyph_origin, glyph_origin), alpha)
    return img


def render_app_icon(size: int = RUNTIME_ICON_SIZE) -> Any:
    """Render the macOS / Windows app icon at the requested size.

    Different from :func:`render_status_icon`. The tray badge encodes
    state — green = running, yellow = no storage, red = error — and
    that traffic-light treatment is the wrong language for the
    Finder / Applications / Spotlight icon, which should encode
    *product identity*. This renderer uses AEMS brand navy
    (``--brand-primary`` from the aems-web design tokens) with the
    AEMS glyph composited in white at Apple-HIG-appropriate inset.

    Apple's macOS app-icon mask is applied by the OS at draw time when
    the icon is loaded out of a `.icns`, so we render a filled
    rounded-rectangle and let macOS round it to the system squircle on
    its own. We still use a generous corner radius so the standalone
    PNG version (e.g. embedded in the Windows ICO) reads as a
    contemporary app icon rather than a flat sticker.
    """
    from PIL import Image, ImageDraw

    bg = APP_ICON_BRAND_COLOR
    inset = max(2, round(size * 0.04))
    radius = max(4, round(size * 0.225))  # Apple squircle approximation

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle(
        (inset, inset, size - inset - 1, size - inset - 1),
        radius=radius,
        fill=bg,
    )

    glyph_size = max(1, round(size * (1 - 2 * _APP_ICON_GLYPH_INSET_RATIO)))
    glyph_origin = (size - glyph_size) // 2
    alpha = _load_logo_alpha().resize((glyph_size, glyph_size), Image.Resampling.LANCZOS)
    white = Image.new("RGBA", (glyph_size, glyph_size), (255, 255, 255, 255))
    img.paste(white, (glyph_origin, glyph_origin), alpha)
    return img


def ensure_windows_icon(path: Path) -> Path:
    """Generate the Windows ICO asset with multiple embedded sizes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    largest = max(WINDOWS_ICON_SIZES)
    base = render_app_icon(size=largest)
    base.save(path, format="ICO", sizes=[(size, size) for size in WINDOWS_ICON_SIZES])
    return path


# Apple IconFamily slot codes, paired with the pixel edge length each slot
# expects. The set mirrors what Finder, the Dock, and Spotlight pick
# from on retina + non-retina displays.
_MACOS_ICNS_ENTRIES: tuple[tuple[bytes, int], ...] = (
    (b"icp4", 16),  # 16x16
    (b"icp5", 32),  # 32x32
    (b"ic07", 128),  # 128x128
    (b"ic08", 256),  # 256x256
    (b"ic09", 512),  # 512x512
    (b"ic10", 1024),  # 1024x1024 / 512@2x
    (b"ic11", 32),  # 16@2x
    (b"ic12", 64),  # 32@2x
    (b"ic13", 256),  # 128@2x
    (b"ic14", 512),  # 256@2x
)


def ensure_macos_icns(path: Path) -> Path:
    """Generate the macOS ICNS asset with multiple embedded sizes.

    Pillow's built-in ICNS writer only emits a single size and is
    unreliable across platforms, so we assemble a real Apple IconFamily
    container ourselves: PNG payload per slot, big-endian length
    prefix, 'icns' magic + total-size header. Format reference:
    https://en.wikipedia.org/wiki/Apple_Icon_Image_format

    Apple's Finder uses these slots to render at different zoom levels;
    shipping a single 1024px source produces visibly blurry icons in
    column view and on the Dock at 16-32px.
    """
    import io
    import struct

    path.parent.mkdir(parents=True, exist_ok=True)

    body = bytearray()
    for type_code, size in _MACOS_ICNS_ENTRIES:
        # Use the product-identity app icon (brand navy) here, NOT
        # render_status_icon — the tray badge's green/yellow/red
        # palette encodes runtime state, which is the wrong visual
        # language for the Finder / Applications / Spotlight icon.
        img = render_app_icon(size=size)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        payload = buf.getvalue()
        entry_size = len(payload) + 8  # 4-byte type code + 4-byte length header
        body += type_code
        body += struct.pack(">I", entry_size)
        body += payload

    total_size = 8 + len(body)
    with open(path, "wb") as f:
        f.write(b"icns")
        f.write(struct.pack(">I", total_size))
        f.write(bytes(body))

    return path
