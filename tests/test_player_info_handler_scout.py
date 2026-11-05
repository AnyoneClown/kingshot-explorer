import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from handlers.player_info_handler import PlayerInfoHandler
from services import player_card_renderer as card_renderer


def test_scout_uses_mystic_trial_board_type():
    assert PlayerInfoHandler.SCOUT_BOARD_TYPE == 20


class FakeKingshotDataService:
    def __init__(self, arena_result=None, search_result=None):
        self.arena_result = arena_result
        self.search_result = search_result
        self.arena_uids = []
        self.search_calls = []

    async def get_arena(self, uid):
        self.arena_uids.append(uid)
        return self.arena_result

    async def search_leaderboard(self, board_type, uid, kid):
        self.search_calls.append((board_type, uid, kid))
        return self.search_result


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
        "uid": 30669644,
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
        mystic_trial={"rank": 93, "entry": {"rank": 93, "score": 1239}},
        description=None,
    )
    fields = {field.name: field.value for field in embed.fields}

    assert embed.title == "📊 Amoeba"
    assert embed.description is None
    assert fields["Player ID"] == "`121704562`"
    assert player_data["playerUid"] == "30669644"
    assert fields["Kingdom"] == "830"
    assert fields["Castle Level"] == "55"
    assert fields["Power"] == "572,264,916"
    assert fields["VIP Level"] == "Hidden"
    assert fields["Alliance"] == "`[FKA]` FateKillsAll (`83900009`)"
    assert fields["Mystic Trial"] == "Kingdom Rank: #93\nScore: 1,239"
    assert "Links" not in fields
    assert embed.footer.text is None
    assert embed.fields[0].name == "Power"


def test_get_mystic_trial_searches_type_20_by_uid_and_kingdom():
    service = FakeKingshotDataService(search_result={"success": True, "data": {"rank": 93, "entry": {"score": 1239}}})
    handler = PlayerInfoHandler(
        player_info_service=object(),
        bot=object(),
        interaction_tracking_service=object(),
        kingshot_data_service=service,
    )

    result = asyncio.run(handler._get_mystic_trial({"kingdom": 830}, {"uid": 30669644}))

    assert service.search_calls == [(20, "30669644", "830")]
    assert result == {"rank": 93, "entry": {"score": 1239}}


def test_scout_player_data_keeps_entry_uid_for_mystic_trial_search():
    entry = {"fid": 121704562, "uid": 30669644, "kid": 830}
    profile = {"fid": 121704562, "name": "Amoeba", "stove_lv": 55}

    player_data = PlayerInfoHandler._build_player_data_from_kingshot(profile, entry, 830)

    assert PlayerInfoHandler._extract_player_uid(player_data, profile) == "30669644"


def test_player_card_uses_local_hero_and_gear_ids(tmp_path):
    hero_dir = tmp_path / "heroes"
    gear_dir = tmp_path / "gear"
    hero_dir.mkdir()
    gear_dir.mkdir()
    (hero_dir / "50024.png").write_bytes(card_renderer.write_rgba_png(1, 1, bytes([255, 0, 0, 255])))
    (hero_dir / "50021.png").write_bytes(card_renderer.write_rgba_png(1, 1, bytes([0, 255, 0, 255])))
    (gear_dir / "1011501.png").write_bytes(card_renderer.write_rgba_png(2, 2, bytes([0, 0, 255, 255] * 4)))
    (gear_dir / "1021501.png").write_bytes(card_renderer.write_rgba_png(2, 2, bytes([255, 255, 0, 255] * 4)))
    (gear_dir / "1031501.png").write_bytes(card_renderer.write_rgba_png(2, 2, bytes([255, 0, 255, 255] * 4)))
    (gear_dir / "1050024.png").write_bytes(card_renderer.write_rgba_png(2, 2, bytes([255, 180, 0, 255] * 4)))
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
    handler.ASSET_DIRS = card_renderer.AssetDirs(
        heroes=hero_dir, hero_gear=gear_dir, governor_gear=tmp_path / "none", charms=tmp_path / "none"
    )

    async def check():
        heroes = await handler._fetch_arena_heroes("30669644")
        embed = discord.Embed()
        file = await handler._attach_player_card(embed, heroes, {"gear": []})
        return embed, file

    embed, file = asyncio.run(check())

    assert service.arena_uids == ["30669644"]
    assert file.filename == "player_card.png"
    assert embed.image.url == "attachment://player_card.png"
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Governor Gear"] == "Equipment hidden"
    assert "Arena" not in fields
    file.fp.seek(0)
    strip = card_renderer.decode_png(file.fp.read())
    assert strip.width == 235
    assert strip.height == 216


