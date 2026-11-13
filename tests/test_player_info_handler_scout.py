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


def _scout_interaction(guild_id=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=1, name="Tester", discriminator="0"),
        guild=SimpleNamespace(id=guild_id) if guild_id else None,
        guild_id=guild_id,
        response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))),
        edit_original_response=AsyncMock(),
        delete_original_response=AsyncMock(),
    )


def _settings(kingdom):
    return SimpleNamespace(get_settings=AsyncMock(return_value=SimpleNamespace(kingdom_id=kingdom)))


def test_scout_posts_one_card_of_the_top_five_without_controls():
    async def check():
        # The API may return more rows than asked for: the report still has five.
        entries = [
            {"rank": index + 1, "uid": index + 200, "fid": index + 100, "name": f"Board {index}", "score": 3000 - index,
             "alliance": {"aid": 1, "abbr": "KOR", "name": "Serendipity"}}
            for index in range(20)
        ]
        active, peak = 0, 0

        async def get_player(uid):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            index = int(uid) - 200
            return {"success": True, "data": {
                "uid": int(uid), "fid": index + 100, "name": f"Player {index}", "power": 773_164_098, "stove_lv": 70,
                "kid": 830, "alliance": {"aid": 2, "abbr": "NEW", "name": "Moved"}, "rank": 99, "score": 999999,
            }}

        service = SimpleNamespace(
            get_kingdom_board=AsyncMock(return_value={"success": True, "data": {"entries": entries}}),
            get_player=AsyncMock(side_effect=get_player),
            get_player_by_fid=AsyncMock(),
        )
        handler = PlayerInfoHandler(object(), object(), object(), service)
        interaction = _scout_interaction()
        await handler._handle_scout_slash(interaction, 830)

        service.get_kingdom_board.assert_awaited_once_with(20, 830, limit=5, resolve=True)
        # One profile per shown player, all at once; nothing for the rows beyond the fifth.
        assert service.get_player.await_count == 5 and peak == 5
        service.get_player_by_fid.assert_not_awaited()
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        interaction.followup.send.assert_awaited_once()
        sent = interaction.followup.send.call_args.kwargs
        assert "view" not in sent
        assert [file.filename for file in sent["files"]] == ["scout.png"]
        assert sent["files"][0].fp.read(8) == b"\x89PNG\r\n\x1a\n"
        embed = sent["embed"]
        assert embed.title == "🔎 Scout Report · Kingdom 830"
        assert embed.description == "**Mystic Trial · top 5**"
        assert embed.image.url == "attachment://scout.png"
        assert embed.fields[0].name == "Governor IDs"
        assert embed.fields[0].value == "#1 `100` · #2 `101` · #3 `102` · #4 `103` · #5 `104`"
        assert "/player" in embed.footer.text and "/leaderboard" in embed.footer.text

    asyncio.run(check())


def test_scout_rows_take_rank_and_score_from_the_board_and_the_rest_from_the_profile():
    entry = {"rank": 1, "uid": 28584855, "fid": 117248174, "name": "Old Name", "score": 2846,
             "alliance": {"abbr": "OLD", "name": "Old Alliance"}}
    profile = {"uid": 28584855, "fid": 117248174, "name": "Resolved Player", "power": 773_164_098, "stove_lv": 70,
               "alliance": {"abbr": "FKA", "name": "FateKillsAll"}, "rank": 99, "score": 999999}

    player = PlayerInfoHandler._scout_player(entry, profile, 0, 830)
    row = player["row"]
    assert (row.rank, row.name, row.tag) == (1, "Resolved Player", "FKA")
    assert (row.town_center, row.power, row.score, row.value) == ("TG8", "773.16M", "2,846", 2846.0)
    assert player["player_id"] == "117248174"

    # Without a profile the board entry still gives a full row, minus the two columns.
    bare = PlayerInfoHandler._scout_player(entry, None, 0, 830)["row"]
    assert (bare.name, bare.tag, bare.town_center, bare.power, bare.score) == ("Old Name", "OLD", "", "", "2,846")
    # Nothing known at all: a labeled row at its position on the board.
    unknown = PlayerInfoHandler._scout_player({"uid": 5}, None, 3, 830)
    assert (unknown["row"].rank, unknown["row"].name, unknown["row"].score) == (4, "Unknown player", "")
    assert unknown["player_id"] is None
    # Hidden or zero values are not printed as "0".
    hidden = PlayerInfoHandler._scout_player(entry, dict(profile, power=0, stove_lv=0), 0, 830)["row"]
    assert (hidden.town_center, hidden.power) == ("", "")
    assert PlayerInfoHandler._scout_player(entry, dict(profile, stove_lv=28), 0, 830)["row"].town_center == "28"


