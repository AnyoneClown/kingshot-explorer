"""Shared drawing for the picture cards the bot attaches to embeds.

A card is a dark panel styled after kingshot.net: a header with a colored dot, rows of
text set in Space Grotesk, thin separators and rounded bars. Layout sizes are in
points; the image is ``SCALE`` times larger, so cards stay sharp on high-density
screens and when opened full size.

Everything here is CPU-bound and synchronous: callers on the event loop must go
through ``asyncio.to_thread``. Renderers hold ``RENDER_LOCK`` while drawing, because
FreeType font objects are shared between calls and are not safe to use from several
threads at once.
"""

from __future__ import annotations

import functools
import io
import logging
import threading
import unicodedata
from datetime import timedelta
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[1]
FONT_PATH = ROOT_DIR / "assets" / "fonts" / "SpaceGrotesk.ttf"
# The card font covers Latin only. Names of players and alliances come in any script,
# so characters it lacks are drawn with the first of these that has them:
# Latin/Cyrillic/Greek, Chinese/Japanese/Korean, Thai, symbols. Scripts that need
# shaping (Arabic, Indic) are not drawn: this Pillow build has no shaping engine.
FALLBACK_FONT_PATHS = tuple(
    ROOT_DIR / "assets" / "fonts" / "fallback" / name
    for name in (
        "NotoSans-Medium.ttf",
        "NotoSansCJKsc-Regular.otf",
        "NotoSansThai-Medium.ttf",
        "NotoSansSymbols2-Regular.ttf",
    )
)

# Layout sizes are in points; the image is SCALE times larger.
SCALE = 2.5
CARD_WIDTH = 440
PADDING = 12
HEADER_HEIGHT = 34

Color = tuple[int, int, int]

# The dark theme of kingshot.net.
BACKGROUND: Color = (34, 31, 29)
LINE: Color = (56, 53, 51)
TRACK: Color = (58, 55, 53)
PILL: Color = (46, 42, 41)
TEXT: Color = (250, 250, 249)
MUTED: Color = (166, 160, 155)
GOOD: Color = (103, 187, 107)
AMBER: Color = (240, 165, 50)
RED: Color = (255, 100, 103)
BLUE: Color = (70, 153, 254)

RENDER_LOCK = threading.Lock()


def px(points: float) -> int:
    return round(points * SCALE)


def mix(color: Color, background: Color, amount: float) -> Color:
    """``color`` at ``amount`` opacity over ``background``."""
    return tuple(round(c * amount + b * (1 - amount)) for c, b in zip(color, background))  # type: ignore[return-value]


@functools.lru_cache(maxsize=64)
def get_font(size: float, weight: int) -> ImageFont.FreeTypeFont:
    loaded = ImageFont.truetype(str(FONT_PATH), px(size))
    try:
        loaded.set_variation_by_axes([weight])
    except (OSError, AttributeError):
        # A FreeType build without variable-font support: the default weight still reads fine.
        logger.debug("Variable font weights are unavailable", exc_info=True)
    return loaded


@functools.lru_cache(maxsize=None)
def _probe(path: str):
    """A small instance of a font file and how it draws a character it does not have."""
    font = ImageFont.truetype(path, 24)
    missing = font.getmask("\uffff")
    return font, missing.size, bytes(missing)


@functools.lru_cache(maxsize=16384)
def _covers(path: str, char: str) -> bool:
    try:
        font, missing_size, missing_bytes = _probe(path)
        mask = font.getmask(char)
    except Exception:
        return False
    return not (mask.size == missing_size and bytes(mask) == missing_bytes)


@functools.lru_cache(maxsize=64)
def _fallback_fonts(pixel_size: int) -> tuple[tuple[str, ImageFont.FreeTypeFont], ...]:
    fonts = []
    for path in FALLBACK_FONT_PATHS:
        try:
            fonts.append((str(path), ImageFont.truetype(str(path), pixel_size)))
        except OSError:
            logger.warning("Fallback font %s is missing; some names will lose characters", path)
    return tuple(fonts)


