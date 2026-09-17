"""Antigona portrait art — half-block ANSI renderer for the CLI banner.

Renders ``assets/antigona_banner.png`` as true-color half-block terminal art
(2 vertical pixels per terminal row). Falls back gracefully to the existing
ASCII banner when Pillow or the asset is unavailable.

The art supports a subtle "breathing" animation: a low-amplitude brightness
modulation applied on each redraw, plus a status accent line rendered below
the art. All animation is pure re-render — no terminal-extended protocols,
so it works in Termux, GNOME Terminal, iTerm and Windows Terminal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeAlias

#: One half-block cell: (foreground hex, background hex, glyph).
Cell: TypeAlias = tuple[str, str, str]
#: One terminal row: a tuple of cells.
Row: TypeAlias = tuple[Cell, ...]
#: Loaded art: (number of terminal rows, rows).
LoadedArt: TypeAlias = tuple[int, tuple[Row, ...]]

try:
    from PIL import Image as _PILImage
except Exception:  # pragma: no cover - Pillow optional
    _PILImage = None  # type: ignore[assignment]

_ART_CACHE: dict[int, LoadedArt] = {}


def _asset_path() -> Path | None:
    """Resolve assets/antigona_banner.png relative to the source tree."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "assets" / "antigona_banner.png"
        if candidate.is_file():
            return candidate
    return None


def available() -> bool:
    """Whether portrait art rendering is possible in this environment."""
    return _PILImage is not None and _asset_path() is not None


def _load_art(width: int) -> LoadedArt | None:
    """Load the banner, quantize to ``width`` columns, cache by width.

    Returns (terminal_rows, rows) where each row is a tuple of (fg, bg, chr)
    half-block cells. Each terminal row covers two image rows.
    """
    if _PILImage is None:
        return None
    if width in _ART_CACHE:
        return _ART_CACHE[width]
    asset = _asset_path()
    if asset is None:
        return None
    try:
        img = _PILImage.open(asset).convert("RGBA")
    except Exception:
        return None
    target_w = min(max(width - 4, 20), 60)
    scale = target_w / img.width
    h = max(1, int(img.height * scale * 0.5))
    try:
        img = img.resize((target_w, h * 2), _PILImage.Resampling.LANCZOS)
    except Exception:
        img = img.resize((target_w, h * 2))
    rows: list[Row] = []
    for y in range(0, h * 2, 2):
        row: list[Cell] = []
        for x in range(target_w):
            top_px = img.getpixel((x, y))
            bot_px = img.getpixel((x, y + 1)) if y + 1 < h * 2 else (0, 0, 0, 255)
            top = top_px if isinstance(top_px, tuple) else (0, 0, 0, 255)
            bot = bot_px if isinstance(bot_px, tuple) else (0, 0, 0, 255)
            row.append((_rgb(top), _rgb(bot), "\u2580"))
        rows.append(tuple(row))
    result = (h, tuple(rows))
    _ART_CACHE[width] = result
    return result


def _rgb(px: Any) -> str:
    if not isinstance(px, tuple) or len(px) < 3:
        return "#000000"
    r, g, b = int(px[0]), int(px[1]), int(px[2])
    a = int(px[3]) if len(px) > 3 else 255
    if a < 8:
        return "#000000"
    return f"#{r:02x}{g:02x}{b:02x}"


def art_lines(width: int, *, breathe: float = 0.0) -> list[str]:
    """Render banner art as ANSI-truecolor lines."""
    if not available():
        return []
    loaded = _load_art(width)
    if loaded is None:
        return []
    _, rows = loaded
    lines: list[str] = []
    for row in rows:
        parts: list[str] = []
        for fg, bg, ch in row:
            if breathe:
                fg = _brighten(fg, breathe)
                bg = _brighten(bg, breathe)
            parts.append(f"\x1b[38;2;{_hex_rgb(fg)}m\x1b[48;2;{_hex_rgb(bg)}m{ch}")
        lines.append("".join(parts) + "\x1b[0m")
    return lines


def _hex_rgb(hexcolor: str) -> str:
    h = hexcolor.lstrip("#")
    return f"{int(h[0:2],16)};{int(h[2:4],16)};{int(h[4:6],16)}"


def _brighten(hexcolor: str, amount: float) -> str:
    h = hexcolor.lstrip("#")
    r = min(255, int(int(h[0:2], 16) + amount * 18))
    g = min(255, int(int(h[2:4], 16) + amount * 18))
    b = min(255, int(int(h[4:6], 16) + amount * 18))
    return f"#{r:02x}{g:02x}{b:02x}"
