from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers.player_info_handler import PlayerInfoHandler
from services.player_info_service import PlayerInfoService, PlayerInfoUnavailableError


@pytest.mark.asyncio
async def test_player_info_uses_modern_governor_profile():
    profile = {
        "uid": 30679898,
        "fid": 123949113,
        "name": "Governor",
        "kid": 830,
        "stove_lv": 30,
        "power": 123456789,
    }
    api = SimpleNamespace(get_player_by_fid=AsyncMock(return_value={"success": True, "data": profile}))
    service = PlayerInfoService(api)

    player = await service.get_player_info("123949113")

    assert player == {
        "name": "Governor",
        "playerId": "123949113",
        "playerUid": "30679898",
        "level": 30,
        "kingdom": 830,
        "profilePhoto": None,
        "_profile": profile,
    }
    api.get_player_by_fid.assert_awaited_once_with("123949113")


@pytest.mark.asyncio
async def test_unknown_governor_is_distinct_from_api_failure():
    api = SimpleNamespace(get_player_by_fid=AsyncMock(return_value={
        "success": True, "data": {"fid": 123949113, "error": "fid not found"},
    }))
    assert await PlayerInfoService(api).get_player_info("123949113") is None

    api.get_player_by_fid.return_value = {"success": False, "error_message": "gateway unavailable"}
    with pytest.raises(PlayerInfoUnavailableError):
        await PlayerInfoService(api).get_player_info("123949113")


@pytest.mark.asyncio
async def test_stats_renders_modern_profile_without_second_profile_request():
    profile = {
        "uid": 30679898,
        "fid": 123949113,
        "name": "Governor",
        "kid": 830,
        "stove_lv": 30,
        "power": 123456789,
        "alliance": {"aid": 83900004, "abbr": None, "name": None},
    }
    api = SimpleNamespace(
        get_player_by_fid=AsyncMock(return_value={"success": True, "data": profile}),
        get_player=AsyncMock(),
        search_leaderboard=AsyncMock(return_value={"success": False}),
        get_arena=AsyncMock(return_value={"success": False}),
    )
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)

    handler = PlayerInfoHandler(
        player_info_service=PlayerInfoService(api),
        bot=object(),
        interaction_tracking_service=SimpleNamespace(track_player_lookup=AsyncMock()),
        kingshot_data_service=api,
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=111, name="tester", discriminator="0", display_name="Tester"),
        guild=None,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=send),
    )

    await handler._handle_player_stats_slash(interaction, "123949113")

    assert len(sent) == 1
    embed = sent[0]["embed"]
    assert embed.title == "📊 Governor"
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Player ID"] == "`123949113`"
    assert fields["Kingdom"] == "830"
    assert fields["Power"] == "123,456,789"
    api.get_player_by_fid.assert_awaited_once_with("123949113")
    api.get_player.assert_not_awaited()


@pytest.mark.asyncio
async def test_stats_accepts_uid_and_tracks_canonical_governor_id():
    profile = {
        "uid": 100404437,
        "fid": 402436144,
        "name": "Chikawa",
        "kid": 830,
        "stove_lv": 30,
        "power": 123456789,
    }
    api = SimpleNamespace(
        get_player_by_fid=AsyncMock(return_value={
            "success": True, "data": {"fid": 100404437, "error": "fid not found"},
        }),
        get_player=AsyncMock(return_value={"success": True, "data": profile}),
        search_leaderboard=AsyncMock(return_value={"success": False}),
        get_arena=AsyncMock(return_value={"success": False}),
    )
    tracking = SimpleNamespace(track_player_lookup=AsyncMock(), sync_player_metadata=AsyncMock())
    send = AsyncMock()
    handler = PlayerInfoHandler(
        player_info_service=PlayerInfoService(api), bot=object(),
        interaction_tracking_service=tracking, kingshot_data_service=api,
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=111, name="tester", discriminator="0", display_name="Tester"),
        guild=None, response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=send),
    )

    await handler._handle_player_stats_slash(interaction, "100404437")

    send.assert_awaited_once()
    embed = send.await_args.kwargs["embed"]
    assert embed.title == "📊 Chikawa"
    assert {field.name: field.value for field in embed.fields}["Player ID"] == "`402436144`"
    api.get_player_by_fid.assert_awaited_once_with("100404437")
    api.get_player.assert_awaited_once_with("100404437")
    api.search_leaderboard.assert_awaited_once_with(20, "100404437", "830")
    api.get_arena.assert_awaited_once_with("100404437")
    assert tracking.track_player_lookup.await_args.kwargs["player_id"] == "402436144"
    assert tracking.track_player_lookup.await_args.kwargs["player_uid"] == "100404437"
    assert tracking.track_player_lookup.await_args.kwargs["success"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("uid_result, expected_title, tracks_failure", [
    ({"success": True, "data": {"uid": 999999999999, "fid": None, "name": None}},
     "❌ Player Not Found", True),
    ({"success": True, "data": {"error": "uid not found"}}, "❌ Player Not Found", True),
    ({"success": False, "error_message": "gateway unavailable"},
     "❌ Player Lookup Unavailable", False),
    ({"success": True, "data": None}, "❌ Player Lookup Unavailable", False),
])
async def test_stats_distinguishes_unknown_uids_from_uid_lookup_failures(
    uid_result, expected_title, tracks_failure,
):
    api = SimpleNamespace(
        get_player_by_fid=AsyncMock(return_value={
            "success": True, "data": {"error": "fid not found"},
        }),
        get_player=AsyncMock(return_value=uid_result),
    )
    tracking = SimpleNamespace(track_player_lookup=AsyncMock())
    send = AsyncMock()
    handler = PlayerInfoHandler(
        player_info_service=PlayerInfoService(api), bot=object(),
        interaction_tracking_service=tracking, kingshot_data_service=api,
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=111, name="tester", discriminator="0", display_name="Tester"),
        guild=None, response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=send),
    )

    await handler._handle_player_stats_slash(interaction, "999999999999")

    send.assert_awaited_once()
    assert send.await_args.kwargs["embed"].title == expected_title
    if tracks_failure:
        assert tracking.track_player_lookup.await_args.kwargs["success"] is False
    else:
        tracking.track_player_lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_stats_reports_gateway_failure_without_calling_player_not_found():
    api = SimpleNamespace(get_player_by_fid=AsyncMock(return_value={
        "success": False, "error_message": "gateway unavailable",
    }))
    tracking = SimpleNamespace(track_player_lookup=AsyncMock())
    send = AsyncMock()
    handler = PlayerInfoHandler(
        player_info_service=PlayerInfoService(api),
        bot=object(),
        interaction_tracking_service=tracking,
        kingshot_data_service=api,
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=111, name="tester", discriminator="0", display_name="Tester"),
        guild=None,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=send),
    )

    await handler._handle_player_stats_slash(interaction, "123949113")

    assert send.await_args.kwargs["embed"].title == "❌ Player Lookup Unavailable"
    tracking.track_player_lookup.assert_not_awaited()
