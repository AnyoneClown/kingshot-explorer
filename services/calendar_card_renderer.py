"""Renders the event calendar as PNG cards for Discord embeds.

Two kinds of cards, styled after the calendar on kingshot.net (dark panel, event
artwork, a progress bar in the category color):

* **Today** — ``render_today_cards``: "Running now" as a two-column grid, and a second
  card with "Up next" and "Packs".
* **Week** — ``render_week_cards``: a timeline (one row per event, one column per game
  day) for events, and a second one for packs.

Each card is its own image because Discord shows an embed image in a small box
(roughly 400 x 300 points): one tall picture with everything on it would be scaled
down until the text is unreadable. Cards are drawn at ``SCALE`` times their layout
size, so they stay sharp on high-density screens and when opened full size.

Everything here is CPU-bound and synchronous: callers on the event loop must go
through ``asyncio.to_thread``. A lock serializes rendering, because FreeType font
objects are shared between calls and are not safe to use from several threads at once.

Event artwork is read from ``images/events/<event key>.png`` (the key is the API's
``event`` id, which is also the file name on kingshot.net). Artwork for events that
are not bundled can be passed as raw PNG bytes in ``extra_icons``; an event without
any artwork gets a lettered tile in its category color.
"""

from __future__ import annotations

import functools
import io
import logging
from datetime import datetime, time, timedelta
from typing import Iterable, Mapping, Optional, Sequence

from PIL import Image, ImageDraw, ImageFont

from services.card_drawing import (
    AMBER,
    BACKGROUND,
    BLUE,
    CARD_WIDTH,
    FONT_PATH,
    GOOD,
    HEADER_HEIGHT,
    MUTED,
    PADDING,
    RED,
    RENDER_LOCK as _RENDER_LOCK,
    ROOT_DIR,
    SCALE,
    TEXT,
    TRACK,
    Card as _Card,
    Color,
    ellipsize as _ellipsize,
    get_font as _font,
    format_duration,
    mix as _mix,
    px as _px,
    text_width as _width,
)
from services.kingshot_calendar_service import (
    CATEGORY_ALLIANCE,
    CATEGORY_DEALS,
    CATEGORY_GROWTH,
    CATEGORY_PACK,
    CATEGORY_SPECIAL,
    CATEGORY_WAR,
    CalendarDigest,
    CalendarEvent,
    CalendarWeek,
    start_of_day,
)

logger = logging.getLogger(__name__)

EVENT_ICONS_DIR = ROOT_DIR / "images" / "events"

COLUMNS = 2
CELL_HEIGHT = 52
COMPACT_CELL_HEIGHT = 38
# Longer lists would make a card too tall to read inside an embed.
MAX_RUNNING = 14
MAX_UPCOMING = 6
MAX_PACKS = 6
MAX_TIMELINE_ROWS = 20

TIMELINE_LABEL_WIDTH = 164
TIMELINE_DAYS_HEIGHT = 22
TIMELINE_ROW_HEIGHT = 20

TODAY_TINT: Color = (42, 39, 37)

CATEGORY_COLORS: dict[str, Color] = {
    CATEGORY_WAR: RED,
    CATEGORY_ALLIANCE: BLUE,
    CATEGORY_GROWTH: GOOD,
    CATEGORY_DEALS: MUTED,
    CATEGORY_SPECIAL: (74, 168, 255),
    CATEGORY_PACK: AMBER,
}

_MIDNIGHT = time(0, 0)


def category_color(event: CalendarEvent) -> Color:
    return CATEGORY_COLORS.get(event.category, CATEGORY_COLORS[CATEGORY_SPECIAL])


# ----------------------------------------------------------------------- text


def _moment_label(moment: datetime, now: datetime) -> str:
    """A date and time in game time, as short as it can be without being ambiguous."""
    days_away = (moment.date() - now.date()).days
    if days_away == 0:
        day = "today"
    elif 0 < days_away < 7:
        day = f"{moment:%a} {moment.day}"
    else:
        day = f"{moment:%b} {moment.day}"
    return f"{day}, {moment:%H:%M}"


def _day_label(day: datetime, now: datetime) -> str:
    days_away = (day.date() - now.date()).days
    if days_away == 0:
        return "Today"
    if days_away == 1:
        return "Tomorrow"
    return f"{day:%a}, {day:%b} {day.day}"


# ---------------------------------------------------------------------- icons


