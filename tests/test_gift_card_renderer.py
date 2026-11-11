import io
from datetime import datetime, timedelta, timezone

from PIL import Image

from services import card_drawing
from services import gift_card_renderer as renderer
from services.gift_card_renderer import build_redemption_stats, kingdom_rows, render_redemption_card

NOW = datetime(2026, 11, 11, 8, tzinfo=timezone.utc)


def results(*groups):
    """``(kingdom, status, count)`` groups as redemption results."""
    rows = []
    for kingdom, status, count in groups:
        for _ in range(count):
            rows.append({"player_id": str(len(rows) + 1), "status_category": status, "kingdom": kingdom})
    return rows


def opened(png):
    image = Image.open(io.BytesIO(png))
    image.load()
    return image


def colors(image):
    return {color for _, color in image.convert("RGB").getcolors(maxcolors=1 << 24)}


def height(points):
    return round(points * card_drawing.SCALE)


def test_stats_count_statuses_overall_and_per_kingdom():
    stats = build_redemption_stats(
        "CODE",
        results(
            ("795", "success", 5),
            ("795", "already_redeemed", 2),
            ("795", "invalid_id", 1),
            ("830", "success", 3),
            ("830", "api_rejected", 1),
        ),
    )

    assert stats.total == 12 and stats.received == 8 and stats.rewarded == 10
    assert stats.coverage == 83
    assert stats.counts == {"success": 8, "already_redeemed": 2, "api_rejected": 1, "invalid_id": 1, "skipped": 0}
    first, second = stats.kingdoms
    assert (first.kingdom, first.total, first.rewarded, first.label) == ("795", 8, 7, "Kingdom 795")
    assert (second.kingdom, second.total, second.rewarded) == ("830", 4, 3)


def test_stats_prefer_the_kingdom_resolved_while_redeeming():
    rows = [
        {"player_id": "1", "status_category": "success", "kingdom": "830"},  # transferred since it was stored
        {"player_id": "2", "status_category": "success"},
        {"player_id": "3", "status_category": "success", "kingdom": None},
        {"player_id": "4", "status_category": "made up"},
    ]

    stats = build_redemption_stats("CODE", rows, {"1": "795", "2": "795", "3": " ", "4": "unknown"})

    assert {row.kingdom: row.total for row in stats.kingdoms} == {"830": 1, "795": 1, None: 2}
    # An unrecognized status counts as a rejection instead of disappearing.
    assert stats.counts["api_rejected"] == 1
    assert stats.kingdoms[-1].label == "Unknown kingdom"


def test_empty_stats_do_not_divide_by_zero():
    stats = build_redemption_stats("CODE", [])

    assert (stats.total, stats.coverage, stats.kingdoms) == (0, 0, ())
    assert opened(render_redemption_card(stats, now=NOW)).height == height(
        card_drawing.HEADER_HEIGHT + renderer.HERO_HEIGHT + renderer.SUMMARY_HEIGHT
    )


def test_small_kingdoms_are_folded_into_one_row():
    stats = build_redemption_stats("CODE", results(*[(str(700 + index), "success", 10 - index) for index in range(8)]))

    rows = kingdom_rows(stats)

    assert [row.kingdom for row in rows[:-1]] == ["700", "701", "702", "703"]
    assert (rows[-1].label, rows[-1].total) == ("4 more kingdoms", 6 + 5 + 4 + 3)
    assert len(kingdom_rows(build_redemption_stats("CODE", results(("795", "success", 2), ("830", "success", 1))))) == 2


def test_a_roster_without_any_known_kingdom_has_no_kingdom_section():
    stats = build_redemption_stats("CODE", [{"player_id": "1", "status_category": "success"}])

    assert kingdom_rows(stats) == []
    card = opened(render_redemption_card(stats, now=NOW))
    assert card.height == height(card_drawing.HEADER_HEIGHT + renderer.HERO_HEIGHT + renderer.SUMMARY_HEIGHT)


def test_card_shows_every_status_that_occurred_and_a_row_per_kingdom():
    stats = build_redemption_stats(
        "KINGSHOT2026",
        results(
            ("795", "success", 96),
            ("795", "already_redeemed", 21),
            ("795", "api_rejected", 1),
            ("830", "success", 31),
            ("830", "invalid_id", 1),
        ),
    )

    card = opened(render_redemption_card(stats, expires_at=NOW + timedelta(days=9), now=NOW))

    assert card.format == "PNG" and card.width == height(card_drawing.CARD_WIDTH)
    assert card.height == height(
        card_drawing.HEADER_HEIGHT * 2
        + renderer.HERO_HEIGHT
        + renderer.SUMMARY_HEIGHT
        + 2 * renderer.KINGDOM_ROW_HEIGHT
        + 8
    )
    assert card.width > card.height
    # One rejected and one invalid account among 150 still get their color on the bar.
    assert {card_drawing.GOOD, card_drawing.BLUE, card_drawing.RED, card_drawing.AMBER} <= colors(card)


def test_card_accepts_a_naive_or_past_expiry_and_a_very_long_code():
    stats = build_redemption_stats("X" * 80, results(("795", "success", 1)))

    for expires_at in (None, datetime(2026, 11, 1), datetime(2026, 12, 1), NOW - timedelta(days=1)):
        assert opened(render_redemption_card(stats, title="Redeeming…", expires_at=expires_at, now=NOW)).width


def test_statuses_without_accounts_get_no_tile_color_but_received_is_always_shown():
    stats = build_redemption_stats("CODE", results(("795", "already_redeemed", 3)))

    card = colors(opened(render_redemption_card(stats, title="No new rewards", accent=card_drawing.MUTED, now=NOW)))

    assert card_drawing.GOOD in card  # the "Received 0" tile
    assert card_drawing.RED not in card and card_drawing.AMBER not in card