@pytest.mark.parametrize("source", ["uid_only", "fid_only", "embedded"])
def test_scout_looks_a_profile_up_by_uid_then_governor_id_and_reuses_an_embedded_one(source):
    async def check():
        profile = {"uid": 28584855, "fid": 117248174, "name": "Resolved Player", "power": 5, "kid": 830}
        entry = {"rank": 1, "uid": 28584855, "score": 2846, "fid": None}
        if source == "fid_only":
            entry.pop("uid")
            entry["fid"] = 117248174
        elif source == "embedded":
            entry["player"] = profile
        service = SimpleNamespace(
            get_player=AsyncMock(return_value={"success": True, "data": profile}),
            get_player_by_fid=AsyncMock(return_value={"success": True, "data": profile}),
        )
        handler = PlayerInfoHandler(object(), object(), object(), service)
        assert await handler._scout_profiles([entry]) == [profile]
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


def test_scout_is_not_held_up_by_failed_or_slow_profiles():
    async def check():
        entries = [{"rank": index + 1, "uid": index + 200, "name": f"Board {index}", "score": 10 - index}
                   for index in range(3)]

        async def get_player(uid):
            if uid == "200":
                return {"success": False, "error_message": "Temporarily unavailable"}
            if uid == "201":
                raise RuntimeError("boom")
            await asyncio.sleep(60)

        service = SimpleNamespace(
            get_kingdom_board=AsyncMock(return_value={"success": True, "data": {"entries": entries}}),
            get_player=AsyncMock(side_effect=get_player),
            get_player_by_fid=AsyncMock(),
        )
        handler = PlayerInfoHandler(object(), object(), object(), service)
        handler.SCOUT_PROFILE_SECONDS = 0.05
        assert await handler._scout_profiles(entries) == [None, None, None]

        interaction = _scout_interaction()
        await handler._handle_scout_slash(interaction, 830)
        sent = interaction.followup.send.call_args.kwargs
        assert [file.filename for file in sent["files"]] == ["scout.png"]
        # No Governor ID is known, so the field is left out.
        assert not sent["embed"].fields

    asyncio.run(check())


def test_scout_text_fallback_lists_the_same_columns(monkeypatch):
    async def check():
        entries = [{"rank": 1, "uid": 200, "fid": 100, "name": "Mila_*", "score": 2846,
                    "alliance": {"abbr": "AQM"}, "player": {"fid": 100, "name": "Mila_*", "power": 1_500_000,
                                                             "stove_lv": 35, "alliance": {"abbr": "AQM"}}},
                   {"rank": 2, "uid": 201, "name": "Bare", "score": 12, "player": {"error": "uid not found"}}]
        service = SimpleNamespace(
            get_kingdom_board=AsyncMock(return_value={"success": True, "data": {"entries": entries}}),
            get_player=AsyncMock(return_value={"success": False}),
            get_player_by_fid=AsyncMock(),
        )
        handler = PlayerInfoHandler(object(), object(), object(), service)

        def broken(*args, **kwargs):
            raise RuntimeError("no fonts")

        monkeypatch.setattr("handlers.player_info_handler.ranking_card_renderer.render_scout_card", broken)
        interaction = _scout_interaction()
        await handler._handle_scout_slash(interaction, 830)
        sent = interaction.followup.send.call_args.kwargs
        assert sent["files"] == []
        embed = sent["embed"]
        assert embed.image.url is None
        lines = embed.description.split("\n")
        assert lines[0] == "**Mystic Trial · top 2**"
        assert lines[1] == (
            "**#1** `[AQM]` Mila\\_\\* · Town Center TG1 · Power 1.5M · Trial stages 2,846"
        )
        assert lines[2] == "**#2** Bare · Town Center unavailable · Power unavailable · Trial stages 12"
        assert embed.fields[0].value == "#1 `100`"

    asyncio.run(check())


