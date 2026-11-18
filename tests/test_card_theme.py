import discord

from services import card_drawing, card_theme
from services.card_theme import CHRISTMAS, CLASSIC, resolve_theme


def colors(card):
    return {color for _, color in card.image.getcolors(maxcolors=1 << 24)}


def test_the_theme_is_picked_by_name_and_falls_back_to_the_default():
    default = card_theme.THEMES[card_theme.DEFAULT_THEME]

    assert resolve_theme(None) is default and resolve_theme("  ") is default
    assert resolve_theme(" Classic ") is CLASSIC and resolve_theme("christmas") is CHRISTMAS
    assert resolve_theme("halloween") is default


def test_the_classic_theme_keeps_discords_own_embed_colors():
    assert CLASSIC.embed_success == discord.Color.green().value
    assert CLASSIC.embed_warning == discord.Color.orange().value
    assert CLASSIC.embed_error == discord.Color.red().value
    assert CLASSIC.embed_info == discord.Color.blue().value
    assert CLASSIC.embed_neutral == discord.Color.blurple().value


def test_every_theme_tells_its_status_colors_apart():
    for theme in card_theme.THEMES.values():
        assert len({theme.good, theme.amber, theme.red, theme.blue, theme.muted, theme.text}) == 6
        assert len({theme.embed_success, theme.embed_warning, theme.embed_error, theme.embed_info}) == 4


def test_decoration_never_paints_a_status_color():
    # Cards are read by color: an empty card, whatever its size, must not show any.
    status = {card_drawing.GOOD, card_drawing.AMBER, card_drawing.RED, card_drawing.BLUE}
    for height in (34, 60, 190, 380):
        card = card_drawing.Card(height)
        card.header(0, "Title", card_drawing.MUTED, count=3, note="note")

        assert card.image.size == (card_drawing.px(card_drawing.CARD_WIDTH), card_drawing.px(height))
        assert not status & colors(card)


def test_value_bars_and_presents_keep_their_color():
    card = card_drawing.Card(80)
    card.bar(12, 40, 200, 6, card_drawing.GOOD, candy=True)
    card.bar(12, 50, 2, 6, card_drawing.RED, candy=True)
    card.present(12, 60, 16, card_drawing.BLUE)

    assert {card_drawing.GOOD, card_drawing.RED, card_drawing.BLUE} <= colors(card)