@functools.lru_cache(maxsize=512)
def _bundled_icon(key: str, size: int) -> Optional[Image.Image]:
    # Keys come from a third-party API: never let one walk out of the artwork folder.
    if not key or not all(char.isalnum() or char in "-_" for char in key):
        return None
    path = EVENT_ICONS_DIR / f"{key}.png"
    try:
        with Image.open(path) as source:
            return _fit(source.convert("RGBA"), size)
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("Could not read event artwork %s", path, exc_info=True)
        return None


def has_bundled_icon(key: str) -> bool:
    return _bundled_icon(key, _px(32)) is not None


def _fit(image: Image.Image, size: int) -> Image.Image:
    image.thumbnail((size, size), Image.LANCZOS)
    return image


def _icon(event: CalendarEvent, size: int, extra_icons: Optional[Mapping[str, bytes]]) -> Image.Image:
    icon = _bundled_icon(event.key, size)
    if icon is None and extra_icons and extra_icons.get(event.key):
        try:
            with Image.open(io.BytesIO(extra_icons[event.key])) as source:
                icon = _fit(source.convert("RGBA"), size)
        except Exception:
            logger.debug("Could not decode downloaded artwork for %s", event.key, exc_info=True)
    return icon if icon is not None else _placeholder_icon(event, size)