def test_scout_single_summary_navigation_and_unavailable_data():
    async def check():
        entries = [{"rank": index + 1, "fid": None, "uid": index + 200, "score": index} for index in range(20)]
        active, peak = 0, 0

        async def get_player(uid):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            index = int(uid) - 200
            return {"success": True, "data": {
                "uid": int(uid), "fid": index + 100, "name": f"Player {index}", "power": 0, "stove_lv": 0,
            }}

        service = SimpleNamespace(
            get_kingdom_board=AsyncMock(return_value={"success": True, "data": {"entries": entries}}),
            get_player=AsyncMock(side_effect=get_player),
            get_player_by_fid=AsyncMock(),
        )
        handler = PlayerInfoHandler(object(), object(), object(), service)
        handler._fetch_arena_heroes = AsyncMock(side_effect=[[{"id": 50024}], None, None])
        handler._render_player_card = AsyncMock(
            side_effect=[card_renderer.PlayerCard(b"image", heroes_drawn=1)] + [card_renderer.PlayerCard(None)] * 2
        )
        interaction = _scout_interaction()
        message = interaction.followup.send.return_value
        await handler._handle_scout_slash(interaction, 830, 99)
        interaction.followup.send.assert_awaited_once()
        assert service.get_player.await_count == 15
        assert 1 <= peak <= 3
        service.get_player_by_fid.assert_not_awaited()
        view = interaction.followup.send.call_args.kwargs["view"]
        assert len(view.entries) == 15
        assert len(view.build_summary().description) < 4096
        assert view.previous_button.disabled and not view.next_button.disabled
        assert view.summary_button.disabled
        assert "**#1 Player 0** · 0" in view.build_summary().description
        assert "public message" in view.build_summary().footer.text
        assert not any(option.default for option in view.player_select.options)
        await _select(view, interaction, "0")
        first = interaction.edit_original_response.call_args.kwargs
        assert [file.filename for file in first["attachments"]] == ["player_card.png"]
        assert first["embed"].image.url == "attachment://player_card.png"
        fields = {field.name: field.value for field in first["embed"].fields}
        assert fields["Power"] == "0" and fields["Castle Level"] == "0"
        assert fields["VIP Level"] == "Unavailable"
        await view.next_button.callback(interaction)
        second = interaction.edit_original_response.call_args.kwargs
        assert second["attachments"] == []
        assert second["embed"].fields[-1].value == "Loadout unavailable"
        assert view.current_player == 1
        await view.summary_button.callback(interaction)
        assert interaction.response.edit_message.call_args.kwargs["attachments"] == []
        await _select(view, interaction, "14")
        assert view.current_player == 14 and view.next_button.disabled
        assert not view.showing_summary
        assert view.player_select.options[14].default
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_arena(uid):
            entered.set()
            await release.wait()
            return None

        handler._fetch_arena_heroes = slow_arena
        handler._render_player_card = AsyncMock(return_value=card_renderer.PlayerCard(None))
        pending = asyncio.create_task(_select(view, interaction, "14"))
        await entered.wait()
        assert not await view.interaction_check(interaction)
        assert view.current_player == 14
        release.set()
        await pending
        assert not view._loading
        assert service.get_player.await_count == 15
        service.get_player_by_fid.assert_not_awaited()
        stranger = SimpleNamespace(user=SimpleNamespace(id=2), response=SimpleNamespace(send_message=AsyncMock()))
        assert not await view.interaction_check(stranger)
        assert stranger.response.send_message.call_args.kwargs["ephemeral"]
        await view.on_timeout()
        assert all(item.disabled for item in view.children)
        message.edit.assert_awaited_once()
        assert view.timeout == 900

    asyncio.run(check())


async def _select(view, interaction, value):
    view.player_select._values = [value]
    await view.player_select.callback(interaction)


def _scout_interaction():
    return SimpleNamespace(
        user=SimpleNamespace(id=1, name="Tester", discriminator="0"),
        guild=None,
        response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))),
        edit_original_response=AsyncMock(),
    )


