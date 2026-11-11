from types import SimpleNamespace

import pytest


@pytest.fixture
def gift_cards(monkeypatch):
    """Record what every gift-code result card would show instead of drawing it."""
    drawn = []

    def fake_render(stats, **options):
        drawn.append(SimpleNamespace(stats=stats, **options))
        return b"\x89PNG\r\n\x1a\n card"

    monkeypatch.setattr("services.gift_card_renderer.render_redemption_card", fake_render)
    return drawn
