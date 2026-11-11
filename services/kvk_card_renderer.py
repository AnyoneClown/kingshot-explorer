"""PNG cards for the KvK commands: a kingdom's history and a two-kingdom comparison.

Both read the summaries built by ``KVKService`` (wins, losses, win rates, streak and
the normalized match history). Missing values are drawn as "–", never as zero.

CPU-bound and synchronous: callers on the event loop go through ``asyncio.to_thread``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence

from services.card_drawing import (
    AMBER,
    BACKGROUND,
    BLUE,
    CARD_WIDTH,
    GOOD,
    HEADER_HEIGHT,
    MUTED,
    PADDING,
    RED,
    RENDER_LOCK,
    TEXT,
    TRACK,
    Card,
    Color,
    as_number,
    get_font,
    mix,
)

HISTORY_FILENAME = "kvk-history.png"
COMPARE_FILENAME = "kvk-compare.png"

TILES_HEIGHT = 58
SEASONS_HEIGHT = 80
TOTALS_HEIGHT = 30
MAX_SEASONS = 10

VERSUS_HEIGHT = 54
METRIC_ROW_HEIGHT = 22

RESULT_COLORS: dict[str, Color] = {"win": GOOD, "loss": RED}
# The two sides of a comparison.
SIDE_A: Color = BLUE
SIDE_B: Color = AMBER


def _count(value: Any) -> str:
    number = as_number(value)
    return "–" if number is None else f"{int(number)}"


def _record(stats: Mapping, wins: str, losses: str) -> str:
    return f"{_count(stats.get(wins))}–{_count(stats.get(losses))}"


def _rate(stats: Mapping, wins: str, losses: str, rate: str) -> str:
    won, lost = as_number(stats.get(wins)), as_number(stats.get(losses))
    value = as_number(stats.get(rate))
    if won is None or lost is None or value is None:
        return "win rate unavailable"
    if won + lost == 0:
        return "no decided matches"
    return f"{value:.0f}% win rate"


def _tile(card: Card, left: float, top: float, label: str, value: str, detail: str, color: Color = TEXT) -> None:
    card.text(left, top + 15, label.upper(), get_font(8, 600), MUTED)
    card.text(left, top + 37, value, get_font(20, 700), color)
    card.text(left, top + 50, detail, get_font(9.5, 400), MUTED)


def _streak(stats: Mapping) -> tuple[str, str, Color]:
    streak = str(stats.get("currentStreak") or "")
    if streak[:1] in {"W", "L"} and streak[1:].isdigit():
        won = streak[0] == "W"
        count = int(streak[1:])
        detail = f"{'win' if won else 'loss'}{'' if count == 1 else ('s' if won else 'es')} in a row"
        return streak, detail, GOOD if won else RED
    return "–", "no current streak", MUTED


def _seasons(card: Card, top: float, history: Sequence[Mapping]) -> None:
    """The latest seasons, oldest on the left: prep and castle results as colored squares."""
    shown = list(reversed(list(history[:MAX_SEASONS])))
    label_font = get_font(9, 500)
    card.text(PADDING, top + 31, "Prep", label_font, MUTED, anchor="lm")
    card.text(PADDING, top + 51, "Castle", label_font, MUTED, anchor="lm")
    left = PADDING + 44
    cell = (CARD_WIDTH - PADDING - left) / MAX_SEASONS
    small = get_font(8, 500)
    for index, match in enumerate(shown):
        center = left + (index + 0.5) * cell
        season = match.get("season_id")
        card.text(center, top + 13, f"#{season}" if season is not None else "–", small, MUTED, anchor="mm")
        for offset, key in ((23, "prepResult"), (43, "castleResult")):
            color = RESULT_COLORS.get(str(match.get(key)), TRACK)
            card.bar(center - 8, top + offset, 16, 16, color, radius=4)
        opponent = match.get("opponent")
        card.text(center, top + 69, f"K{opponent}" if opponent is not None else "–", small, MUTED, anchor="mm")
    if not shown:
        card.text(CARD_WIDTH / 2, top + SEASONS_HEIGHT / 2, "No matches recorded", get_font(10, 400), MUTED, anchor="mm")


def _totals(card: Card, top: float, stats: Mapping) -> None:
    items = (
        ("attacks", "attacks"),
        ("defenses", "defenses"),
        ("castleCaptures", "castles captured"),
        ("defensesHeld", "defenses held"),
    )
    width = (CARD_WIDTH - PADDING * 2) / len(items)
    for index, (key, label) in enumerate(items):
        left = PADDING + index * width
        taken = card.text(left, top + 19, _count(stats.get(key)), get_font(11, 700), TEXT)
        card.text(left + taken + 4, top + 19, label, get_font(9.5, 400), MUTED)


def render_kvk_history_card(kingdom: int, stats: Mapping) -> bytes:
    """A kingdom's KvK record: castle and prep records, the streak and the latest seasons."""
    history = stats.get("history") or []
    with RENDER_LOCK:
        card = Card(HEADER_HEIGHT + TILES_HEIGHT + SEASONS_HEIGHT + TOTALS_HEIGHT)
        matches = len(history)
        card.header(0, f"Kingdom {kingdom} · KvK history", RED, count=f"{matches} match{'' if matches == 1 else 'es'}")
        top = HEADER_HEIGHT
        column = (CARD_WIDTH - PADDING * 2) / 3
        _tile(card, PADDING, top, "Castle", _record(stats, "wins", "losses"), _rate(stats, "wins", "losses", "winRate"))
        _tile(
            card,
            PADDING + column,
            top,
            "Preparation",
            _record(stats, "prepWins", "prepLosses"),
            _rate(stats, "prepWins", "prepLosses", "prepWinRate"),
        )
        streak, detail, color = _streak(stats)
        _tile(card, PADDING + column * 2, top, "Castle streak", streak, detail, color)
        top += TILES_HEIGHT
        card.line(0, top, CARD_WIDTH, top)
        _seasons(card, top, history)
        top += SEASONS_HEIGHT
        card.line(0, top, CARD_WIDTH, top)
        _totals(card, top, stats)
        return card.png()


