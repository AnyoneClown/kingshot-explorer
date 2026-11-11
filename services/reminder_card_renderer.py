"""A dashboard card for a channel's reminders.

One picture answers what is coming and when: the selected reminder up top with its
countdown, a strip of the next seven days with a mark for every reminder, and the list
of upcoming reminders. Times are game time (UTC); the embed next to the card keeps the
exact time as a Discord timestamp in the reader's own zone.

CPU-bound and synchronous: callers on the event loop go through ``asyncio.to_thread``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from services.card_drawing import (
    AMBER,
    BACKGROUND,
    BLUE,
    CARD_WIDTH,
    HEADER_HEIGHT,
    MUTED,
    PADDING,
    RED,
    RENDER_LOCK,
    TEXT,
    TRACK,
    Card,
    ellipsize,
    format_duration,
    get_font,
    mix,
    px,
)

CARD_FILENAME = "reminders.png"
HERO_HEIGHT = 60
WEEK_HEIGHT = 46
ROW_HEIGHT = 22
MAX_ROWS = 8
WEEK_DAYS = 7


@dataclass(frozen=True)
class ReminderRow:
    """One reminder as the card shows it. ``when`` is its trigger time."""

    reminder_id: Optional[int]
    when: datetime
    message: str
    repeat_every_days: Optional[int] = None
    targets: str = "@everyone"
    has_problem: bool = False

    @property
    def title(self) -> str:
        """The first line of the message that has any text (the card never needs more than a line)."""
        return next((line.strip()[:160] for line in self.message.splitlines() if line.strip()), "Reminder")

    @property
    def repeat_label(self) -> str:
        if not self.repeat_every_days:
            return "ONCE"
        return "DAILY" if self.repeat_every_days == 1 else f"EVERY {self.repeat_every_days} DAYS"

    @property
    def short_repeat(self) -> str:
        return f"{self.repeat_every_days}d" if self.repeat_every_days else ""


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def countdown(when: datetime, now: datetime) -> str:
    delta = _utc(when) - _utc(now)
    return "due now" if delta.total_seconds() < 60 else f"in {format_duration(delta)}"


def moment_label(when: datetime, now: datetime) -> str:
    """A date and time, as short as it can be without being ambiguous."""
    when, now = _utc(when), _utc(now)
    days_away = (when.date() - now.date()).days
    if days_away == 0:
        day = "Today"
    elif days_away == 1:
        day = "Tomorrow"
    elif 1 < days_away < 7:
        day = f"{when:%a} {when.day}"
    else:
        day = f"{when:%b} {when.day}"
    return f"{day}, {when:%H:%M}"


def visible_rows(count: int, selected: int, limit: int = MAX_ROWS) -> range:
    """Which rows the list shows: a window that always contains the selected one."""
    if count <= limit:
        return range(count)
    start = min(max(0, selected - limit // 2), count - limit)
    return range(start, start + limit)


def _hero(card: Card, top: float, row: ReminderRow, now: datetime) -> None:
    right = CARD_WIDTH - PADDING
    count_font = get_font(20, 700)
    text = countdown(row.when, now)
    count_width = count_font.getlength(text) / px(1)
    card.text(right, top + 29, text, count_font, AMBER, anchor="rs")
    card.text(right, top + 45, f"{moment_label(row.when, now)} UTC", get_font(9.5, 400), MUTED, anchor="rs")

    room = right - max(count_width, 96) - 16 - PADDING
    title_font = get_font(13, 600)
    card.text(PADDING, top + 25, ellipsize(row.title, title_font, px(room)), title_font, TEXT)
    cursor = PADDING
    if row.reminder_id is not None:
        cursor += card.badge(cursor, top + 45, f"#{row.reminder_id}", MUTED) + 5
    cursor += card.badge(cursor, top + 45, row.repeat_label, BLUE if row.repeat_every_days else MUTED) + 5
    if row.has_problem:
        cursor += card.badge(cursor, top + 45, "PROBLEM", RED) + 5
    targets_font = get_font(9.5, 400)
    left_room = PADDING + room - cursor - 2
    if left_room > 30:
        card.text(cursor + 2, top + 45, ellipsize(row.targets, targets_font, px(left_room)), targets_font, MUTED)
    card.line(0, top + HERO_HEIGHT, CARD_WIDTH, top + HERO_HEIGHT)


def _week(card: Card, top: float, rows: Sequence[ReminderRow], selected: int, now: datetime) -> None:
    """The next seven days, with a mark at the time of every reminder."""
    now = _utc(now)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=WEEK_DAYS)
    left, width = PADDING, CARD_WIDTH - PADDING * 2
    day_width = width / WEEK_DAYS
    label_font = get_font(8.5, 400)
    track_y = top + 30
    card.bar(left, track_y - 1.5, width, 3, TRACK)
    for offset in range(WEEK_DAYS):
        day = start + timedelta(days=offset)
        x = left + offset * day_width
        if offset:
            card.line(x, top + 20, x, top + 40)
        card.text(
            x + day_width / 2,
            top + 12,
            "Today" if offset == 0 else f"{day:%a} {day.day}",
            get_font(8.5, 600) if offset == 0 else label_font,
            TEXT if offset == 0 else MUTED,
            anchor="mm",
        )
    # The time already passed today is dimmed, so "now" is visible on the strip.
    passed = width * ((now - start) / (end - start))
    card.bar(left, track_y - 1.5, passed, 3, mix(TEXT, BACKGROUND, 0.35))
    marks = [(index, _utc(row.when)) for index, row in enumerate(rows) if start <= _utc(row.when) < end]
    # The selected mark is drawn last, on top of its neighbours.
    for index, when in sorted(marks, key=lambda mark: mark[0] == selected):
        x = left + width * ((when - start) / (end - start))
        chosen = index == selected
        radius = 5.0 if chosen else 3.5
        card.draw.ellipse(
            (px(x - radius), px(track_y - radius), px(x + radius), px(track_y + radius)),
            fill=AMBER if chosen else BLUE,
            outline=BACKGROUND,
            width=max(1, px(0.8)),
        )
    card.line(0, top + WEEK_HEIGHT, CARD_WIDTH, top + WEEK_HEIGHT)


def _list_row(card: Card, top: float, row: ReminderRow, now: datetime, *, chosen: bool) -> None:
    middle = top + ROW_HEIGHT / 2
    if chosen:
        card.draw.rectangle((0, px(top), px(CARD_WIDTH), px(top + ROW_HEIGHT)), fill=mix(AMBER, BACKGROUND, 0.1))
        card.draw.rectangle((0, px(top), px(2.5), px(top + ROW_HEIGHT)), fill=AMBER)
    small, regular = get_font(9, 500), get_font(10, 500)
    identifier = f"#{row.reminder_id}" if row.reminder_id is not None else "–"
    card.text(PADDING, middle, identifier, small, MUTED, anchor="lm")
    card.text(PADDING + 34, middle, moment_label(row.when, now), regular, TEXT, anchor="lm")
    card.text(PADDING + 128, middle, countdown(row.when, now), small, AMBER if chosen else MUTED, anchor="lm")
    right = CARD_WIDTH - PADDING
    if row.has_problem:
        card.draw.ellipse((px(right - 6), px(middle - 3), px(right), px(middle + 3)), fill=RED)
        right -= 12
    if row.short_repeat:
        taken = card.text(right, middle, row.short_repeat, small, BLUE, anchor="rm")
        right -= taken + 8
    title_left = PADDING + 190
    card.text(title_left, middle, ellipsize(row.title, regular, px(right - title_left)), regular, TEXT, anchor="lm")


def render_reminders_card(rows: Sequence[ReminderRow], now: datetime, *, selected: int = 0) -> bytes:
    """Draw the dashboard for ``rows`` (soonest first), highlighting ``rows[selected]``."""
    if not rows:
        raise ValueError("a reminders card needs at least one reminder")
    selected = min(max(0, selected), len(rows) - 1)
    now = _utc(now)
    shown = visible_rows(len(rows), selected)
    hidden = len(rows) - len(shown)
    with RENDER_LOCK:
        card = Card(HEADER_HEIGHT + HERO_HEIGHT + WEEK_HEIGHT + len(shown) * ROW_HEIGHT + (22 if hidden else 8))
        card.header(
            0,
            "Reminders",
            AMBER,
            count=f"{len(rows)} active",
            note=f"{now:%a}, {now:%b} {now.day} · {now:%H:%M} UTC",
        )
        _hero(card, HEADER_HEIGHT, rows[selected], now)
        _week(card, HEADER_HEIGHT + HERO_HEIGHT, rows, selected, now)
        list_top = HEADER_HEIGHT + HERO_HEIGHT + WEEK_HEIGHT + 4
        for position, index in enumerate(shown):
            _list_row(card, list_top + position * ROW_HEIGHT, rows[index], now, chosen=index == selected)
        if hidden:
            card.text(
                CARD_WIDTH / 2,
                list_top + len(shown) * ROW_HEIGHT + 9,
                f"and {hidden} more",
                get_font(9.5, 400),
                MUTED,
                anchor="mm",
            )
        return card.png()