def test_scout_uses_the_servers_kingdom_and_asks_privately_when_there_is_none():
    async def check():
        board = {"success": True, "data": {"entries": [{"rank": 1, "uid": 200, "name": "Mila", "score": 5}]}}
        service = SimpleNamespace(
            get_kingdom_board=AsyncMock(return_value=board),
            get_player=AsyncMock(return_value={"success": False}),
            get_player_by_fid=AsyncMock(),
        )
        handler = PlayerInfoHandler(object(), object(), object(), service, guild_configuration_service=_settings(795))
        interaction = _scout_interaction(guild_id=10)
        await handler._handle_scout_slash(interaction)
        # The lookup is acknowledged privately; the report itself is a public follow-up.
        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        assert service.get_kingdom_board.await_args.args == (20, 795)
        sent = interaction.followup.send.call_args.kwargs
        assert sent["embed"].title == "🔎 Scout Report · Kingdom 795" and "ephemeral" not in sent
        interaction.edit_original_response.assert_awaited_once_with(content="Scout report ready.")
        interaction.delete_original_response.assert_awaited_once()

        for handler, guild_id, hinted in (
            (PlayerInfoHandler(object(), object(), object(), service, guild_configuration_service=_settings(None)), 10, True),
            (PlayerInfoHandler(object(), object(), object(), service), None, False),
        ):
            service.get_kingdom_board.reset_mock()
            asked = _scout_interaction(guild_id=guild_id)
            await handler._handle_scout_slash(asked)
            asked.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
            embed = asked.edit_original_response.call_args.kwargs["embed"]
            assert embed.title == "Which kingdom?" and "`/scout kingdom:830`" in embed.description
            assert ("`/settings`" in embed.description) is hinted
            asked.followup.send.assert_not_awaited()
            service.get_kingdom_board.assert_not_awaited()

    asyncio.run(check())


def test_scout_reports_board_problems():
    async def check():
        service = SimpleNamespace(
            get_kingdom_board=AsyncMock(return_value={"success": False, "error_message": "gateway down"}),
            get_player=AsyncMock(),
            get_player_by_fid=AsyncMock(),
        )
        handler = PlayerInfoHandler(object(), object(), object(), service, guild_configuration_service=_settings(795))
        public = _scout_interaction()
        await handler._handle_scout_slash(public, 830)
        failed = public.followup.send.call_args.kwargs["embed"]
        assert failed.title == "❌ Could Not Scout Kingdom 830" and failed.description == "gateway down"

        # After a private acknowledgement the problem replaces the private receipt.
        private = _scout_interaction(guild_id=10)
        await handler._handle_scout_slash(private)
        assert private.edit_original_response.call_args.kwargs["embed"].title == "❌ Could Not Scout Kingdom 795"
        private.followup.send.assert_not_awaited()

        service.get_kingdom_board = AsyncMock(return_value={"success": True, "data": {"entries": []}})
        empty = _scout_interaction()
        await handler._handle_scout_slash(empty, 830)
        assert "No Mystic Trial leaderboard entries" in empty.followup.send.call_args.kwargs["embed"].description
        service.get_player.assert_not_awaited()

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


def test_scout_has_one_optional_kingdom_option_and_rejects_bad_values_privately():
    from discord.ext import commands

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    handler = PlayerInfoHandler(object(), bot, object(), SimpleNamespace())
    handler.register_commands()
    command = bot.tree.get_command("scout")
    assert bot.tree.get_command("player") is not None and bot.tree.get_command("stats") is None
    assert [parameter.name for parameter in command.parameters] == ["kingdom"]
    kingdom = command.get_parameter("kingdom")
    assert (kingdom.min_value, kingdom.max_value, kingdom.required) == (1, 99999, False)

    for kingdom_number in (0, 100000):
        interaction = _scout_interaction()
        asyncio.run(handler._handle_scout_slash(interaction, kingdom_number))
        interaction.response.defer.assert_not_awaited()
        assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True

    # Without the Data API there is nothing to scout: said privately, before any lookup.
    unconfigured = PlayerInfoHandler(object(), object(), object(), None)
    interaction = _scout_interaction()
    asyncio.run(unconfigured._handle_scout_slash(interaction, 830))
    interaction.response.defer.assert_not_awaited()
    assert "Not Configured" in interaction.response.send_message.call_args.kwargs["embed"].title