def _placeholder_icon(event: CalendarEvent, size: int) -> Image.Image:
    """A lettered tile in the category color for events without artwork."""
    color = category_color(event)
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(tile)
    draw.rounded_rectangle((0, 0, size - 1, size - 1), radius=size // 4, fill=_mix(color, BACKGROUND, 0.22))
    letter = next((char for char in event.title if char.isalnum()), "?").upper()
    font = ImageFont.truetype(str(FONT_PATH), max(8, int(size * 0.56)))
    draw.text((size / 2, size / 2), letter, font=font, fill=color, anchor="mm")
    return tile


# ----------------------------------------------------------------------- grid


def _rows(count: int) -> int:
    return (count + COLUMNS - 1) // COLUMNS


def _cell_origin(index: int, top: float, cell_height: float) -> tuple[float, float, float]:
    """Left, top and width of grid cell ``index``."""
    cell_width = CARD_WIDTH / COLUMNS
    return (index % COLUMNS) * cell_width, top + (index // COLUMNS) * cell_height, cell_width


def _grid_lines(card: _Card, top: float, count: int, cell_height: float, *, last: bool) -> None:
    rows = _rows(count)
    for row in range(1, rows + (0 if last else 1)):
        card.line(0, top + row * cell_height, CARD_WIDTH, top + row * cell_height)
    card.line(CARD_WIDTH / COLUMNS, top, CARD_WIDTH / COLUMNS, top + rows * cell_height)


def _event_badges(event: CalendarEvent, now: datetime, *, with_timing: bool = True) -> list[tuple[str, Color]]:
    badges: list[tuple[str, Color]] = []
    if with_timing and event.is_active(now):
        day_start = start_of_day(now)
        if event.start >= day_start:
            badges.append(("NEW", GOOD))
        elif event.end <= day_start + timedelta(days=1):
            badges.append(("LAST DAY", AMBER))
    if event.estimated:
        badges.append(("EST.", MUTED))
    return badges


def _title_with_badges(
    card: _Card, x: float, baseline: float, max_width: float, event: CalendarEvent, badges, font
) -> None:
    badge_font = _font(7.5, 600)
    badges_width = sum(_width(text, badge_font) / SCALE + 8 + 5 for text, _ in badges)
    title = _ellipsize(event.display_title, font, _px(max(40.0, max_width - badges_width)))
    cursor = x + card.text(x, baseline, title, font, TEXT) + 6
    for text, color in badges:
        cursor += card.badge(cursor, baseline, text, color) + 5


# ---------------------------------------------------------------- today cards


def _running_cell(card: _Card, event: CalendarEvent, index: int, top: float, now: datetime, extra_icons) -> None:
    left, cell_top, width = _cell_origin(index, top, CELL_HEIGHT)
    text_x = left + PADDING + 38
    text_width = width - (text_x - left) - 8
    card.paste(_icon(event, _px(30), extra_icons), left + PADDING, cell_top + 7, 30)
    _title_with_badges(card, text_x, cell_top + 18, text_width, event, _event_badges(event, now), _font(11.5, 600))

    left_font = _font(12.5, 700)
    cursor = text_x + card.text(text_x, cell_top + 34, format_duration(event.end - now), left_font, TEXT)
    detail_font = _font(9.5, 400)
    for detail in (f"left · ends {_moment_label(event.end, now)}", "left"):
        if _width(detail, detail_font) / SCALE <= text_x + text_width - cursor - 5:
            card.text(cursor + 5, cell_top + 34, detail, detail_font, MUTED)
            break

    bar_width = width - PADDING * 2
    progress = (now - event.start) / (event.end - event.start)
    card.bar(left + PADDING, cell_top + 42, bar_width, 3, TRACK)
    card.bar(left + PADDING, cell_top + 42, bar_width * min(1.0, max(0.02, progress)), 3, category_color(event))


def _compact_cell(
    card: _Card, event: CalendarEvent, index: int, top: float, detail: str, badges, extra_icons
) -> None:
    left, cell_top, width = _cell_origin(index, top, COMPACT_CELL_HEIGHT)
    text_x = left + PADDING + 32
    text_width = width - (text_x - left) - 8
    card.paste(_icon(event, _px(24), extra_icons), left + PADDING, cell_top + 7, 24)
    _title_with_badges(card, text_x, cell_top + 16.5, text_width, event, badges, _font(11, 600))
    detail_font = _font(9.5, 400)
    card.text(text_x, cell_top + 29.5, _ellipsize(detail, detail_font, _px(text_width)), detail_font, MUTED)


def _more_note(card: _Card, top: float, hidden: int) -> None:
    card.text(CARD_WIDTH / 2, top + 9, f"and {hidden} more", _font(9.5, 400), MUTED, anchor="mm")


def _running_card(events: Sequence[CalendarEvent], now: datetime, extra_icons) -> bytes:
    shown = list(events[:MAX_RUNNING])
    hidden = len(events) - len(shown)
    grid_height = _rows(len(shown)) * CELL_HEIGHT
    card = _Card(HEADER_HEIGHT + grid_height + (18 if hidden else 0))
    card.header(0, "Running now", GOOD, count=len(events), note=f"{now:%a}, {now:%b} {now.day} · {now:%H:%M} UTC")
    for index, event in enumerate(shown):
        _running_cell(card, event, index, HEADER_HEIGHT, now, extra_icons)
    _grid_lines(card, HEADER_HEIGHT, len(shown), CELL_HEIGHT, last=not hidden)
    if hidden:
        _more_note(card, HEADER_HEIGHT + grid_height, hidden)
    return card.png()


def _upcoming_detail(event: CalendarEvent, now: datetime) -> str:
    if event.day_precision:
        return f"{_day_label(event.start, now)} · time not announced"
    when = _day_label(event.start, now)
    if event.start.time() != _MIDNIGHT or event.start.date() == now.date():
        when = f"{when} {event.start:%H:%M}"
    return f"{when} · in {format_duration(event.start - now)}"


def _pack_detail(event: CalendarEvent, now: datetime) -> str:
    if event.start > now:
        return f"starts {_moment_label(event.start, now)}"
    return f"{format_duration(event.end - now)} left · ends {_moment_label(event.end, now)}"


def _more_card(upcoming: Sequence[CalendarEvent], packs: Sequence[CalendarEvent], now: datetime, extra_icons) -> bytes:
    sections = [
        (title, color, list(events[:limit]), len(events), detail, timing)
        for title, color, events, limit, detail, timing in (
            ("Up next", CATEGORY_COLORS[CATEGORY_SPECIAL], upcoming, MAX_UPCOMING, _upcoming_detail, False),
            ("Packs", AMBER, packs, MAX_PACKS, _pack_detail, True),
        )
        if events
    ]
    height = sum(
        HEADER_HEIGHT + _rows(len(shown)) * COMPACT_CELL_HEIGHT + (18 if total > len(shown) else 0)
        for _, _, shown, total, _, _ in sections
    )
    card = _Card(height)
    top = 0.0
    for position, (title, color, shown, total, detail, timing) in enumerate(sections):
        if position:
            card.line(0, top, CARD_WIDTH, top)
        card.header(top, title, color, count=total)
        top += HEADER_HEIGHT
        for index, event in enumerate(shown):
            badges = _event_badges(event, now, with_timing=timing)
            _compact_cell(card, event, index, top, detail(event, now), badges, extra_icons)
        hidden = total - len(shown)
        _grid_lines(card, top, len(shown), COMPACT_CELL_HEIGHT, last=not hidden)
        top += _rows(len(shown)) * COMPACT_CELL_HEIGHT
        if hidden:
            _more_note(card, top, hidden)
            top += 18
    return card.png()


def render_today_cards(
    digest: CalendarDigest,
    now: datetime,
    extra_icons: Optional[Mapping[str, bytes]] = None,
) -> list[bytes]:
    """PNG cards for one game day: "Running now", then "Up next" and "Packs".

    A card is left out when it would be empty, so the result has zero to two images.
    """
    running = sorted(
        (
            event
            for event in (*digest.starting_today, *digest.ending_today, *digest.running)
            if event.is_active(now)
        ),
        key=lambda event: (event.end, event.title),
    )
    # Events that start later today belong with what is coming.
    upcoming = [event for event in digest.starting_today if event.start > now]
    upcoming.extend(event for _, day_events in digest.upcoming for event in day_events)
    packs = [
        *(event for event in digest.packs_starting),
        *digest.packs_ending,
        *digest.packs_running,
    ]
    packs.sort(key=lambda event: (not event.is_active(now), event.end, event.title))

    with _RENDER_LOCK:
        cards: list[bytes] = []
        if running:
            cards.append(_running_card(running, now, extra_icons))
        if upcoming or packs:
            cards.append(_more_card(upcoming, packs, now, extra_icons))
        return cards


# ----------------------------------------------------------------- week cards


def _span_note(event: CalendarEvent) -> str:
    """Hours worth showing next to a bar: only when the event does not run reset to reset."""
    if event.day_precision:
        return ""
    starts_at_reset = event.start.time() == _MIDNIGHT
    ends_at_reset = event.end.time() == _MIDNIGHT
    if starts_at_reset and ends_at_reset:
        return ""
    if not starts_at_reset and not ends_at_reset:
        return f"{event.start:%H:%M}–{event.end:%H:%M}"
    return f"from {event.start:%H:%M}" if not starts_at_reset else f"until {event.end:%H:%M}"


def _timeline_rows(events: Iterable[CalendarEvent], week_start: datetime) -> list[list[CalendarEvent]]:
    """One row per event: an event that runs twice in a week gets two bars on its row."""
    rows: dict[tuple[str, str], list[CalendarEvent]] = {}
    for event in sorted(events, key=lambda event: (max(event.start, week_start), event.end, event.title)):
        rows.setdefault((event.key, event.phase), []).append(event)
    return list(rows.values())


def _timeline_card(
    title: str,
    color: Color,
    rows: Sequence[Sequence[CalendarEvent]],
    week_start: datetime,
    now: datetime,
    extra_icons,
) -> bytes:
    shown = list(rows[:MAX_TIMELINE_ROWS])
    hidden = len(rows) - len(shown)
    week_end = week_start + timedelta(days=7)
    rows_top = HEADER_HEIGHT + TIMELINE_DAYS_HEIGHT
    rows_height = len(shown) * TIMELINE_ROW_HEIGHT
    has_estimates = False
    will_explain_estimates = any(event.estimated and not event.has_ended(now) for row in shown for event in row)
    footer_height = 18 if hidden or will_explain_estimates else 6
    card = _Card(rows_top + rows_height + footer_height)
    last_day = week_end - timedelta(days=1)
    card.header(
        0,
        title,
        color,
        count=len(rows),
        note=f"{week_start:%b} {week_start.day} – {last_day:%b} {last_day.day} · game time (UTC)",
    )

    chart_left = TIMELINE_LABEL_WIDTH
    chart_width = CARD_WIDTH - PADDING - chart_left
    day_width = chart_width / 7
    today = start_of_day(now)
    bottom = rows_top + rows_height

    def x_at(moment: datetime) -> float:
        return chart_left + chart_width * ((moment - week_start) / (week_end - week_start))

    for offset in range(7):
        day = week_start + timedelta(days=offset)
        left = chart_left + offset * day_width
        is_today = day == today
        if is_today:
            card.draw.rectangle((_px(left), _px(HEADER_HEIGHT) + 1, _px(left + day_width), _px(bottom + 3)), fill=TODAY_TINT)
        card.text(
            left + day_width / 2,
            HEADER_HEIGHT + TIMELINE_DAYS_HEIGHT / 2,
            f"{day:%a} {day.day}",
            _font(8.5, 600 if is_today else 400),
            TEXT if is_today else MUTED,
            anchor="mm",
        )
    for offset in range(8):
        x = chart_left + offset * day_width
        card.line(x, HEADER_HEIGHT + TIMELINE_DAYS_HEIGHT - 4, x, bottom + 3)

    label_font = _font(9.5, 500)
    note_font = _font(7.5, 500)
    for index, row in enumerate(shown):
        top = rows_top + index * TIMELINE_ROW_HEIGHT
        middle = top + TIMELINE_ROW_HEIGHT / 2
        first = row[0]
        card.paste(_icon(first, _px(15), extra_icons), PADDING - 2, top + 2.5, 15)
        label = _ellipsize(first.display_title, label_font, _px(chart_left - PADDING - 22))
        all_ended = all(event.has_ended(now) for event in row)
        card.text(PADDING + 18, middle, label, label_font, MUTED if all_ended else TEXT, anchor="lm")
        for event in row:
            has_estimates |= _timeline_bar(card, event, middle, x_at, week_start, week_end, now, chart_left, note_font)

    if week_start <= now < week_end:
        x = x_at(now)
        card.draw.line((_px(x), _px(rows_top - 3), _px(x), _px(bottom + 3)), fill=TEXT, width=max(1, _px(0.8)))
        card.draw.ellipse((_px(x - 2), _px(rows_top - 5), _px(x + 2), _px(rows_top - 1)), fill=TEXT)
    footer_font = _font(8.5, 400)
    if has_estimates:
        card.striped_bar(PADDING, bottom + 7.5, 16, 7, MUTED, radius=2)
        card.text(PADDING + 21, bottom + 11, "estimated date, not confirmed yet", footer_font, MUTED, anchor="lm")
    if hidden:
        card.text(CARD_WIDTH - PADDING, bottom + 11, f"and {hidden} more", footer_font, MUTED, anchor="rm")
    return card.png()


def _timeline_bar(
    card: _Card,
    event: CalendarEvent,
    middle: float,
    x_at,
    week_start: datetime,
    week_end: datetime,
    now: datetime,
    chart_left: float,
    note_font,
) -> bool:
    """Draw one event's bar on its row. Returns whether it was drawn as an estimate."""
    ended = event.has_ended(now)
    base = category_color(event)
    bar_left = x_at(max(event.start, week_start)) + 0.5
    bar_width = max(3.0, x_at(min(event.end, week_end)) - bar_left - 0.5)
    top = middle - 4.5
    striped = event.estimated and not ended
    if striped:
        card.striped_bar(bar_left, top, bar_width, 9, base, radius=2.5)
    else:
        fill = _mix(base, BACKGROUND, 0.3 if ended else 1.0)
        card.bar(bar_left, top, bar_width, 9, fill, radius=2.5)
        # Square off the side that continues outside this week.
        edge = min(4.0, bar_width)
        if event.start < week_start:
            card.bar(bar_left, top, edge, 9, fill, radius=0)
        if event.end > week_end:
            card.bar(bar_left + bar_width - edge, top, edge, 9, fill, radius=0)

    note = _span_note(event)
    if note:
        note_width = _width(note, note_font) / SCALE
        after = bar_left + bar_width + 4
        if after + note_width <= CARD_WIDTH - PADDING:
            card.text(after, middle, note, note_font, MUTED, anchor="lm")
        elif bar_width >= note_width + 8 and not striped:
            # No room beside the bar: write the hours on it.
            card.text(bar_left + bar_width - 4, middle, note, note_font, BACKGROUND, anchor="rm")
        elif bar_left - 4 - note_width >= chart_left:
            card.text(bar_left - 4, middle, note, note_font, MUTED, anchor="rm")
    return striped


def render_week_cards(
    week: CalendarWeek,
    now: datetime,
    extra_icons: Optional[Mapping[str, bytes]] = None,
    *,
    title: str = "This week",
) -> list[bytes]:
    """PNG timelines for one game week: events, then packs. Empty ones are left out."""
    events: list[CalendarEvent] = [event for event in week.carried_over if not event.has_ended(now)]
    for _, day_events in week.days:
        events.extend(day_events)

    regular = _timeline_rows((event for event in events if not event.is_pack), week.start)
    packs = _timeline_rows((event for event in events if event.is_pack), week.start)
    with _RENDER_LOCK:
        cards: list[bytes] = []
        if regular:
            cards.append(_timeline_card(title, GOOD, regular, week.start, now, extra_icons))
        if packs:
            cards.append(_timeline_card("Packs", AMBER, packs, week.start, now, extra_icons))
        return cards


def missing_icon_keys(events: Iterable[CalendarEvent]) -> list[str]:
    """Event keys without bundled artwork, in first-seen order."""
    return list(dict.fromkeys(event.key for event in events if not has_bundled_icon(event.key)))