@pytest.mark.parametrize("source", ["uid_only", "fid_only", "embedded"])
def test_scout_resolves_identity_for_summary_selector_and_cached_details(source):
    async def check():
        profile = {
            "uid": 28584855,
            "fid": 117248174,
            "name": "Resolved Player",
            "power": 0,
            "vip": 0,
            "stove_lv": 0,
            "kid": 830,
            "alliance": {"abbr": "FKA", "name": "FateKillsAll"},
            "rank": 99,
            "score": 999999,
        }
        entry = {"rank": 1, "uid": 28584855, "score": 2846, "fid": None}
        if source == "fid_only":
            entry.pop("uid")
            entry["fid"] = 117248174
        elif source == "embedded":
            entry["player"] = profile
        service = SimpleNamespace(
            get_kingdom_board=AsyncMock(return_value={"success": True, "data": {"entries": [entry]}}),
            get_player=AsyncMock(return_value={"success": True, "data": profile}),
            get_player_by_fid=AsyncMock(return_value={"success": True, "data": profile}),
        )
        handler = PlayerInfoHandler(object(), object(), object(), service)
        handler._fetch_arena_heroes = AsyncMock(return_value=None)
        interaction = _scout_interaction()
        await handler._handle_scout_slash(interaction, 830, 5)
        view = interaction.followup.send.call_args.kwargs["view"]
        assert view.entries[0]["player"] == profile
        assert view.entries[0]["rank"] == 1 and view.entries[0]["score"] == 2846
        assert "**#1 Resolved Player** · 2,846" in view.build_summary().description
        assert "999,999" not in view.build_summary().description
        assert "Resolved Player" in view.player_select.options[0].label
        assert "117248174" in view.player_select.options[0].description
        await _select(view, interaction, "0")
        embed = interaction.edit_original_response.call_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        assert embed.title == "📊 Resolved Player"
        assert fields["Player ID"] == "`117248174`"
        assert fields["Power"] == "0" and fields["Castle Level"] == "0"
        assert fields["Alliance"] == "`[FKA]` FateKillsAll"
        assert fields["Mystic Trial"] == "Kingdom Rank: #1\nScore: 2,846"
        await view.summary_button.callback(interaction)
        await _select(view, interaction, "0")
        if source == "uid_only":
            service.get_player.assert_awaited_once_with("28584855")
            service.get_player_by_fid.assert_not_awaited()
        elif source == "fid_only":
            service.get_player_by_fid.assert_awaited_once_with("117248174")
            service.get_player.assert_not_awaited()
        else:
            service.get_player.assert_not_awaited()
            service.get_player_by_fid.assert_not_awaited()

    asyncio.run(check())


def test_scout_unavailable_profile_preserves_board_score_and_uid_arena(tmp_path):
    async def check():
        uid = 28584855
        service = SimpleNamespace(
            get_kingdom_board=AsyncMock(return_value={"success": True, "data": {
                "entries": [{"rank": 1, "uid": uid, "score": 2846, "fid": None}],
            }}),
            get_player=AsyncMock(return_value={"success": False, "error_message": "Temporarily unavailable"}),
            get_player_by_fid=AsyncMock(),
            get_arena=AsyncMock(return_value={"success": True, "data": {"heroes": [{"id": 50024}]}}),
        )
        handler = PlayerInfoHandler(object(), object(), object(), service)
        handler.ASSET_DIRS = card_renderer.AssetDirs(
            heroes=tmp_path, hero_gear=tmp_path, governor_gear=tmp_path, charms=tmp_path
        )
        (tmp_path / "50024.png").write_bytes(card_renderer.write_rgba_png(1, 1, bytes([255, 0, 0, 255])))
        interaction = _scout_interaction()
        await handler._handle_scout_slash(interaction, 830, 5)
        view = interaction.followup.send.call_args.kwargs["view"]
        assert "2,846" in view.build_summary().description
        assert "Unavailable" in view.player_select.options[0].description
        await _select(view, interaction, "0")
        sent = interaction.edit_original_response.call_args.kwargs
        fields = {field.name: field.value for field in sent["embed"].fields}
        assert fields["Player ID"] == "`Unavailable`"
        assert fields["Power"] == "Unavailable"
        assert fields["Mystic Trial"] == "Kingdom Rank: #1\nScore: 2,846"
        assert sent["attachments"][0].filename == "player_card.png"
        assert fields["Governor Gear"] == "Unavailable"
        service.get_arena.assert_awaited_once_with(str(uid))
        service.get_player_by_fid.assert_not_awaited()
        assert service.get_player.await_count >= 1
        assert all(call.args == (str(uid),) for call in service.get_player.await_args_list)

    asyncio.run(check())


