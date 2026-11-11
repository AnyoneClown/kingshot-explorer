import io

from PIL import Image

from services import card_drawing
from services import kvk_card_renderer as renderer


def opened(png):
    image = Image.open(io.BytesIO(png))
    image.load()
    return image


def colors(image):
    return {color for _, color in image.convert("RGB").getcolors(maxcolors=1 << 24)}


def height(points):
    return round(points * card_drawing.SCALE)


def match(season, opponent, prep, castle):
    return {"season_id": season, "opponent": opponent, "prepResult": prep, "castleResult": castle}


STATS = {
    "history": [match(20, 961, "loss", "loss"), match(19, 631, "win", "win"), match(18, 656, "unknown", "win")],
    "matchCount": 3,
    "wins": 2,
    "losses": 1,
    "winRate": 66.7,
    "prepWins": 1,
    "prepLosses": 1,
    "prepWinRate": 50.0,
    "attacks": 2,
    "defenses": 1,
    "castleCaptures": 1,
    "defensesHeld": 0,
    "currentStreak": "L1",
}


def test_history_card_has_a_fixed_landscape_layout():
    card = opened(renderer.render_kvk_history_card(795, STATS))

    assert card.format == "PNG" and card.width == height(card_drawing.CARD_WIDTH)
    assert card.height == height(
        card_drawing.HEADER_HEIGHT + renderer.TILES_HEIGHT + renderer.SEASONS_HEIGHT + renderer.TOTALS_HEIGHT
    )
    assert card.width > card.height
    # Wins and losses of the seasons are colored squares; an unknown result is neither.
    assert {card_drawing.GOOD, card_drawing.RED} <= colors(card)


def test_history_card_survives_missing_numbers_and_no_matches():
    empty = {"history": [], "matchCount": 0, "wins": 0, "losses": 0, "prepWins": None, "prepLosses": None}

    card = opened(renderer.render_kvk_history_card(830, empty))

    assert card.height == opened(renderer.render_kvk_history_card(795, STATS)).height
    # No season squares: nothing is green (red is still the header's dot).
    assert card_drawing.GOOD not in colors(card)
    assert opened(renderer.render_kvk_history_card(1, {})).width


def test_history_helpers_keep_unknown_apart_from_zero():
    assert renderer._record(STATS, "wins", "losses") == "2–1"
    assert renderer._record({"wins": None, "losses": 3}, "wins", "losses") == "––3"
    assert renderer._rate(STATS, "wins", "losses", "winRate") == "67% win rate"
    assert renderer._rate({"wins": 0, "losses": 0, "winRate": 0}, "wins", "losses", "winRate") == "no decided matches"
    assert renderer._rate({}, "wins", "losses", "winRate") == "win rate unavailable"
    assert renderer._streak(STATS) == ("L1", "loss in a row", card_drawing.RED)
    assert renderer._streak({"currentStreak": "W3"}) == ("W3", "wins in a row", card_drawing.GOOD)
    assert renderer._streak({"currentStreak": "N/A"})[0] == "–"


def test_comparison_picks_the_better_side_per_metric():
    stats_a = dict(STATS)
    stats_b = dict(STATS, wins=5, losses=0, winRate=100.0, prepWins=0, prepLosses=0, prepWinRate=0, castleCaptures=None)
    direct = {"available": True, "kingdom_a": {"wins": 1}, "kingdom_b": {"wins": 2}}

    rows = {row[0]: row for row in renderer.comparison_metrics(stats_a, stats_b, direct)}

    def leader(label):
        _, value_a, value_b, lower_is_better, _ = rows[label]
        return renderer._leader(value_a, value_b, lower_is_better)

    assert leader("Castle win rate") == "b" and leader("Castle wins") == "b"
    # Fewer losses is better.
    assert leader("Castle losses") == "b"
    # No decided prep matches: the rate is unknown, not 0%.
    assert rows["Prep win rate"][2] is None and leader("Prep win rate") is None
    assert leader("Prep wins") == "a"
    assert leader("Castles captured") is None  # unknown on one side
    assert leader("Defenses held") is None  # equal
    assert leader("Head-to-head wins") == "b"
    assert "Head-to-head wins" not in {row[0] for row in renderer.comparison_metrics(stats_a, stats_b, None)}
    assert "Head-to-head wins" not in {
        row[0] for row in renderer.comparison_metrics(stats_a, stats_b, {"available": False})
    }


def test_compare_card_grows_with_the_head_to_head_row():
    direct = {"available": True, "kingdom_a": {"wins": 1}, "kingdom_b": {"wins": 0}}

    plain = opened(renderer.render_kvk_compare_card(795, 830, STATS, STATS))
    with_direct = opened(renderer.render_kvk_compare_card(795, 830, STATS, STATS, direct))

    assert with_direct.height - plain.height == height(renderer.METRIC_ROW_HEIGHT)
    assert plain.width == height(card_drawing.CARD_WIDTH) and plain.width > with_direct.height
    assert {renderer.SIDE_A, renderer.SIDE_B} <= colors(with_direct)
    assert opened(renderer.render_kvk_compare_card(1, 2, {}, {})).width
