"""Shared drawing for the picture cards the bot attaches to embeds.

A card is a dark panel styled after kingshot.net: a header with a colored dot, rows of
text set in Space Grotesk, thin separators and rounded bars. Layout sizes are in
points; the image is ``SCALE`` times larger, so cards stay sharp on high-density
screens and when opened full size.

The palette comes from the active theme (``card_theme``). A festive theme also
decorates every card: snow in the background, a string of lights along the top edge,
a bauble in place of the header dot and candy stripes on value bars. Decoration never
changes the size of a card or where its content sits, apart from the first header,
which moves down a little to hang clear of the lights.

Everything here is CPU-bound and synchronous: callers on the event loop must go
through ``asyncio.to_thread``. Renderers hold ``RENDER_LOCK`` while drawing, because
FreeType font objects are shared between calls and are not safe to use from several
threads at once.
"""

from __future__ import annotations

import functools
import io
import logging
import random
import threading
import unicodedata
from datetime import timedelta
from pathlib import Path
from typing import Optional

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from services.card_theme import THEME

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

BACKGROUND: Color = THEME.background
LINE: Color = THEME.line
TRACK: Color = THEME.track
PILL: Color = THEME.pill
TODAY_TINT: Color = THEME.today_tint
TEXT: Color = THEME.text
MUTED: Color = THEME.muted
INK: Color = THEME.ink
GOOD: Color = THEME.good
AMBER: Color = THEME.amber
RED: Color = THEME.red
BLUE: Color = THEME.blue
SPECIAL: Color = THEME.special
PODIUM: dict[int, Color] = dict(enumerate(THEME.podium, start=1))

FESTIVE = THEME.festive
# How far the first header moves down to hang clear of the string of lights.
GARLAND_SHIFT = 3.0
# Decoration colors are deliberately not palette colors: a card must show GOOD, RED,
# AMBER or BLUE only where its data does.
_WHITE: Color = (255, 255, 255)
_WIRE: Color = (64, 98, 84)
_BULB_CAP: Color = (120, 146, 134)
_BULBS: tuple[Color, ...] = ((255, 96, 104), (255, 214, 110), (112, 232, 156), (132, 206, 255), (255, 244, 214))
_BAUBLE_CAP: Color = (232, 204, 136)
_RIBBON: Color = (255, 232, 168)

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
    """Cut ``text`` to ``max_width`` pixels, marking the cut with an ellipsis.

    The longest prefix that fits is found by bisection, so a reminder with a
    2,000-character message costs a dozen measurements, not two thousand.
    """
    if text_width(text, font) <= max_width:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if text_width(text[:middle].rstrip() + "…", font) <= max_width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip() + "…" if low else ""


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


# ----------------------------------------------------------------- decoration

# Decoration is drawn this many times larger and scaled down, for smooth edges.
_SUPERSAMPLE = 3
GARLAND_SPANS = 11
GARLAND_HEIGHT = 15.0
BAUBLE_BOX = 12.0
# Where the ball of a bauble sits inside its box.
_BAUBLE_CENTER = (6.0, 7.2)


def _snowflake(draw: ImageDraw.ImageDraw, x: float, y: float, radius: float, fill: Color) -> None:
    """A six-armed crystal, each arm with a pair of barbs."""
    for arm in range(6):
        direction = complex(0, 1) * complex(0.5, 0.8660254) ** arm
        tip = direction * radius
        draw.line((x, y, x + tip.real, y + tip.imag), fill=fill, width=1)
        fork = direction * radius * 0.58
        for turn in (complex(0.5, 0.8660254), complex(0.5, -0.8660254)):
            barb = fork + direction * turn * radius * 0.3
            draw.line((x + fork.real, y + fork.imag, x + barb.real, y + barb.imag), fill=fill, width=1)


