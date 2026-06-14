import importlib.util
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

        def add_field(self, name, value, inline=True):
            self.fields.append({"name": name, "value": value, "inline": inline})

        def set_footer(self, text):
            self.footer = text

    discord_module = types.ModuleType("discord")
    discord_module.Interaction = object
    discord_module.Embed = FakeEmbed
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


def test_build_scout_player_embed_matches_stats_style():
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

    embed = PlayerInfoHandler._build_scout_player_embed(entry, profile, 830)
    fields = {field["name"]: field["value"] for field in embed.fields}

    assert embed.title == "📊 #1 Amoeba"
    assert "👤 **Name:** Amoeba" in embed.description
    assert fields["Player ID"] == "`121704562`"
    assert fields["Kingdom"] == "830"
    assert fields["Castle Level"] == "55"
    assert fields["Power"] == "572,264,916"
    assert fields["VIP Level"] == "Hidden"
    assert fields["Alliance"] == "`[FKA]` FateKillsAll (`83900009`)"
    assert "Player details" in fields["Links"]
