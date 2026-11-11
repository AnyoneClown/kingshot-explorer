"""PNG cards for ranked lists: a leaderboard, and two kingdoms' rankings side by side.

Used by ``/scout`` (Mystic Trial leaderboard) and the ``/kvk prep`` dossier (top
alliances, top players by power and by kills). Bars are scaled to the largest value on
the card, so the two kingdoms of a dossier can be compared at a glance.

CPU-bound and synchronous: callers on the event loop go through ``asyncio.to_thread``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from services.card_drawing import (
    AMBER,
    BLUE,
    CARD_WIDTH,
    HEADER_HEIGHT,
    MUTED,
    PADDING,
    RENDER_LOCK,
    TEXT,
    TRACK,
    Card,
    Color,
    can_draw,
    compact_number,
    ellipsize,
    get_font,
    px,
)

LEADERBOARD_FILENAME = "leaderboard.png"
LEADERBOARD_ROW_HEIGHT = 22
MAX_LEADERBOARD_ROWS = 15

VERSUS_COLUMN_HEADER = 20
VERSUS_ROW_HEIGHT = 26
MAX_VERSUS_ROWS = 5

PODIUM: dict[int, Color] = {1: (240, 190, 70), 2: (196, 200, 208), 3: (205, 135, 80)}
SIDE_A: Color = BLUE
SIDE_B: Color = AMBER


@dataclass(frozen=True)
class RankedRow:
    """One line of a ranking. ``value`` drives the bar; ``text`` is what is printed."""

    name: str
    value: Optional[float] = None
    text: str = ""
    rank: Optional[int] = None
    tag: str = ""

    @property
    def label(self) -> str:
        return f"[{self.tag}] {self.name}" if self.tag else self.name

    @property
    def shown_value(self) -> str:
        return self.text or compact_number(self.value)


UNSUPPORTED_NAME = "name in an unsupported script"


def _name(card: Card, x: float, y: float, row: RankedRow, font, max_width: float, *, anchor: str) -> None:
    """Draw a row's name, or say so when none of its characters can be drawn."""
    if can_draw(row.name, font):
        card.text(x, y, ellipsize(row.label, font, px(max_width)), font, TEXT, anchor=anchor)
    else:
        note = f"[{row.tag}] {UNSUPPORTED_NAME}" if row.tag else UNSUPPORTED_NAME
        card.text(x, y, ellipsize(note, font, px(max_width)), font, MUTED, anchor=anchor)


def _largest(rows: Sequence[RankedRow]) -> float:
    return max((row.value or 0 for row in rows), default=0) or 1


# -------------------------------------------------------------- leaderboard


def render_leaderboard_card(
    title: str,
    rows: Sequence[RankedRow],
    *,
    note: str = "",
    accent: Color = BLUE,
) -> bytes:
    """A ranked list with a bar per row; the first three places get podium colors."""
    shown = list(rows[:MAX_LEADERBOARD_ROWS])
    largest = _largest(shown)
    with RENDER_LOCK:
        card = Card(HEADER_HEIGHT + len(shown) * LEADERBOARD_ROW_HEIGHT + 10)
        card.header(0, title, accent, count=len(rows), note=note)
        name_font, value_font, rank_font = get_font(10.5, 500), get_font(10.5, 600), get_font(9.5, 700)
        name_left = PADDING + 26
        bar_left, bar_right = PADDING + 176, CARD_WIDTH - PADDING - 74
        for index, row in enumerate(shown):
            middle = HEADER_HEIGHT + 5 + index * LEADERBOARD_ROW_HEIGHT + LEADERBOARD_ROW_HEIGHT / 2
            rank = row.rank if row.rank is not None else index + 1
            color = PODIUM.get(rank)
            if color is not None:
                card.bar(PADDING, middle - 8, 18, 16, color, radius=4)
                card.text(PADDING + 9, middle, str(rank), rank_font, (34, 31, 29), anchor="mm")
            else:
                card.text(PADDING + 9, middle, str(rank), rank_font, MUTED, anchor="mm")
            _name(card, name_left, middle, row, name_font, bar_left - name_left - 8, anchor="lm")
            card.bar(bar_left, middle - 3, bar_right - bar_left, 6, TRACK)
            if row.value:
                card.bar(bar_left, middle - 3, (bar_right - bar_left) * min(1.0, row.value / largest), 6, color or accent)
            card.text(CARD_WIDTH - PADDING, middle, row.shown_value, value_font, TEXT, anchor="rm")
        return card.png()


# ------------------------------------------------------------------- versus


@dataclass(frozen=True)
class VersusSection:
    """One ranking for both kingdoms. A side is None when its data is unavailable."""

    title: str
    note: str = ""
    rows_a: Optional[Sequence[RankedRow]] = None
    rows_b: Optional[Sequence[RankedRow]] = None

    @property
    def row_count(self) -> int:
        return max(1, min(MAX_VERSUS_ROWS, max(len(self.rows_a or ()), len(self.rows_b or ()))))

    @property
    def height(self) -> float:
        return HEADER_HEIGHT + VERSUS_COLUMN_HEADER + self.row_count * VERSUS_ROW_HEIGHT + 6


def _versus_column(
    card: Card,
    left: float,
    top: float,
    width: float,
    title: str,
    color: Color,
    rows: Optional[Sequence[RankedRow]],
    largest: float,
) -> None:
    card.text(left + PADDING, top + 14, title, get_font(10, 700), color)
    rows_top = top + VERSUS_COLUMN_HEADER
    if not rows:
        message = "Unavailable" if rows is None else "No ranking returned"
        card.text(left + PADDING, rows_top + 15, message, get_font(9.5, 400), MUTED)
        return
    name_font, value_font, rank_font = get_font(10, 500), get_font(10, 600), get_font(9, 600)
    inner_right = left + width - PADDING
    for index, row in enumerate(list(rows)[:MAX_VERSUS_ROWS]):
        row_top = rows_top + index * VERSUS_ROW_HEIGHT
        value = row.shown_value
        value_width = value_font.getlength(value) / px(1)
        card.text(left + PADDING, row_top + 13, str(row.rank if row.rank is not None else index + 1), rank_font, MUTED)
        name_left = left + PADDING + 14
        _name(card, name_left, row_top + 13, row, name_font, inner_right - value_width - 8 - name_left, anchor="ls")
        card.text(inner_right, row_top + 13, value, value_font, TEXT, anchor="rs")
        bar_width = inner_right - (left + PADDING)
        card.bar(left + PADDING, row_top + 18, bar_width, 3, TRACK)
        if row.value:
            card.bar(left + PADDING, row_top + 18, bar_width * min(1.0, row.value / largest), 3, color)


def render_versus_card(
    name_a: str,
    name_b: str,
    sections: Sequence[VersusSection],
) -> bytes:
    """Rankings of two kingdoms in two columns, one block per section."""
    with RENDER_LOCK:
        card = Card(sum(section.height for section in sections))
        top = 0.0
        half = CARD_WIDTH / 2
        for index, section in enumerate(sections):
            if index:
                card.line(0, top, CARD_WIDTH, top)
            card.header(top, section.title, MUTED, note=section.note)
            body_top = top + HEADER_HEIGHT
            # One scale for both kingdoms: the bars compare across the columns.
            largest = _largest([*(section.rows_a or ()), *(section.rows_b or ())])
            _versus_column(card, 0, body_top, half, name_a, SIDE_A, section.rows_a, largest)
            _versus_column(card, half, body_top, half, name_b, SIDE_B, section.rows_b, largest)
            card.line(half, body_top, half, top + section.height)
            top += section.height
        return card.png()