@functools.lru_cache(maxsize=2)
def _garland(width: int) -> Image.Image:
    """A string of lights for the top edge of a card ``width`` pixels wide."""
    unit = SCALE * _SUPERSAMPLE
    size = (width * _SUPERSAMPLE, round(GARLAND_HEIGHT * unit))
    glow = Image.new("RGBA", size, (0, 0, 0, 0))
    lights = Image.new("RGBA", size, (0, 0, 0, 0))
    halo, draw = ImageDraw.Draw(glow), ImageDraw.Draw(lights)
    span = CARD_WIDTH / GARLAND_SPANS

    def wire(x: float) -> float:
        """How low the wire hangs at ``x``: pinned at the ends of a span, sagging between."""
        along = (x % span) / span
        return 0.8 + 13.6 * along * (1 - along)

    def box(left: float, top: float, right: float, bottom: float) -> tuple[float, float, float, float]:
        return (left * unit, top * unit, right * unit, bottom * unit)

    steps = int(CARD_WIDTH * 2)
    draw.line(
        [(step / 2 * unit, wire(step / 2) * unit) for step in range(steps + 1)],
        fill=_WIRE + (255,),
        width=max(1, round(0.55 * unit)),
        joint="curve",
    )
    for index in range(GARLAND_SPANS * 2):
        x = (index // 2 + (0.73 if index % 2 else 0.27)) * span
        y = wire(x)
        color = _BULBS[index % len(_BULBS)]
        halo.ellipse(box(x - 5.6, y - 2.4, x + 5.6, y + 8.8), fill=color + (135,))
        draw.rectangle(box(x - 0.8, y - 0.1, x + 0.8, y + 1.3), fill=_BULB_CAP + (255,))
        draw.ellipse(box(x - 1.6, y + 1.0, x + 1.6, y + 5.2), fill=color + (255,))
        draw.ellipse(box(x - 0.95, y + 1.7, x - 0.2, y + 2.9), fill=_WHITE + (150,))
    strip = Image.alpha_composite(glow.filter(ImageFilter.GaussianBlur(2.2 * unit)), lights)
    return strip.resize((width, round(GARLAND_HEIGHT * SCALE)), Image.LANCZOS)


@functools.lru_cache(maxsize=32)
def _bauble(color: Color) -> Image.Image:
    """A tree ornament in ``color``: what a header wears instead of a plain dot."""
    unit = SCALE * _SUPERSAMPLE
    side = round(BAUBLE_BOX * unit)
    image = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    x, y = _BAUBLE_CENTER

    def box(left: float, top: float, right: float, bottom: float) -> tuple[float, float, float, float]:
        return (left * unit, top * unit, right * unit, bottom * unit)

    draw.arc(box(x - 0.95, y - 6.9, x + 0.95, y - 4.9), 0, 360, fill=_BAUBLE_CAP + (255,), width=max(1, round(0.45 * unit)))
    draw.rounded_rectangle(box(x - 1.3, y - 5.4, x + 1.3, y - 3.7), radius=0.4 * unit, fill=_BAUBLE_CAP + (255,))
    draw.ellipse(box(x - 4.1, y - 4.1, x + 4.1, y + 4.1), fill=color + (255,))
    draw.ellipse(box(x - 2.7, y - 2.9, x - 0.9, y - 1.2), fill=mix(_WHITE, color, 0.6) + (255,))
    return image.resize((px(BAUBLE_BOX), px(BAUBLE_BOX)), Image.LANCZOS)


def _backdrop(size: tuple[int, int]) -> Image.Image:
    """The empty card: a plain panel, or a winter night with snow and a string of lights."""
    if not FESTIVE:
        return Image.new("RGB", size, BACKGROUND)
    width, height = size
    fade = Image.new("RGB", (1, 2), BACKGROUND)
    fade.putpixel((0, 1), THEME.background_bottom)
    image = fade.resize(size, Image.BILINEAR)
    draw = ImageDraw.Draw(image)
    # Seeded by the size, so a card of a given size always gets the same snowfall.
    rng = random.Random(width * 8191 + height)
    area = (width / SCALE) * (height / SCALE)
    for _ in range(max(1, round(area / 13000))):
        x, y = rng.uniform(0, width), rng.uniform(px(HEADER_HEIGHT), max(px(HEADER_HEIGHT) + 1, height))
        _snowflake(draw, x, y, SCALE * rng.uniform(4.0, 7.0), mix(_WHITE, BACKGROUND, 0.06))
    for _ in range(round(area / 420)):
        x, y = rng.uniform(0, width), rng.uniform(0, height)
        radius = SCALE * rng.uniform(0.35, 1.05)
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=mix(_WHITE, BACKGROUND, rng.uniform(0.07, 0.2)))
    garland = _garland(width)
    image.paste(garland, (0, 0), garland)
    return image