# --------------------------------------------------------------- comparison


def comparison_metrics(
    stats_a: Mapping, stats_b: Mapping, head_to_head: Optional[Mapping] = None
) -> list[tuple[str, Optional[float], Optional[float], bool, bool]]:
    """Rows of the comparison: label, value A, value B, whether lower is better, whether it is a percentage."""
    def decided(stats: Mapping, wins: str, losses: str) -> bool:
        return bool((as_number(stats.get(wins)) or 0) + (as_number(stats.get(losses)) or 0))

    def rate(stats: Mapping, wins: str, losses: str, key: str) -> Optional[float]:
        return as_number(stats.get(key)) if decided(stats, wins, losses) else None

    rows = [
        ("Castle win rate", rate(stats_a, "wins", "losses", "winRate"), rate(stats_b, "wins", "losses", "winRate"), False, True),
        ("Castle wins", as_number(stats_a.get("wins")), as_number(stats_b.get("wins")), False, False),
        ("Castle losses", as_number(stats_a.get("losses")), as_number(stats_b.get("losses")), True, False),
        (
            "Prep win rate",
            rate(stats_a, "prepWins", "prepLosses", "prepWinRate"),
            rate(stats_b, "prepWins", "prepLosses", "prepWinRate"),
            False,
            True,
        ),
        ("Prep wins", as_number(stats_a.get("prepWins")), as_number(stats_b.get("prepWins")), False, False),
        ("Castles captured", as_number(stats_a.get("castleCaptures")), as_number(stats_b.get("castleCaptures")), False, False),
        ("Defenses held", as_number(stats_a.get("defensesHeld")), as_number(stats_b.get("defensesHeld")), False, False),
    ]
    if head_to_head and head_to_head.get("available"):
        direct_a, direct_b = head_to_head.get("kingdom_a") or {}, head_to_head.get("kingdom_b") or {}
        rows.append(("Head-to-head wins", as_number(direct_a.get("wins")), as_number(direct_b.get("wins")), False, False))
    return rows


