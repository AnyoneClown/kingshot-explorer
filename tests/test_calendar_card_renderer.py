import io
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from services import calendar_card_renderer as renderer
from services.kingshot_calendar_service import CalendarEvent, build_digest, build_week


def utc(day, hour=0, minute=0, month=11):
    return datetime(2026, month, day, hour, minute, tzinfo=timezone.utc)


NOW = utc(11, 8)


def event(key, title, category, start, end, **kwargs):
    return CalendarEvent(key=key, title=title, category=category, start=start, end=end, **kwargs)


EVENTS = [
    event("swordland-summit-league", "Swordland Summit League", "WAR", utc(30, month=10), utc(15, month=12), is_main=True),
    event("alliance-brawl", "Alliance Brawl", "ALLIANCE", utc(9), utc(15, 12)),
    event("officer-project", "Officer Project", "GROWTH", utc(10), utc(12)),
    event("champions-way", "Champion's Way", "GROWTH", utc(11), utc(14)),
    event("mystic-divination", "Mystic Divination", "DEALS", utc(13), utc(15), estimated=True),
    event("officer-project", "Officer Project", "GROWTH", utc(15), utc(17)),
    event("custom-selection", "Custom Selection", "PACK", utc(9), utc(12)),
    event("master-collection", "Master's Collection", "PACK", utc(11), utc(13)),
    event("castle-battle", "Castle Battle", "WAR", utc(14, 12), utc(14, 17), is_main=True),
]


def opened(png):
    image = Image.open(io.BytesIO(png))
    image.load()
    return image


def colors(image):
    return {color for _, color in image.convert("RGB").getcolors(maxcolors=1 << 24)}


def test_today_is_two_cards_of_the_same_width():
    cards = renderer.render_today_cards(build_digest(EVENTS, NOW), NOW)

    running, more = (opened(card) for card in cards)
    width = round(renderer.CARD_WIDTH * renderer.SCALE)
    assert running.format == more.format == "PNG"
    assert running.width == more.width == width
    # Four running events: a header and two rows of the grid.
    assert running.height == round((renderer.HEADER_HEIGHT + 2 * renderer.CELL_HEIGHT) * renderer.SCALE)
    # "Up next" (one event) and "Packs" (two): two headers and two compact rows.
    assert more.height == round((2 * renderer.HEADER_HEIGHT + 2 * renderer.COMPACT_CELL_HEIGHT) * renderer.SCALE)
    # Wider than tall, so Discord's embed box is filled by width.
    assert running.width > running.height and more.width > more.height
    # Progress bars use the category colors.
    assert {renderer.CATEGORY_COLORS["WAR"], renderer.CATEGORY_COLORS["ALLIANCE"], renderer.GOOD} <= colors(running)


def test_today_leaves_out_empty_cards():
    only_running = renderer.render_today_cards(build_digest(EVENTS[:2], NOW), NOW)
    only_packs = renderer.render_today_cards(build_digest(EVENTS[6:8], NOW), NOW)

    assert len(only_running) == 1 and len(only_packs) == 1
    assert renderer.render_today_cards(build_digest([], NOW), NOW) == []


def test_an_event_that_starts_later_today_is_listed_as_coming():
    later = [event("castle-battle", "Castle Battle", "WAR", utc(11, 12), utc(11, 17))]

    cards = renderer.render_today_cards(build_digest(later, NOW), NOW)

    # Not running yet: only the "Up next" card, one compact row.
    assert len(cards) == 1
    assert opened(cards[0]).height == round(
        (renderer.HEADER_HEIGHT + renderer.COMPACT_CELL_HEIGHT) * renderer.SCALE
    )


def test_long_lists_are_capped_with_a_note():
    many = [
        event(f"event-{index}", f"Event {index}", "GROWTH", utc(9), utc(20)) for index in range(40)
    ]

    (card,) = renderer.render_today_cards(build_digest(many, NOW), NOW)

    rows = renderer.MAX_RUNNING // renderer.COLUMNS
    assert opened(card).height == round(
        (renderer.HEADER_HEIGHT + rows * renderer.CELL_HEIGHT + 18) * renderer.SCALE
    )