class Card:
    """A card being drawn. Coordinates are layout points, converted on the way in."""

    def __init__(self, height: float):
        self.height = height
        self.image = _backdrop((px(CARD_WIDTH), px(height)))
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

    def bar(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        fill: Color,
        *,
        radius: Optional[float] = None,
        candy: bool = False,
    ) -> None:
        """A rounded bar. ``candy`` marks a value bar, which a festive theme stripes like a candy cane."""
        if width <= 0:
            return
        radius = height / 2 if radius is None else radius
        box = (px(x), px(y), max(px(x) + 1, px(x + width)), px(y + height))
        self.draw.rounded_rectangle(box, radius=px(radius), fill=fill)
        # Thin or short bars stay plain: stripes would only blur them.
        if candy and FESTIVE and height >= 5 and width >= 6:
            self._candy_stripes(box, px(radius), fill)

    def _candy_stripes(self, box: tuple[int, int, int, int], radius: int, fill: Color) -> None:
        """Lighter diagonal stripes over a bar already drawn in ``fill``."""
        left, top, right, bottom = box
        width, height = right - left + 1, bottom - top + 1
        big_width, big_height = width * 2, height * 2
        stripes = Image.new("L", (big_width, big_height), 0)
        pen = ImageDraw.Draw(stripes)
        for offset in range(-big_height, big_width + big_height, px(5) * 2):
            pen.line((offset, big_height, offset + big_height, 0), fill=255, width=px(1.5) * 2)
        shape = Image.new("L", (big_width, big_height), 0)
        ImageDraw.Draw(shape).rounded_rectangle((0, 0, big_width - 1, big_height - 1), radius=radius * 2, fill=255)
        mask = ImageChops.multiply(stripes, shape).resize((width, height), Image.LANCZOS)
        # Lightened in proportion to the bar's own brightness, so a dimmed bar keeps quiet stripes.
        self.image.paste(mix(_WHITE, fill, 0.3 * max(fill) / 255), (left, top), mask)

    def present(self, x: float, y: float, size: float, fill: Color, *, radius: float = 4.0) -> None:
        """A square mark; a festive theme ties it up with a ribbon and a bow."""
        self.bar(x, y, size, size, fill, radius=radius)
        if not FESTIVE:
            return
        half = px(0.75)
        center_x, center_y = px(x + size / 2), px(y + size / 2)
        self.draw.rectangle((center_x - half, px(y), center_x + half, px(y + size)), fill=_RIBBON)
        self.draw.rectangle((px(x), center_y - half, px(x + size), center_y + half), fill=_RIBBON)
        for side in (-1, 1):
            loop_x = x + size / 2 + side * 1.9
            self.draw.ellipse((px(loop_x - 1.9), px(y - 1.9), px(loop_x + 1.9), px(y + 1.3)), fill=_RIBBON)

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
        if FESTIVE:
            if top == 0:
                middle += GARLAND_SHIFT
            bauble = _bauble(color)
            center_x, center_y = _BAUBLE_CENTER
            self.image.paste(bauble, (px(PADDING + 3.5 - center_x), px(middle - center_y)), bauble)
        else:
            self.draw.ellipse((px(PADDING), px(middle - 3.5), px(PADDING + 7), px(middle + 3.5)), fill=color)
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
