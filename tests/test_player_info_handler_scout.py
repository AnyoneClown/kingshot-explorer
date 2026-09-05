import asyncio
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from handlers.player_info_handler import PlayerInfoHandler


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
        hero_file = discord.File(BytesIO(b"image"), filename="arena_loadout.png")
        handler._get_arena_loadout_image = AsyncMock(side_effect=[hero_file, None])
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
        await view.view_player_button.callback(interaction)
        first = interaction.edit_original_response.call_args.kwargs
        assert first["attachments"] == [hero_file]
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
        view.player_select._values = ["14"]
        await view.player_select.callback(interaction)
        assert view.current_player == 14 and view.next_button.disabled
        assert view.player_select.options[14].default
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow_arena(player_data, profile):
            entered.set()
            await release.wait()
            return None

        handler._get_arena_loadout_image = slow_arena
        pending = asyncio.create_task(view.view_player_button.callback(interaction))
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

    asyncio.run(check())


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
        handler._get_arena_loadout_image = AsyncMock(return_value=None)
        interaction = _scout_interaction()
        await handler._handle_scout_slash(interaction, 830, 5)
        view = interaction.followup.send.call_args.kwargs["view"]
        assert view.entries[0]["player"] == profile
        assert view.entries[0]["rank"] == 1 and view.entries[0]["score"] == 2846
        assert "**#1 Resolved Player** · 2,846" in view.build_summary().description
        assert "999,999" not in view.build_summary().description
        assert "Resolved Player" in view.player_select.options[0].label
        assert "117248174" in view.player_select.options[0].description
        await view.view_player_button.callback(interaction)
        embed = interaction.edit_original_response.call_args.kwargs["embed"]
        fields = {field.name: field.value for field in embed.fields}
        assert embed.title == "📊 Resolved Player"
        assert fields["Player ID"] == "`117248174`"
        assert fields["Power"] == "0" and fields["Castle Level"] == "0"
        assert fields["Alliance"] == "`[FKA]` FateKillsAll"
        assert fields["Mystic Trial"] == "Kingdom Rank: #1\nScore: 2,846"
        await view.summary_button.callback(interaction)
        await view.view_player_button.callback(interaction)
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
        handler.HERO_IMAGE_DIR = tmp_path
        (tmp_path / "50024.png").write_bytes(b"placeholder")
        handler._build_hero_loadout_png = lambda _: b"arena image"
        interaction = _scout_interaction()
        await handler._handle_scout_slash(interaction, 830, 5)
        view = interaction.followup.send.call_args.kwargs["view"]
        assert "2,846" in view.build_summary().description
        assert "Unavailable" in view.player_select.options[0].description
        await view.view_player_button.callback(interaction)
        sent = interaction.edit_original_response.call_args.kwargs
        fields = {field.name: field.value for field in sent["embed"].fields}
        assert fields["Player ID"] == "`Unavailable`"
        assert fields["Power"] == "Unavailable"
        assert fields["Mystic Trial"] == "Kingdom Rank: #1\nScore: 2,846"
        assert sent["attachments"][0].filename == "arena_loadout.png"
        service.get_arena.assert_awaited_once_with(str(uid))
        service.get_player_by_fid.assert_not_awaited()
        assert service.get_player.await_count >= 1
        assert all(call.args == (str(uid),) for call in service.get_player.await_args_list)

    asyncio.run(check())