def _leader(value_a: Optional[float], value_b: Optional[float], lower_is_better: bool) -> Optional[str]:
    if value_a is None or value_b is None or value_a == value_b:
        return None
    return "a" if (value_a < value_b) == lower_is_better else "b"


def _metric_row(card: Card, top: float, row) -> None:
    label, value_a, value_b, lower_is_better, percent = row
    middle = top + METRIC_ROW_HEIGHT / 2
    leader = _leader(value_a, value_b, lower_is_better)
    card.text(CARD_WIDTH / 2, middle, label, get_font(9.5, 400), MUTED, anchor="mm")
    scale = 100.0 if percent else max(value_a or 0, value_b or 0, 1)
    bar_width = 106.0

    def text(value: Optional[float]) -> str:
        if value is None:
            return "–"
        return f"{value:.0f}%" if percent else f"{int(value)}"

    for side, value, color in (("a", value_a, SIDE_A), ("b", value_b, SIDE_B)):
        ahead = leader == side
        fill = color if ahead or leader is None else mix(color, BACKGROUND, 0.4)
        length = bar_width * min(1.0, (value or 0) / scale) if value else 0.0
        font = get_font(10.5, 700 if ahead else 500)
        if side == "a":
            edge = PADDING + bar_width
            card.bar(PADDING, middle - 3, bar_width, 6, TRACK)
            card.bar(edge - length, middle - 3, length, 6, fill)
            card.text(edge + 8, middle, text(value), font, TEXT if ahead or leader is None else MUTED, anchor="lm")
        else:
            edge = CARD_WIDTH - PADDING - bar_width
            card.bar(edge, middle - 3, bar_width, 6, TRACK)
            card.bar(edge, middle - 3, length, 6, fill)
            card.text(edge - 8, middle, text(value), font, TEXT if ahead or leader is None else MUTED, anchor="rm")


def render_kvk_compare_card(
    kingdom_a: int,
    kingdom_b: int,
    stats_a: Mapping,
    stats_b: Mapping,
    head_to_head: Optional[Mapping] = None,
) -> bytes:
    """Two kingdoms side by side: one row per metric, the better side highlighted."""
    rows = comparison_metrics(stats_a, stats_b, head_to_head)
    ahead_a = sum(1 for row in rows if _leader(row[1], row[2], row[3]) == "a")
    ahead_b = sum(1 for row in rows if _leader(row[1], row[2], row[3]) == "b")
    with RENDER_LOCK:
        card = Card(HEADER_HEIGHT + VERSUS_HEIGHT + len(rows) * METRIC_ROW_HEIGHT + 10)
        card.header(0, "KvK comparison", RED, note="all recorded seasons")
        top = HEADER_HEIGHT
        name_font, detail_font = get_font(19, 700), get_font(9.5, 400)
        card.text(PADDING, top + 28, f"Kingdom {kingdom_a}", name_font, SIDE_A)
        card.text(PADDING, top + 43, f"ahead on {ahead_a} of {len(rows)}", detail_font, MUTED)
        card.text(CARD_WIDTH - PADDING, top + 28, f"Kingdom {kingdom_b}", name_font, SIDE_B, anchor="rs")
        card.text(CARD_WIDTH - PADDING, top + 43, f"ahead on {ahead_b} of {len(rows)}", detail_font, MUTED, anchor="rs")
        card.text(CARD_WIDTH / 2, top + 26, "vs", get_font(11, 500), MUTED, anchor="ms")
        top += VERSUS_HEIGHT
        card.line(0, top, CARD_WIDTH, top)
        for index, row in enumerate(rows):
            _metric_row(card, top + 5 + index * METRIC_ROW_HEIGHT, row)
        return card.png()


__all__ = [
    "COMPARE_FILENAME",
    "HISTORY_FILENAME",
    "comparison_metrics",
    "render_kvk_compare_card",
    "render_kvk_history_card",
]
