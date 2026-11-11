import io
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from services import card_drawing
from services import reminder_card_renderer as renderer
from services.reminder_card_renderer import ReminderRow, countdown, moment_label, render_reminders_card, visible_rows

NOW = datetime(2026, 11, 11, 21, 20, tzinfo=timezone.utc)


def row(index, **kwargs):
    return ReminderRow(index, NOW + timedelta(days=index, hours=3), f"Reminder {index}", **kwargs)


def opened(png):
    image = Image.open(io.BytesIO(png))
    image.load()
    return image


def colors(image):
    return {color for _, color in image.convert("RGB").getcolors(maxcolors=1 << 24)}


def height(points):
    return round(points * card_drawing.SCALE)


def fixed_height():
    return card_drawing.HEADER_HEIGHT + renderer.HERO_HEIGHT + renderer.WEEK_HEIGHT


def test_card_has_a_hero_a_week_strip_and_a_row_per_reminder():
    card = opened(render_reminders_card([row(0), row(1, repeat_every_days=7), row(2, has_problem=True)], NOW))

    assert card.format == "PNG" and card.width == height(card_drawing.CARD_WIDTH)
    assert card.height == height(fixed_height() + 3 * renderer.ROW_HEIGHT + 8)
    assert card.width > card.height
    # Selected mark and countdown, other marks, the problem dot.
    assert {card_drawing.AMBER, card_drawing.BLUE, card_drawing.RED} <= colors(card)


def test_long_lists_show_a_window_around_the_selected_reminder():
    rows = [row(index) for index in range(20)]

    card = opened(render_reminders_card(rows, NOW, selected=15))

    assert card.height == height(fixed_height() + renderer.MAX_ROWS * renderer.ROW_HEIGHT + 22)
    assert list(visible_rows(20, 0)) == list(range(8))
    assert list(visible_rows(20, 15)) == list(range(11, 19))
    assert list(visible_rows(20, 19)) == list(range(12, 20))
    assert list(visible_rows(3, 2)) == [0, 1, 2]
    # A selection outside the list is clamped instead of failing.
    assert opened(render_reminders_card(rows, NOW, selected=99)).height == card.height
    assert opened(render_reminders_card(rows[:1], NOW, selected=-5)).height == height(fixed_height() + renderer.ROW_HEIGHT + 8)


def test_a_card_needs_a_reminder():
    with pytest.raises(ValueError):
        render_reminders_card([], NOW)


def test_countdowns_and_dates_are_short_and_unambiguous():
    assert countdown(NOW + timedelta(hours=14, minutes=40), NOW) == "in 14h 40m"
    assert countdown(NOW + timedelta(days=3, hours=14), NOW) == "in 3d 14h"
    assert countdown(NOW + timedelta(seconds=30), NOW) == "due now"
    assert countdown(NOW - timedelta(hours=1), NOW) == "due now"
    assert moment_label(NOW + timedelta(hours=1), NOW) == "Today, 22:20"
    assert moment_label(NOW + timedelta(days=1), NOW) == "Tomorrow, 21:20"
    assert moment_label(NOW + timedelta(days=3), NOW) == "Sat 14, 21:20"
    assert moment_label(NOW + timedelta(days=20), NOW) == "Dec 1, 21:20"
    # Stored times without a zone are UTC.
    assert countdown(datetime(2026, 11, 12, 21, 20), NOW) == "in 1d 0h"


def test_rows_describe_themselves():
    multi = ReminderRow(1, NOW, "\n  \nBear Trap — bring heroes\nsecond line")

    assert multi.title == "Bear Trap — bring heroes"
    assert ReminderRow(1, NOW, "   ").title == "Reminder"
    assert len(ReminderRow(1, NOW, "x" * 5000).title) == 160
    assert [ReminderRow(1, NOW, "m", days).repeat_label for days in (None, 1, 7)] == ["ONCE", "DAILY", "EVERY 7 DAYS"]
    assert [ReminderRow(1, NOW, "m", days).short_repeat for days in (None, 7)] == ["", "7d"]


def test_a_very_long_message_does_not_slow_the_card_down():
    import time

    rows = [ReminderRow(index, NOW + timedelta(hours=index), "Castle Battle — збір 12:00 " * 200) for index in range(8)]

    started = time.perf_counter()
    card = opened(render_reminders_card(rows, NOW))

    assert card.width and time.perf_counter() - started < 3.0
