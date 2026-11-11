"""Renders the result of a gift-code redemption run as a PNG card for a Discord embed.

The card answers what a server wants to know after a code went out: how many accounts
got the reward, how many already had it, how many failed and why, and how that splits
across kingdoms. It shares its look with the calendar cards (``card_drawing``).

``build_redemption_stats`` is pure and does the counting; ``render_redemption_card``
is CPU-bound and synchronous, so callers on the event loop run it through
``asyncio.to_thread``.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional

from PIL import Image

from services.card_drawing import (
    AMBER,
    BLUE,
    CARD_WIDTH,
    GOOD,
    HEADER_HEIGHT,
    MUTED,
    PADDING,
    RED,
    RENDER_LOCK,
    ROOT_DIR,
    TEXT,
    TRACK,
    Card,
    Color,
    ellipsize,
    get_font,
    px,
)
from services.gift_redemption_service import (
    STATUS_ALREADY_REDEEMED,
    STATUS_API_REJECTED,
    STATUS_INVALID_ID,
    STATUS_ORDER,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
)

logger = logging.getLogger(__name__)

GIFT_ICON_PATH = ROOT_DIR / "images" / "items" / "gift.png"
CARD_FILENAME = "gift-code.png"
# More rows would make the card too tall to read inside an embed.
MAX_KINGDOM_ROWS = 5

HERO_HEIGHT = 58
SUMMARY_HEIGHT = 62
KINGDOM_ROW_HEIGHT = 22

STATUS_STYLE: dict[str, tuple[str, Color]] = {
    STATUS_SUCCESS: ("Received", GOOD),
    STATUS_ALREADY_REDEEMED: ("Already had it", BLUE),
    STATUS_API_REJECTED: ("Rejected", RED),
    STATUS_INVALID_ID: ("Invalid ID", AMBER),
    STATUS_SKIPPED: ("Skipped", MUTED),
}


@dataclass(frozen=True)
class KingdomStat:
    """Results of the accounts of one kingdom. ``kingdom`` is None when it is unknown."""

    kingdom: Optional[str]
    counts: Mapping[str, int]
    # How many kingdoms this row stands for (more than one for the "other" row).
    kingdoms: int = 1

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def rewarded(self) -> int:
        """Accounts that have the reward now: redeemed by this run or claimed earlier."""
        return self.counts.get(STATUS_SUCCESS, 0) + self.counts.get(STATUS_ALREADY_REDEEMED, 0)

    @property
    def label(self) -> str:
        if self.kingdoms > 1:
            return f"{self.kingdoms} more kingdoms"
        if not self.kingdom:
            return "Unknown kingdom"
        return f"Kingdom {self.kingdom}" if self.kingdom.isdigit() else self.kingdom


@dataclass(frozen=True)
class RedemptionStats:
    gift_code: str
    counts: Mapping[str, int] = field(default_factory=dict)
    kingdoms: tuple[KingdomStat, ...] = ()

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def received(self) -> int:
        return self.counts.get(STATUS_SUCCESS, 0)

    @property
    def rewarded(self) -> int:
        """Accounts that have the reward now: redeemed by this run or claimed earlier."""
        return self.received + self.counts.get(STATUS_ALREADY_REDEEMED, 0)

    @property
    def coverage(self) -> int:
        """Share of accounts that have the reward, in whole percent (rounded down)."""
        return self.rewarded * 100 // self.total if self.total else 0


def _empty_counts() -> dict[str, int]:
    return dict.fromkeys(STATUS_ORDER, 0)


def _status(result: Mapping) -> str:
    status = result.get("status_category")
    return status if status in STATUS_STYLE else STATUS_API_REJECTED


def _kingdom(value) -> Optional[str]:
    text = str(value).strip() if value is not None else ""
    return text if text and text.lower() not in {"none", "unknown", "unavailable"} else None


def build_redemption_stats(
    gift_code: str,
    results: Iterable[Mapping],
    kingdoms_by_player: Optional[Mapping[str, object]] = None,
) -> RedemptionStats:
    """Count results by status, overall and per kingdom.

    A result's own ``kingdom`` (resolved while redeeming, so it follows transfers)
    wins over the stored one in ``kingdoms_by_player``.
    """
    kingdoms_by_player = kingdoms_by_player or {}
    counts = _empty_counts()
    by_kingdom: dict[Optional[str], dict[str, int]] = {}
    for result in results:
        status = _status(result)
        counts[status] += 1
        kingdom = _kingdom(result.get("kingdom")) or _kingdom(kingdoms_by_player.get(str(result.get("player_id"))))
        by_kingdom.setdefault(kingdom, _empty_counts())[status] += 1

    def order(item):
        kingdom, kingdom_counts = item
        # Largest first; accounts without a known kingdom always come last.
        return (kingdom is None, -sum(kingdom_counts.values()), kingdom or "")

    return RedemptionStats(
        gift_code=gift_code,
        counts=counts,
        kingdoms=tuple(KingdomStat(kingdom, kingdom_counts) for kingdom, kingdom_counts in sorted(by_kingdom.items(), key=order)),
    )


def kingdom_rows(stats: RedemptionStats, limit: int = MAX_KINGDOM_ROWS) -> list[KingdomStat]:
    """Rows of the kingdom section: the largest kingdoms, the rest folded into one row."""
    kingdoms = list(stats.kingdoms)
    # One row saying "unknown" for everybody tells the reader nothing.
    if len(kingdoms) == 1 and kingdoms[0].kingdom is None:
        return []
    if len(kingdoms) <= limit:
        return kingdoms
    shown, rest = kingdoms[: limit - 1], kingdoms[limit - 1 :]
    merged = _empty_counts()
    for row in rest:
        for status, count in row.counts.items():
            merged[status] = merged.get(status, 0) + count
    # Accounts without a known kingdom ride along in this row but are not a kingdom.
    return [*shown, KingdomStat(None, merged, kingdoms=sum(1 for row in rest if row.kingdom))]


# ------------------------------------------------------------------- drawing


@functools.lru_cache(maxsize=4)
def _gift_icon(size: int) -> Optional[Image.Image]:
    try:
        with Image.open(GIFT_ICON_PATH) as source:
            icon = source.convert("RGBA")
            icon.thumbnail((size, size), Image.LANCZOS)
            return icon
    except Exception:
        logger.warning("Could not read the gift artwork %s", GIFT_ICON_PATH, exc_info=True)
        return None


def _segments(card: Card, x: float, y: float, width: float, height: float, counts: Mapping[str, int]) -> None:
    """One bar split by status, in ``STATUS_ORDER``."""
    total = sum(counts.values())
    card.bar(x, y, width, height, TRACK)
    if not total:
        return
    gap = 1.5
    parts = [(status, counts.get(status, 0)) for status in STATUS_ORDER if counts.get(status, 0)]
    usable = width - gap * (len(parts) - 1)
    cursor = x
    for status, count in parts:
        # Every status that occurred stays visible, however small its share.
        part = max(2.0, usable * count / total)
        part = min(part, x + width - cursor)
        card.bar(cursor, y, part, height, STATUS_STYLE[status][1], radius=height / 2)
        cursor += part + gap


def _hero(card: Card, top: float, stats: RedemptionStats, detail: str) -> None:
    icon = _gift_icon(px(36))
    text_x = PADDING
    if icon is not None:
        card.paste(icon, PADDING, top + 11, 36)
        text_x = PADDING + 46
    number_font = get_font(22, 700)
    number = f"{stats.coverage}%"
    number_width = number_font.getlength(number) / px(1)
    right = CARD_WIDTH - PADDING
    card.text(right, top + 31, number, number_font, GOOD if stats.rewarded else MUTED, anchor="rs")
    card.text(right, top + 45, "have the reward", get_font(9.5, 400), MUTED, anchor="rs")

    code_font = get_font(17, 700)
    room = right - max(number_width, 86) - 14 - text_x
    card.text(text_x, top + 30, ellipsize(stats.gift_code, code_font, px(room)), code_font, TEXT)
    if detail:
        detail_font = get_font(9.5, 400)
        card.text(text_x, top + 45, ellipsize(detail, detail_font, px(room)), detail_font, MUTED)
    card.line(0, top + HERO_HEIGHT, CARD_WIDTH, top + HERO_HEIGHT)


def _summary(card: Card, top: float, stats: RedemptionStats) -> None:
    _segments(card, PADDING, top + 12, CARD_WIDTH - PADDING * 2, 6, stats.counts)
    shown = [status for status in STATUS_ORDER if stats.counts.get(status, 0)]
    for status in (STATUS_ALREADY_REDEEMED, STATUS_SUCCESS):
        if status not in shown:
            shown.insert(0, status)
    shown.sort(key=STATUS_ORDER.index)
    tile_width = (CARD_WIDTH - PADDING * 2) / len(shown)
    for index, status in enumerate(shown):
        label, color = STATUS_STYLE[status]
        left = PADDING + index * tile_width
        card.draw.ellipse((px(left), px(top + 31), px(left + 6), px(top + 37)), fill=color)
        card.text(left + 11, top + 39, str(stats.counts.get(status, 0)), get_font(14, 700), TEXT)
        card.text(left, top + 53, label, get_font(9.5, 400), MUTED)


def _kingdoms(card: Card, top: float, rows: list[KingdomStat], kingdom_count: int) -> None:
    card.line(0, top, CARD_WIDTH, top)
    card.header(top, "By kingdom", BLUE, count=kingdom_count, note="have the reward / accounts")
    label_font = get_font(10.5, 500)
    value_font = get_font(10.5, 600)
    bar_left = PADDING + 118
    bar_right = CARD_WIDTH - PADDING - 70
    for index, row in enumerate(rows):
        middle = top + HEADER_HEIGHT + index * KINGDOM_ROW_HEIGHT + KINGDOM_ROW_HEIGHT / 2 + 2
        label = ellipsize(row.label, label_font, px(bar_left - PADDING - 8))
        card.text(PADDING, middle, label, label_font, TEXT if row.kingdom or row.kingdoms > 1 else MUTED, anchor="lm")
        _segments(card, bar_left, middle - 3, bar_right - bar_left, 6, row.counts)
        card.text(CARD_WIDTH - PADDING, middle, f"{row.rewarded} / {row.total}", value_font, TEXT, anchor="rm")


def render_redemption_card(
    stats: RedemptionStats,
    *,
    title: str = "Gift code redeemed",
    accent: Color = GOOD,
    expires_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> bytes:
    """Draw the result card and return it as PNG bytes."""
    now = now or datetime.now(timezone.utc)
    detail = ""
    if isinstance(expires_at, datetime):
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        expires_at = expires_at.astimezone(timezone.utc)
        if expires_at > now:
            detail = f"expires {expires_at:%b} {expires_at.day}, {expires_at:%H:%M} UTC"
    rows = kingdom_rows(stats)
    kingdoms_height = HEADER_HEIGHT + len(rows) * KINGDOM_ROW_HEIGHT + 8 if rows else 0
    with RENDER_LOCK:
        card = Card(HEADER_HEIGHT + HERO_HEIGHT + SUMMARY_HEIGHT + kingdoms_height)
        accounts = f"{stats.total} account{'' if stats.total == 1 else 's'}"
        card.header(0, title, accent, count=accounts, note=f"{now:%a}, {now:%b} {now.day} · {now:%H:%M} UTC")
        _hero(card, HEADER_HEIGHT, stats, detail)
        _summary(card, HEADER_HEIGHT + HERO_HEIGHT, stats)
        if rows:
            known = sum(1 for kingdom in stats.kingdoms if kingdom.kingdom)
            _kingdoms(card, HEADER_HEIGHT + HERO_HEIGHT + SUMMARY_HEIGHT, rows, known)
        return card.png()
