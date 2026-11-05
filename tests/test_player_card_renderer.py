from pathlib import Path

import pytest

from services import player_card_renderer as card_renderer

IMAGES = Path(__file__).resolve().parents[1] / "images"


@pytest.fixture(autouse=True)
def fresh_cache():
    card_renderer.clear_caches()
    yield
    card_renderer.clear_caches()


def _png(path: Path, color=(255, 0, 0, 255), size=(4, 4)) -> None:
    width, height = size
    path.write_bytes(card_renderer.write_rgba_png(width, height, bytes(color) * width * height))


def _assets(tmp_path: Path) -> card_renderer.AssetDirs:
    dirs = {name: tmp_path / name for name in ("heroes", "hero_gear", "governor_gear", "charms")}
    for directory in dirs.values():
        directory.mkdir()
    return card_renderer.AssetDirs(**dirs)


def test_documented_gear_and_charm_ids_map_to_shipped_artwork():
    # docs/SCHEMAS.md example: { "slot": 1, "equipid": 1005, "charms": [201, 201, 201] }
    assert (IMAGES / "gear" / "1005.png").is_file()
    assert (IMAGES / "charms" / "201.png").is_file()
    for slot in range(1, 7):
        assert (IMAGES / "gear" / f"0{slot}.png").is_file()  # empty-slot silhouettes
        assert all((IMAGES / "gear" / f"{slot}00{tier}.png").is_file() for tier in range(1, 9))
    for troop in (1, 2, 3):
        assert all((IMAGES / "charms" / f"{troop}{level:02d}.png").is_file() for level in range(1, 23))

    card = card_renderer.render_player_card([], [{"slot": 1, "equipid": 1005, "charms": [201, 201, 201]}])

    assert card.gear_drawn == 1 and card.heroes_drawn == 0
    image = card_renderer.decode_png(card.png)
    charm_row = card_renderer.CHARM_SLOTS * card_renderer.CHARM_ICON_SIZE + 2 * card_renderer.CHARM_GAP
    column = max(card_renderer.GEAR_ICON_SIZE, charm_row)
    assert image.width == 6 * column + 5 * card_renderer.GEAR_COLUMN_SPACING
    assert image.height == card_renderer.GEAR_ICON_SIZE + card_renderer.GEAR_CHARM_TOP_GAP + card_renderer.CHARM_ICON_SIZE


def test_charm_level_is_inferred_from_id():
    assert card_renderer.charm_level(201) == 1
    assert card_renderer.charm_level("322") == 22
    assert card_renderer.charm_level(300) is None
    assert card_renderer.charm_level(5) is None
    assert card_renderer.charm_level(None) is None


def test_hidden_or_empty_gear_draws_no_strip(tmp_path):
    assets = _assets(tmp_path)

    assert card_renderer.build_gear_strip([], assets) == (None, 0)
    assert card_renderer.build_gear_strip([{"slot": 0, "equipid": 0, "charms": []}], assets) == (None, 0)
    assert card_renderer.render_player_card([], [], assets) == card_renderer.PlayerCard(None)
    assert card_renderer.render_player_card(None, None, assets).png is None


def test_unknown_gear_and_charm_ids_are_skipped_gracefully(tmp_path):
    assets = _assets(tmp_path)
    _png(assets.governor_gear / "1005.png", (10, 200, 10, 255))
    _png(assets.governor_gear / "03.png", (90, 90, 90, 255))
    _png(assets.charms / "201.png", (10, 10, 200, 255))

    strip, drawn = card_renderer.build_gear_strip(
        [
            {"slot": 1, "equipid": 1005, "charms": [201, 999, "bad"]},
            {"slot": 3, "equipid": 3999, "charms": [201]},  # unknown item -> silhouette, charm still shown
            {"slot": 7, "equipid": 1005},  # invalid slot
            {"slot": None, "equipid": 1005},
            "bad-row",
        ],
        assets,
    )

    assert drawn == 1
    assert strip is not None
    only_unknown, unknown_drawn = card_renderer.build_gear_strip([{"slot": 2, "equipid": 2999, "charms": [777]}], assets)
    assert (only_unknown, unknown_drawn) == (None, 0)


def test_render_is_pure_and_reuses_decoded_artwork(tmp_path, monkeypatch):
    assets = _assets(tmp_path)
    _png(assets.heroes / "50024.png", (200, 10, 10, 255), size=(137, 241))
    _png(assets.hero_gear / "1050024.png", (240, 180, 0, 255), size=(128, 128))
    _png(assets.governor_gear / "1005.png", (10, 200, 10, 255), size=(128, 128))
    _png(assets.charms / "201.png", (10, 10, 200, 255), size=(128, 128))
    heroes = [{"slot": 1, "id": 50024, "lv": 80, "star": 12, "exclusive_equip": 1050024, "exclusive_equip_lv": 5}]
    gear = [{"slot": 1, "equipid": 1005, "charms": [201, 201]}]
    reads = []
    original_read = card_renderer.read_png
    monkeypatch.setattr(card_renderer, "read_png", lambda path: reads.append(path.name) or original_read(path))

    first = card_renderer.render_player_card(heroes, gear, assets)
    decoded_once = sorted(reads)
    second = card_renderer.render_player_card(heroes, gear, assets)

    assert first == second
    assert first.heroes_drawn == 1 and first.gear_drawn == 1
    assert decoded_once == ["1005.png", "1050024.png", "201.png", "50024.png"]
    assert sorted(reads) == decoded_once  # second render decoded nothing
    hero = card_renderer.render_player_card(heroes, None, assets)
    gear_only = card_renderer.render_player_card(None, gear, assets)
    stacked = card_renderer.decode_png(first.png)
    hero_image = card_renderer.decode_png(hero.png)
    gear_image = card_renderer.decode_png(gear_only.png)
    assert stacked.height == hero_image.height + card_renderer.SECTION_GAP + gear_image.height
    assert stacked.width == max(hero_image.width, gear_image.width)


def test_hero_level_badge_overlays_portrait_without_resizing(tmp_path):
    assets = _assets(tmp_path)
    _png(assets.heroes / "50024.png", (200, 200, 200, 255), size=(76, 134))

    plain = card_renderer.render_player_card([{"id": 50024}], None, assets)
    leveled = card_renderer.render_player_card([{"id": 50024, "lv": 80}], None, assets)

    plain_image = card_renderer.decode_png(plain.png)
    leveled_image = card_renderer.decode_png(leveled.png)
    assert (plain_image.width, plain_image.height) == (leveled_image.width, leveled_image.height) == (76, 134)
    assert plain_image.pixels != leveled_image.pixels
    bottom_left = ((130 * 76) + 4) * 4
    assert leveled_image.pixels[bottom_left : bottom_left + 3] != plain_image.pixels[bottom_left : bottom_left + 3]


def test_unknown_or_corrupt_hero_images_are_skipped(tmp_path):
    assets = _assets(tmp_path)
    (assets.heroes / "50001.png").write_bytes(b"not a png")
    _png(assets.heroes / "50002.png")

    card = card_renderer.render_player_card([{"id": 50001}, {"id": 99999}, {"id": 50002}, {"id": None}, "x"], None, assets)

    assert card.heroes_drawn == 1
    assert card.png is not None