def text_runs(text: str, font: ImageFont.FreeTypeFont) -> list[tuple[ImageFont.FreeTypeFont, str]]:
    """Split text into runs, each with a font that can draw it.

    Characters no bundled font has (emoji, scripts that need shaping) and invisible
    marks are left out rather than drawn as empty boxes.
    """
    if text.isascii():
        return [(font, text)] if text else []
    primary_path = str(getattr(font, "path", FONT_PATH))
    fallbacks = _fallback_fonts(int(font.size))
    runs: list[tuple[ImageFont.FreeTypeFont, str]] = []
    for char in text:
        if char.isspace():
            # Collapse spaces: a dropped emoji must not leave a double gap.
            if not runs or runs[-1][1].endswith(" "):
                continue
            chosen = runs[-1][0]
            char = " "
        elif unicodedata.category(char) in {"Cf", "Cc", "Cs", "Co"} or "\ufe00" <= char <= "\ufe0f":
            # Invisible controls, joiners and variation selectors.
            continue
        elif char.isascii() or _covers(primary_path, char):
            chosen = font
        else:
            chosen = next((fallback for path, fallback in fallbacks if _covers(path, char)), None)
            if chosen is None:
                continue
        if runs and runs[-1][0] is chosen:
            runs[-1] = (chosen, runs[-1][1] + char)
        else:
            runs.append((chosen, char))
    return runs


def text_width(text: str, font: ImageFont.FreeTypeFont) -> float:
    return sum(run_font.getlength(run) for run_font, run in text_runs(text, font))


def can_draw(text: str, font: ImageFont.FreeTypeFont) -> bool:
    """Whether anything of ``text`` survives: False for a name made only of unsupported characters."""
    return any(run.strip() for _, run in text_runs(text, font))


def ellipsize(text: str, font: ImageFont.FreeTypeFont, max_width: float) -> str:
    if text_width(text, font) <= max_width:
        return text
    cut = text
    while cut and text_width(cut.rstrip() + "…", font) > max_width:
        cut = cut[:-1]
    return cut.rstrip() + "…" if cut else ""


