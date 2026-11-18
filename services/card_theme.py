"""Which look the bot wears: the Christmas one or the everyday one.

``BOT_THEME`` picks it when the process starts: ``christmas`` (the default) or
``classic``. A theme is the palette of the picture cards, the colors of the embeds
around them and whether cards get their seasonal decoration (snow, a string of
lights, baubles, candy stripes).

The palette is read at import time, by modules that are imported before ``main``
loads ``.env``, so the setting is also looked up in that file here (without touching
the environment of the process).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dotenv import dotenv_values

logger = logging.getLogger(__name__)

Color = tuple[int, int, int]
Rgba = tuple[int, int, int, int]

DEFAULT_THEME = "christmas"


@dataclass(frozen=True)
class Theme:
    name: str
    # Snow, lights, baubles and candy stripes on the picture cards.
    festive: bool

    # Picture cards.
    background: Color
    # The card fades to this color at its bottom edge (the same color: no fade).
    background_bottom: Color
    line: Color
    track: Color
    pill: Color
    today_tint: Color
    text: Color
    muted: Color
    # Text drawn on a bright fill, such as the number on a podium badge.
    ink: Color
    good: Color
    amber: Color
    red: Color
    blue: Color
    special: Color
    podium: tuple[Color, Color, Color]

    # Embeds (0xRRGGBB).
    embed_success: int
    embed_warning: int
    embed_error: int
    embed_info: int
    embed_neutral: int
    embed_highlight: int

    # The /player card: badge backgrounds and their text.
    hero_level_badge: tuple[Rgba, Rgba]
    gear_level_badge: tuple[Rgba, Rgba]
    mystery_level_badge: tuple[Rgba, Rgba]
    charm_level_badge: tuple[Rgba, Rgba]
    empty_charm: Rgba
    separator: Rgba
    # Two colors make the separator a candy cane; None keeps it a plain line.
    separator_stripe: Rgba | None


# The dark theme of kingshot.net.
CLASSIC = Theme(
    name="classic",
    festive=False,
    background=(34, 31, 29),
    background_bottom=(34, 31, 29),
    line=(56, 53, 51),
    track=(58, 55, 53),
    pill=(46, 42, 41),
    today_tint=(42, 39, 37),
    text=(250, 250, 249),
    muted=(166, 160, 155),
    ink=(34, 31, 29),
    good=(103, 187, 107),
    amber=(240, 165, 50),
    red=(255, 100, 103),
    blue=(70, 153, 254),
    special=(74, 168, 255),
    podium=((240, 190, 70), (196, 200, 208), (205, 135, 80)),
    embed_success=0x2ECC71,
    embed_warning=0xE67E22,
    embed_error=0xE74C3C,
    embed_info=0x3498DB,
    embed_neutral=0x5865F2,
    embed_highlight=0xE6A23C,
    hero_level_badge=((24, 46, 86, 225), (214, 234, 255, 255)),
    gear_level_badge=((35, 39, 52, 230), (255, 236, 160, 255)),
    mystery_level_badge=((62, 38, 86, 230), (232, 214, 255, 255)),
    charm_level_badge=((20, 24, 32, 220), (255, 255, 255, 255)),
    empty_charm=(70, 78, 92, 150),
    separator=(128, 132, 148, 110),
    separator_stripe=None,
)

# A winter night under the tree: deep pine, snow white, holly green, berry red, gold.
CHRISTMAS = Theme(
    name="christmas",
    festive=True,
    background=(15, 35, 30),
    background_bottom=(10, 25, 24),
    line=(38, 64, 55),
    track=(41, 68, 59),
    pill=(29, 55, 47),
    today_tint=(24, 50, 42),
    text=(247, 250, 246),
    muted=(152, 178, 166),
    ink=(13, 31, 27),
    good=(96, 210, 134),
    amber=(246, 192, 82),
    red=(238, 82, 90),
    blue=(124, 200, 242),
    special=(176, 196, 255),
    podium=((250, 206, 96), (208, 218, 224), (216, 144, 96)),
    embed_success=0x3FBF74,
    embed_warning=0xF2B84B,
    embed_error=0xD9414E,
    embed_info=0x7CC8F2,
    embed_neutral=0xEEF4F1,
    embed_highlight=0xF6C052,
    hero_level_badge=((16, 74, 54, 232), (236, 250, 240, 255)),
    gear_level_badge=((14, 44, 36, 235), (255, 226, 140, 255)),
    mystery_level_badge=((24, 58, 92, 232), (216, 238, 255, 255)),
    charm_level_badge=((12, 36, 30, 225), (255, 255, 255, 255)),
    empty_charm=(58, 94, 80, 150),
    separator=(214, 48, 58, 255),
    separator_stripe=(248, 250, 248, 255),
)

THEMES: dict[str, Theme] = {theme.name: theme for theme in (CHRISTMAS, CLASSIC)}


def resolve_theme(name: str | None) -> Theme:
    """The theme called ``name``; the default one when it is empty or unknown."""
    key = (name or "").strip().lower()
    if not key:
        return THEMES[DEFAULT_THEME]
    if key not in THEMES:
        logger.warning("Unknown BOT_THEME %r: using %r (known: %s)", name, DEFAULT_THEME, ", ".join(THEMES))
        return THEMES[DEFAULT_THEME]
    return THEMES[key]


THEME = resolve_theme(os.getenv("BOT_THEME") or dotenv_values().get("BOT_THEME"))
