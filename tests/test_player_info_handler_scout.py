import importlib.util
import asyncio
import sys
import types
from pathlib import Path


def load_player_info_handler():
    stub_modules = {}

    class FakeEmbed:
        def __init__(self, title=None, description=None, color=None):
            self.title = title
            self.description = description
            self.color = color
            self.fields = []
            self.footer = None
            self.image_url = None

        def add_field(self, name, value, inline=True):
            self.fields.append({"name": name, "value": value, "inline": inline})

        def set_footer(self, text):
            self.footer = text

        def set_image(self, url):
            self.image_url = url

    discord_module = types.ModuleType("discord")
    discord_module.Interaction = object
    discord_module.Embed = FakeEmbed
    discord_module.File = lambda fp, filename=None: types.SimpleNamespace(fp=fp, filename=filename)
    discord_module.Color = types.SimpleNamespace(blue=lambda: "blue")
    discord_module.app_commands = types.SimpleNamespace(describe=lambda **_: lambda func: func)
    stub_modules["discord"] = discord_module

    discord_ext_module = types.ModuleType("discord.ext")
    commands_module = types.ModuleType("discord.ext.commands")
    commands_module.Bot = object
    stub_modules["discord.ext"] = discord_ext_module
    stub_modules["discord.ext.commands"] = commands_module

    services_player_info_module = types.ModuleType("services.player_info_service")
    services_player_info_module.IPlayerInfoService = object
    stub_modules["services"] = types.ModuleType("services")
    stub_modules["services.player_info_service"] = services_player_info_module

    services_interaction_module = types.ModuleType("services.interaction_tracking_service")
    services_interaction_module.InteractionTrackingService = object
    stub_modules["services.interaction_tracking_service"] = services_interaction_module

    services_kingshot_module = types.ModuleType("services.kingshot_data_service")
    services_kingshot_module.KingshotDataService = object
    stub_modules["services.kingshot_data_service"] = services_kingshot_module

    module_path = Path(__file__).resolve().parents[1] / "handlers" / "player_info_handler.py"
    spec = importlib.util.spec_from_file_location("player_info_handler_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None

    originals = {name: sys.modules.get(name) for name in stub_modules}
    try:
        sys.modules.update(stub_modules)
        spec.loader.exec_module(module)
    finally:
        for name, original in originals.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original

    return module.PlayerInfoHandler


PlayerInfoHandler = load_player_info_handler()


class FakeKingshotDataService:
    def __init__(self, arena_result):
        self.arena_result = arena_result
        self.arena_uids = []

    async def get_arena(self, uid):
        self.arena_uids.append(uid)
        return self.arena_result


def test_extract_leaderboard_entries_from_api_payload():
    payload = {
        "type": 8,
        "kid": 830,
        "entries": [
            {"rank": 1, "fid": 121704562},
            "bad-row",
            {"rank": 2, "fid": 125276803},
        ],
    }

    entries = PlayerInfoHandler._extract_leaderboard_entries(payload)

    assert entries == [
        {"rank": 1, "fid": 121704562},
        {"rank": 2, "fid": 125276803},
    ]


def test_build_stats_embed_matches_stats_style_for_scout_data():
    entry = {
        "rank": 1,
        "fid": 121704562,
        "name": "Old Name",
        "score": 572264916,
        "alliance": {"abbr": "OLD", "name": "Old Alliance"},
    }
    profile = {
        "fid": 121704562,
        "name": "Amoeba",
        "power": 572264916,
        "vip": 0,
        "stove_lv": 55,
        "life_tree_level": 10,
        "alliance": {"aid": 83900009, "abbr": "FKA", "name": "FateKillsAll"},
    }

    player_data = PlayerInfoHandler._build_player_data_from_kingshot(profile, entry, 830)
    embed = PlayerInfoHandler._build_stats_embed(
        player_id=str(player_data["playerId"]),
        player_name=player_data["name"],
        player_data=player_data,
        ks_data=profile,
        description=PlayerInfoHandler._format_kingshot_profile_summary(player_data),
    )
    fields = {field["name"]: field["value"] for field in embed.fields}

    assert embed.title == "📊 Amoeba"
    assert "👤 **Name:** Amoeba" in embed.description
    assert fields["Player ID"] == "`121704562`"
    assert fields["Kingdom"] == "830"
    assert fields["Castle Level"] == "55"
    assert fields["Power"] == "572,264,916"
    assert fields["VIP Level"] == "Hidden"
    assert fields["Alliance"] == "`[FKA]` FateKillsAll (`83900009`)"
    assert "Links" not in fields
    assert embed.footer is None


def test_get_arena_loadout_image_uses_local_hero_and_gear_ids(tmp_path):
    hero_dir = tmp_path / "heroes"
    gear_dir = tmp_path / "gear"
    hero_dir.mkdir()
    gear_dir.mkdir()
    (hero_dir / "50024.png").write_bytes(PlayerInfoHandler._write_rgba_png(1, 1, bytes([255, 0, 0, 255])))
    (hero_dir / "50021.png").write_bytes(PlayerInfoHandler._write_rgba_png(1, 1, bytes([0, 255, 0, 255])))
    (gear_dir / "1011501.png").write_bytes(PlayerInfoHandler._write_rgba_png(2, 2, bytes([0, 0, 255, 255] * 4)))
    (gear_dir / "1021501.png").write_bytes(PlayerInfoHandler._write_rgba_png(2, 2, bytes([255, 255, 0, 255] * 4)))
    (gear_dir / "1031501.png").write_bytes(PlayerInfoHandler._write_rgba_png(2, 2, bytes([255, 0, 255, 255] * 4)))
    (gear_dir / "1050024.png").write_bytes(PlayerInfoHandler._write_rgba_png(2, 2, bytes([255, 180, 0, 255] * 4)))
    service = FakeKingshotDataService(
        {
            "success": True,
            "data": {
                "heroes": [
                    {
                        "slot": 2,
                        "id": 50021,
                        "star": 20,
                        "equipment": [{"sid": 3, "eid": 1031501, "slv": 20, "rlv": 0}],
                    },
                    {
                        "slot": 1,
                        "id": 50024,
                        "star": 30,
                        "exclusive_equip": 1050024,
                        "exclusive_equip_lv": 10,
                        "equipment": [
                            {"sid": 2, "eid": 1021501, "slv": 70, "rlv": 3},
                            {"sid": 1, "eid": 1011501, "slv": 100, "rlv": 8},
                            {"sid": 3, "eid": 1031501, "slv": 61},
                        ],
                    },
                    {"id": 99999},
                    "bad-row",
                ]
            },
        }
    )
    handler = PlayerInfoHandler(
        player_info_service=object(),
        bot=object(),
        interaction_tracking_service=object(),
        kingshot_data_service=service,
    )
    original_dir = PlayerInfoHandler.HERO_IMAGE_DIR
    original_gear_dir = PlayerInfoHandler.HERO_GEAR_IMAGE_DIR
    PlayerInfoHandler.HERO_IMAGE_DIR = hero_dir
    PlayerInfoHandler.HERO_GEAR_IMAGE_DIR = gear_dir
    try:
        file = asyncio.run(handler._get_arena_loadout_image({"playerUid": "30669644"}, None))
    finally:
        PlayerInfoHandler.HERO_IMAGE_DIR = original_dir
        PlayerInfoHandler.HERO_GEAR_IMAGE_DIR = original_gear_dir

    assert service.arena_uids == ["30669644"]
    assert file.filename == "arena_loadout.png"
    file.fp.seek(0)
    strip_path = tmp_path / "strip.png"
    strip_path.write_bytes(file.fp.read())
    strip = PlayerInfoHandler._read_rgba_png(strip_path)
    assert strip["width"] == 235
    assert strip["height"] == 216


def test_build_star_row_image_draws_five_six_part_stars():
    row = PlayerInfoHandler._build_star_row_image(30)
    partial = PlayerInfoHandler._build_star_row_image(29)
    low = PlayerInfoHandler._build_star_row_image(5)

    assert row is not None
    assert row["width"] == 108
    assert row["height"] == PlayerInfoHandler.ARENA_STAR_ICON_SIZE
    assert _count_yellow_pixels(row) > _count_yellow_pixels(partial) > _count_yellow_pixels(low)


def _count_yellow_pixels(image):
    pixels = image["pixels"]
    return sum(1 for offset in range(0, len(pixels), 4) if pixels[offset] > 200 and pixels[offset + 1] > 200)


def test_format_general_gear_level_wraps_red_levels():
    normal_text, normal_background, normal_foreground = PlayerInfoHandler._format_general_gear_level(100)
    red_text, red_background, red_foreground = PlayerInfoHandler._format_general_gear_level(120)

    assert normal_text == "+100"
    assert red_text == "+20"
    assert red_background != normal_background
    assert red_foreground != normal_foreground


def test_annotate_gear_image_tints_red_stage_gear():
    image = {"width": 40, "height": 40, "pixels": bytes([100, 100, 100, 255] * 40 * 40)}

    normal = PlayerInfoHandler._annotate_gear_image(image, 100, 0)
    red = PlayerInfoHandler._annotate_gear_image(image, 120, 0)
    sample_offset = ((20 * 40) + 20) * 4

    assert red["pixels"][sample_offset] > normal["pixels"][sample_offset]
    assert red["pixels"][sample_offset + 1] < normal["pixels"][sample_offset + 1]
