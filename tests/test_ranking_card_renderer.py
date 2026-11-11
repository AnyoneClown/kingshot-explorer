import io

from PIL import Image

from services import card_drawing
from services import ranking_card_renderer as renderer
from services.ranking_card_renderer import RankedRow, VersusSection


def opened(png):
    image = Image.open(io.BytesIO(png))
    image.load()
    return image


def colors(image):
    return {color for _, color in image.convert("RGB").getcolors(maxcolors=1 << 24)}


def height(points):
    return round(points * card_drawing.SCALE)


def rows(count, tag=""):
    return [RankedRow(name=f"Player {index}", value=1000 - index * 10, rank=index + 1, tag=tag) for index in range(count)]


def test_leaderboard_has_a_row_per_player_and_podium_colors():
    card = opened(renderer.render_leaderboard_card("Kingdom 795 · Mystic Trial", rows(5), note="score"))

    assert card.width == height(card_drawing.CARD_WIDTH)
    assert card.height == height(card_drawing.HEADER_HEIGHT + 5 * renderer.LEADERBOARD_ROW_HEIGHT + 10)
    assert set(renderer.PODIUM.values()) <= colors(card)


def test_leaderboard_is_capped_and_accepts_rows_without_values():
    many = opened(renderer.render_leaderboard_card("Top", rows(40)))
    blank = opened(renderer.render_leaderboard_card("Top", [RankedRow(name="Nobody")]))

    assert many.height == height(
        card_drawing.HEADER_HEIGHT + renderer.MAX_LEADERBOARD_ROWS * renderer.LEADERBOARD_ROW_HEIGHT + 10
    )
    assert blank.height == height(card_drawing.HEADER_HEIGHT + renderer.LEADERBOARD_ROW_HEIGHT + 10)
    assert opened(renderer.render_leaderboard_card("Top", [])).width


def test_ranked_row_labels_and_values():
    assert RankedRow(name="Mila", tag="AQM").label == "[AQM] Mila"
    assert RankedRow(name="Mila").label == "Mila"
    assert RankedRow(name="Mila", value=48_200_000_000).shown_value == "48.2B"
    assert RankedRow(name="Mila", value=5, text="5 pts").shown_value == "5 pts"
    assert RankedRow(name="Mila").shown_value == "–"


def test_versus_card_has_a_block_per_section_and_marks_missing_sides():
    sections = [
        VersusSection("Top power", "player power", rows(5, "AQM"), rows(3, "BSN")),
        VersusSection("Top kills", "kills", rows(9), None),
        VersusSection("Empty", "", [], []),
    ]

    card = opened(renderer.render_versus_card("Kingdom 795", "Kingdom 830", sections))

    assert [section.row_count for section in sections] == [5, renderer.MAX_VERSUS_ROWS, 1]
    assert card.height == height(sum(section.height for section in sections))
    assert card.width == height(card_drawing.CARD_WIDTH)
    assert {renderer.SIDE_A, renderer.SIDE_B} <= colors(card)


def test_names_in_other_scripts_use_fallback_fonts():
    font = card_drawing.get_font(10.5, 500)

    for name in ("Володар Ночі", "王者归来", "한국의전사", "カタナ侍", "Ωmega", "★ Queen ♛"):
        runs = card_drawing.text_runs(name, font)
        assert runs and card_drawing.can_draw(name, font), name
        assert card_drawing.text_width(name, font) > 0
    # Latin text never leaves the card font.
    assert card_drawing.text_runs("Sir Lancelot", font) == [(font, "Sir Lancelot")]
    # Characters outside it come from another font.
    assert any(run_font is not font for run_font, _ in card_drawing.text_runs("Mila Ж", font))


def test_characters_no_font_has_are_dropped_instead_of_drawn_as_boxes():
    font = card_drawing.get_font(10.5, 500)

    assert "".join(run for _, run in card_drawing.text_runs("Ragnar 🔥 the Bold", font)) == "Ragnar the Bold"
    assert "".join(run for _, run in card_drawing.text_runs("A‍️B", font)) == "AB"
    assert card_drawing.can_draw("🔥🔥", font) is False
    assert card_drawing.text_runs("", font) == []
    # A name that cannot be drawn at all is labeled instead of leaving a blank row.
    card = opened(renderer.render_leaderboard_card("Top", [RankedRow(name="🔥🔥", value=5, tag="AQM")]))
    assert card.height == height(card_drawing.HEADER_HEIGHT + renderer.LEADERBOARD_ROW_HEIGHT + 10)


def test_compact_numbers_and_number_parsing():
    assert card_drawing.compact_number(48_200_000_000) == "48.2B"
    assert card_drawing.compact_number("532,000,000") == "532M"
    assert card_drawing.compact_number(18_250) == "18.25K"
    assert card_drawing.compact_number(950) == "950"
    assert card_drawing.compact_number(None) == "–"
    assert card_drawing.as_number("1,234") == 1234.0
    assert card_drawing.as_number(True) is None and card_drawing.as_number("n/a") is None