def _stats_interaction():
    return SimpleNamespace(
        user=SimpleNamespace(id=1, name="Tester", discriminator="0", display_name="Tester"),
        guild=None,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class _PlayerInfo:
    def __init__(self, result):
        self.result = result

    async def get_player_info(self, player_id):
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_stats_reports_lookup_outage_instead_of_not_found():
    from services.player_info_service import PlayerLookupUnavailable

    tracking = SimpleNamespace(track_player_lookup=AsyncMock())
    handler = PlayerInfoHandler(_PlayerInfo(PlayerLookupUnavailable("down")), object(), tracking, None)
    interaction = _stats_interaction()

    asyncio.run(handler._handle_player_stats_slash(interaction, "277720981"))

    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert "Unavailable" in embed.title and "Not Found" not in embed.title
    assert "try again" in embed.description.lower()
    tracking.track_player_lookup.assert_not_awaited()


def test_stats_not_found_and_invalid_id():
    tracking = SimpleNamespace(track_player_lookup=AsyncMock())
    handler = PlayerInfoHandler(_PlayerInfo(None), object(), tracking, None)
    interaction = _stats_interaction()
    asyncio.run(handler._handle_player_stats_slash(interaction, "123"))
    assert interaction.followup.send.call_args.kwargs["embed"].title == "❌ Player Not Found"
    assert tracking.track_player_lookup.await_args.kwargs["success"] is False

    invalid = _stats_interaction()
    asyncio.run(handler._handle_player_stats_slash(invalid, "abc"))
    invalid.response.defer.assert_not_awaited()
    assert invalid.response.send_message.call_args.kwargs["ephemeral"] is True


def test_stats_renders_card_off_the_event_loop_with_gear(monkeypatch):
    import threading

    loop_thread = threading.get_ident()
    render_threads = []
    original_render = card_renderer.render_player_card

    def recording_render(heroes, gear, assets=None):
        render_threads.append(threading.get_ident())
        return original_render(heroes, gear, assets)

    monkeypatch.setattr(card_renderer, "render_player_card", recording_render)
    profile = {
        "uid": 85826384, "fid": 277720981, "name": "Moonberry", "power": 940162, "vip": 3, "kid": 7,
        "alliance": {"aid": 1, "abbr": "AQM", "name": "Aquarium"},
        "gear": [{"slot": 1, "equipid": 1005, "charms": [201, 201, 203]}, {"slot": 2, "equipid": 0, "charms": []}],
    }
    service = SimpleNamespace(
        get_player=AsyncMock(return_value={"success": True, "data": profile}),
        get_player_by_fid=AsyncMock(),
        search_leaderboard=AsyncMock(return_value={"success": True, "data": {"rank": 4, "entry": {"score": 10}}}),
        get_arena=AsyncMock(return_value={"success": True, "data": {"heroes": [{"slot": 1, "id": 50009, "lv": 60}]}}),
    )
    player = {"name": "Moonberry", "playerId": "277720981", "playerUid": "85826384", "level": 30, "kingdom": 7,
              "power": 940162, "allianceTag": "AQM", "profilePhoto": "1234"}
    tracking = SimpleNamespace(track_player_lookup=AsyncMock(), sync_player_metadata=AsyncMock())
    handler = PlayerInfoHandler(_PlayerInfo(player), object(), tracking, service)
    interaction = _stats_interaction()

    asyncio.run(handler._handle_player_stats_slash(interaction, "277720981"))

    assert render_threads and all(thread != loop_thread for thread in render_threads)
    service.get_player.assert_awaited_once_with("85826384")
    service.get_player_by_fid.assert_not_awaited()
    service.get_arena.assert_awaited_once_with("85826384")
    sent = interaction.followup.send.call_args.kwargs
    embed = sent["embed"]
    fields = {field.name: field.value for field in embed.fields}
    assert sent["file"].filename == "player_card.png"
    assert embed.image.url == "attachment://player_card.png"
    assert embed.thumbnail.url is None  # non-URL avatar ids are not sent to Discord
    assert fields["Governor Gear"] == "1/6 slots equipped · 3 charms · avg charm Lv 1.7"
    assert "Arena" not in fields
    assert fields["Mystic Trial"] == "Kingdom Rank: #4\nScore: 10"


def test_gear_summary_covers_hidden_unknown_and_missing_profiles():
    summary = PlayerInfoHandler._format_gear_summary

    assert summary(None, 0) == "Unavailable"
    assert summary({"name": "No gear key"}, 0) == "Unavailable"
    assert summary({"gear": []}, 0) == "Equipment hidden"
    assert summary({"gear": [{"slot": 1, "equipid": 0, "charms": []}]}, 0) == "No equipment equipped"
    unknown = summary({"gear": [{"slot": 1, "equipid": 9999, "charms": []}]}, 0)
    assert unknown.startswith("1/6 slots equipped · 0 charms") and "icons unavailable" in unknown


def test_scout_options_use_ranges_and_reject_bad_values_privately():
    from discord.ext import commands

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    handler = PlayerInfoHandler(object(), bot, object(), SimpleNamespace())
    handler.register_commands()
    command = bot.tree.get_command("scout")
    kingdom = command.get_parameter("kingdom_number")
    limit = command.get_parameter("limit")
    assert (kingdom.min_value, kingdom.max_value) == (1, 99999)
    assert (limit.min_value, limit.max_value, limit.default) == (1, 15, 5)

    for kingdom_number, scout_limit in ((0, 5), (100000, 5), (830, 0)):
        interaction = _scout_interaction()
        asyncio.run(handler._handle_scout_slash(interaction, kingdom_number, scout_limit))
        interaction.response.defer.assert_not_awaited()
        assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True