def format_duration(delta: timedelta) -> str:
    """``1d 4h``, ``5h 12m`` or ``42m``, like the countdowns on the site."""
    minutes = max(0, int(delta.total_seconds() // 60))
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {mins}m"
    return f"{mins}m"


class Card:
    """A card being drawn. Coordinates are layout points, converted on the way in."""

    def __init__(self, height: float):
        self.height = height
        self.image = Image.new("RGB", (px(CARD_WIDTH), px(height)), BACKGROUND)
        self.draw = ImageDraw.Draw(self.image)

    def text(self, x: float, baseline: float, text: str, font, fill: Color, *, anchor: str = "ls") -> float:
        """Draw text on a baseline; returns its width in points.

        ``anchor`` is Pillow's: ``l``/``m``/``r`` then ``s`` (baseline) or ``m`` (middle).
        Text that needs fallback fonts is drawn run by run on one shared baseline.
        """
        runs = text_runs(text, font)
        if not runs:
            return 0.0
        if len(runs) == 1 and runs[0][0] is font:
            self.draw.text((px(x), px(baseline)), runs[0][1], font=font, fill=fill, anchor=anchor)
            return font.getlength(runs[0][1]) / SCALE
        total = sum(run_font.getlength(run) for run_font, run in runs)
        left = px(x) - {"l": 0.0, "m": total / 2, "r": total}[anchor[0]]
        y = px(baseline)
        if anchor[1] == "m":
            # Middle of the card font, so every run sits on the same baseline.
            ascent, descent = font.getmetrics()
            y += (ascent - descent) / 2
        for run_font, run in runs:
            self.draw.text((left, y), run, font=run_font, fill=fill, anchor="ls")
            left += run_font.getlength(run)
        return total / SCALE

    def line(self, x1: float, y1: float, x2: float, y2: float, fill: Color = LINE) -> None:
        self.draw.line((px(x1), px(y1), px(x2), px(y2)), fill=fill, width=max(1, px(0.5)))

    def bar(self, x: float, y: float, width: float, height: float, fill: Color, *, radius: Optional[float] = None) -> None:
        if width <= 0:
            return
        radius = height / 2 if radius is None else radius
        self.draw.rounded_rectangle(
            (px(x), px(y), max(px(x) + 1, px(x + width)), px(y + height)), radius=px(radius), fill=fill
        )

    def striped_bar(self, x: float, y: float, width: float, height: float, color: Color, *, radius: float) -> None:
        """A hatched bar: the mark for a date that is only estimated."""
        box_width, box_height = max(2, px(width)), max(2, px(height))
        layer = Image.new("RGB", (box_width, box_height), mix(color, BACKGROUND, 0.28))
        stripes = ImageDraw.Draw(layer)
        step = max(4, px(4))
        for offset in range(-box_height, box_width + box_height, step):
            stripes.line((offset, box_height, offset + box_height, 0), fill=color, width=max(1, px(1.1)))
        mask = Image.new("L", (box_width, box_height), 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, box_width - 1, box_height - 1), radius=px(radius), fill=255)
        self.image.paste(layer, (px(x), px(y)), mask)

    def paste(self, icon: Image.Image, x: float, y: float, box: float) -> None:
        """Paste ``icon`` centered in a square of ``box`` points."""
        size = px(box)
        left = px(x) + (size - icon.width) // 2
        top = px(y) + (size - icon.height) // 2
        self.image.paste(icon, (left, top), icon)

    def badge(self, x: float, baseline: float, text: str, color: Color) -> float:
        """A small outlined label; returns the width it took, in points."""
        font = get_font(7.5, 600)
        width = text_width(text, font) / SCALE + 8
        top = baseline - 9.5
        self.draw.rounded_rectangle(
            (px(x), px(top), px(x + width), px(top + 12.5)),
            radius=px(3),
            outline=mix(color, BACKGROUND, 0.75),
            width=max(1, px(0.6)),
        )
        self.text(x + 4, baseline - 0.4, text, font, color)
        return width

    def header(
        self, top: float, title: str, color: Color, *, count: Optional[int | str] = None, note: str = ""
    ) -> None:
        middle = top + HEADER_HEIGHT / 2
        self.draw.ellipse(
            (px(PADDING), px(middle - 3.5), px(PADDING + 7), px(middle + 3.5)), fill=color
        )
        self.text(PADDING + 14, middle, title, get_font(13.5, 600), TEXT, anchor="lm")
        right = CARD_WIDTH - PADDING
        if count is not None:
            font = get_font(9.5, 500)
            label = str(count)
            width = max(20.0, text_width(label, font) / SCALE + 12)
            self.bar(right - width, middle - 8, width, 16, PILL)
            self.text(right - width / 2, middle, label, font, MUTED, anchor="mm")
            right -= width + 8
        if note:
            self.text(right, middle, note, get_font(9.5, 400), MUTED, anchor="rm")
        self.line(0, top + HEADER_HEIGHT, CARD_WIDTH, top + HEADER_HEIGHT)

    def png(self) -> bytes:
        buffer = io.BytesIO()
        self.image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue()


def compact_number(value) -> str:
    """``12.4B``, ``532M``, ``18.2K`` or a plain number; ``–`` when there is none."""
    try:
        number = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return "–"
    for divisor, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if abs(number) >= divisor:
            return f"{number / divisor:.2f}".rstrip("0").rstrip(".") + suffix
    return f"{int(number):,}"


def as_number(value) -> Optional[float]:
    """A number from an API value, or None when it is missing or not numeric."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