def test_week_is_a_timeline_for_events_and_one_for_packs():
    week = build_week(EVENTS, NOW)

    events_card, packs_card = (opened(card) for card in renderer.render_week_cards(week, NOW))

    # Officer Project runs twice this week but takes one row: six rows for seven events.
    header = renderer.HEADER_HEIGHT + renderer.TIMELINE_DAYS_HEIGHT
    assert events_card.height == round((header + 6 * renderer.TIMELINE_ROW_HEIGHT + 18) * renderer.SCALE)
    assert packs_card.height == round((header + 2 * renderer.TIMELINE_ROW_HEIGHT + 6) * renderer.SCALE)
    assert renderer.AMBER in colors(packs_card)
    assert renderer.AMBER not in colors(events_card)


def test_week_cards_for_an_empty_week():
    assert renderer.render_week_cards(build_week([], NOW), NOW) == []
    future = renderer.render_week_cards(build_week(EVENTS, NOW + timedelta(days=7)), NOW, title="Next week")
    assert len(future) == 1


def test_timeline_rows_group_repeats_of_an_event():
    rows = renderer._timeline_rows(EVENTS, utc(9))

    assert [len(row) for row in rows if row[0].key == "officer-project"] == [2]
    assert [row[0].key for row in rows][:2] == ["custom-selection", "alliance-brawl"]


def test_every_bundled_picture_can_be_read():
    files = sorted(renderer.EVENT_ICONS_DIR.glob("*.png"))

    assert len(files) >= 50
    for path in files:
        icon = renderer._bundled_icon(path.stem, 64)
        assert icon is not None and icon.mode == "RGBA" and max(icon.size) <= 64


def test_unknown_events_get_downloaded_artwork_or_a_lettered_tile():
    unknown = event("brand-new-festival", "Brand New Festival", "SPECIAL", utc(10), utc(14))
    red = Image.new("RGBA", (40, 40), (255, 0, 0, 255))
    buffer = io.BytesIO()
    red.save(buffer, format="PNG")

    assert renderer.missing_icon_keys([unknown, EVENTS[0], unknown]) == ["brand-new-festival"]
    downloaded = renderer._icon(unknown, 30, {"brand-new-festival": buffer.getvalue()})
    assert downloaded.getpixel((5, 5)) == (255, 0, 0, 255)
    tile = renderer._icon(unknown, 30, None)
    assert tile.size == (30, 30)
    # Unreadable downloads fall back to the tile instead of failing the card.
    assert renderer._icon(unknown, 30, {"brand-new-festival": b"not a png"}).size == (30, 30)
    assert renderer.render_today_cards(build_digest([unknown], NOW), NOW)


@pytest.mark.parametrize("key", ["../../main", "..", "", "a/b", "hero rally", "x.png"])
def test_event_keys_cannot_leave_the_artwork_folder(key):
    assert renderer._bundled_icon(key, 32) is None


@pytest.mark.parametrize(
    ("delta", "text"),
    [
        (timedelta(days=33, hours=4, minutes=59), "33d 4h"),
        (timedelta(days=1), "1d 0h"),
        (timedelta(hours=5, minutes=2, seconds=59), "5h 2m"),
        (timedelta(minutes=42), "42m"),
        (timedelta(seconds=-5), "0m"),
    ],
)
def test_durations_read_like_the_site(delta, text):
    assert renderer.format_duration(delta) == text


def test_moments_are_as_short_as_they_can_be():
    assert renderer._moment_label(utc(11, 12), NOW) == "today, 12:00"
    assert renderer._moment_label(utc(13), NOW) == "Fri 13, 00:00"
    assert renderer._moment_label(utc(15, month=12), NOW) == "Dec 15, 00:00"
    assert renderer._day_label(utc(12), NOW) == "Tomorrow"
    assert renderer._day_label(utc(13), NOW) == "Fri, Nov 13"


def test_span_notes_show_only_unusual_hours():
    assert renderer._span_note(EVENTS[2]) == ""
    assert renderer._span_note(EVENTS[1]) == "until 12:00"
    assert renderer._span_note(EVENTS[8]) == "12:00–17:00"
    assert renderer._span_note(event("x", "X", "WAR", utc(14, 10), utc(15))) == "from 10:00"
    assert renderer._span_note(event("x", "X", "WAR", utc(14), utc(15), day_precision=True)) == ""
