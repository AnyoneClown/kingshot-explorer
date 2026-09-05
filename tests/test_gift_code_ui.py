from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from handlers.gift_code_handler import (
    GiftCodeHandler,
    GiftCodesView,
    PlayerListPaginationView,
    PlayerSearchModal,
    RedemptionResultsView,
)
from handlers.ui import EmbedColors


def make_handler():
    stats = SimpleNamespace(callback=AsyncMock())
    return GiftCodeHandler(
        gift_code_service=SimpleNamespace(
            get_redeemed_players=AsyncMock(return_value=set()),
            redeem_gift_code_remote=AsyncMock(return_value={"success": True, "message": "OK"}),
        ),
        player_info_service=SimpleNamespace(),
        bot=SimpleNamespace(tree=SimpleNamespace(get_command=lambda name: stats if name == "stats" else None)),
        config=SimpleNamespace(admin_user_ids=[123]),
        interaction_tracking_service=AsyncMock(),
        player_registry_service=AsyncMock(),
    )


def interaction(user_id=123):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(
            edit_message=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock(),
            defer=AsyncMock(), is_done=Mock(return_value=True),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def player(index):
    return SimpleNamespace(
        player_id=str(index), player_name=f"Player {index}", enabled=index % 2 == 0,
        kingdom="830", castle_level=None,
    )


@pytest.mark.asyncio
async def test_player_search_filters_pagination_and_selected_action():
    handler = make_handler()
    view = PlayerListPaginationView(handler, [player(i) for i in range(24)], 123)
    action = interaction()
    assert view.page_count == 3
    assert "Castle: Unavailable" in view.build_embed().description
    await view.next_button.callback(action)
    view.player_select._values = ["2"]
    await view.player_select.callback(action)
    await view.view_player.callback(action)
    handler._bot.tree.get_command("stats").callback.assert_awaited_once_with(action, player_id="12")

    view.status_select._values = ["disabled"]
    await view.status_select.callback(action)
    assert view.current_page == 0
    assert len(view.filtered_players) == 12
    assert all(not value.enabled for value in view.filtered_players)

    modal = PlayerSearchModal(view)
    modal.query._value = "PLAYER 21"
    await modal.on_submit(action)
    assert [value.player_id for value in view.filtered_players] == ["21"]
    assert view.prev_button.disabled and view.next_button.disabled

    modal.query._value = "no-match"
    await modal.on_submit(action)
    assert view.player_select.disabled and view.view_player.disabled
    assert "No players match" in view.build_embed().description
    await view.clear_button.callback(action)
    assert len(view.filtered_players) == 12
    view.status_select._values = ["enabled"]
    await view.status_select.callback(action)
    view.query = "22"
    view.refresh()
    assert [value.player_id for value in view.filtered_players] == ["22"]


@pytest.mark.asyncio
async def test_gift_actions_paginate_and_recheck_admin_authorization():
    handler = make_handler()
    codes = [{"code": f"CODE{i}"} for i in range(25)]
    view = GiftCodesView(handler, codes, 123, can_redeem=True)
    action = interaction()
    await view.next_button.callback(action)
    await view.next_button.callback(action)
    assert view.next_button.disabled
    assert len(view.build_embed().fields) == 5
    view.code_select._values = ["24"]
    await view.code_select.callback(action)
    assert view.selected_index == 24

    # Removing an admin after the card was opened must revoke the button too.
    handler._config.admin_user_ids = []
    await view.redeem_button.callback(action)
    assert action.followup.send.call_args.kwargs["embed"].title == "⛔ Admin Only"
    handler._gift_code_service.redeem_gift_code_remote.assert_not_awaited()
    readonly = GiftCodesView(handler, codes, 123, can_redeem=False)
    assert readonly.redeem_button not in readonly.children
    assert not await view.interaction_check(interaction(456))
    view.message = SimpleNamespace(edit=AsyncMock())
    await view.on_timeout()
    assert all(item.disabled for item in view.children)


@pytest.mark.asyncio
async def test_normal_progress_message_is_throttled_and_replaced_by_summary():
    handler = make_handler()
    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(id=789, send=AsyncMock(return_value=message))
    result = {"player_id": "2", "status_category": handler.STATUS_SUCCESS}

    async def redeem(**kwargs):
        progress = kwargs["progress"]
        await progress([], "Redeeming players")
        await progress([result], "Redeeming players")  # Same phase is throttled.
        await progress([result], "Saving redemption results")
        return [result]

    handler._run_bulk_redemption = redeem
    await handler._run_manual_redemption_job(
        gift_code="CODE", registered_players=[player(2)], actor_user_id=123,
        guild_id=456, channel=channel,
    )
    channel.send.assert_awaited_once()
    assert "0/1 players" in channel.send.call_args.kwargs["embed"].description
    assert message.edit.await_count == 3
    final = message.edit.call_args.kwargs
    assert final["embed"].title == "✅ All Gift Codes Redeemed Successfully!"
    assert final["view"] is None


@pytest.mark.asyncio
async def test_bulk_reports_cached_results_rate_limit_wait_and_persistence(monkeypatch):
    handler = make_handler()
    handler._gift_code_service.get_redeemed_players.return_value = {"0"}
    handler._gift_code_service.redeem_gift_code_remote.side_effect = [
        {"success": False, "error_code": "RATE_LIMITED", "message": "Too frequent"},
        {"success": True, "message": "OK"},
    ]
    monkeypatch.setattr("handlers.gift_code_handler.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("handlers.gift_code_handler.random.uniform", lambda *args: 0)
    updates = []

    async def progress(results, phase):
        updates.append((len(results), phase))

    results = await handler._run_bulk_redemption(
        gift_code="CODE", registered_players=[player(0), player(2)], actor_user_id=123,
        guild_id=456, channel_id=789, progress=progress,
    )
    assert updates[0] == (1, "Redeeming players")
    assert any("Rate limited" in phase and "1 waiting" in phase for _, phase in updates)
    assert updates[-1] == (2, "Saving redemption results")
    assert [row["status_category"] for row in results] == ["already_redeemed", "success"]
    assert handler._gift_code_service.redeem_gift_code_remote.await_count == 2
    handler._tracking_service.log_gift_code_redemptions_many.assert_awaited_once()


@pytest.mark.asyncio
async def test_already_claimed_is_success_and_details_cover_every_issue():
    handler = make_handler()
    channel = SimpleNamespace(send=AsyncMock())
    results = [{"status_category": "already_redeemed"}] * 10
    await handler._send_redemption_results_to_channel(channel, 123, "CODE", results)
    summary = channel.send.call_args.kwargs
    assert "Everyone Has Already Claimed" in summary["embed"].title
    assert summary["embed"].color == EmbedColors.SUCCESS
    assert summary["view"] is None

    issues = [
        {"player_id": str(i), "status_category": "skipped" if i == 0 else "api_rejected",
         "player_name": "x" * 400, "message": "reason " * 300}
        for i in range(12)
    ]
    await handler._send_redemption_results_to_channel(channel, 123, "CODE", results + issues)
    view = channel.send.call_args.kwargs["view"]
    assert isinstance(view, RedemptionResultsView)
    assert len(view.issues) == 12
    action = interaction()
    await view.details_button.callback(action)
    names = []
    for page in range(3):
        embed = view.build_embed()
        assert len(embed) < 6000
        assert all(len(field.name) <= 256 and len(field.value) <= 1024 for field in embed.fields)
        names.extend(field.name for field in embed.fields)
        if page < 2:
            await view.next_button.callback(action)
    assert len(names) == 12
    assert view.next_button.disabled
    await view.details_button.callback(action)
    assert view.build_embed() is view.summary


@pytest.mark.asyncio
async def test_expired_player_list_cannot_be_reopened_by_search_modal():
    view = PlayerListPaginationView(make_handler(), [player(2)], 123)
    modal = PlayerSearchModal(view)
    view.stop()
    action = interaction()
    await modal.on_submit(action)
    action.response.edit_message.assert_not_awaited()
    assert "expired" in action.response.send_message.call_args.args[0]


@pytest.mark.asyncio
async def test_progress_reports_finished_players_while_a_batch_peer_waits():
    import asyncio

    handler = make_handler()
    waiting = asyncio.Event()
    finished = asyncio.Event()
    release = asyncio.Event()
    updates = []

    async def redeem(player_id, gift_code, kingdom_id=None):
        if player_id == 2:
            waiting.set()
            await release.wait()
        if player_id == 4:
            await waiting.wait()
        return {"success": True, "message": "OK"}

    async def progress(results, phase):
        updates.append((len(results), phase))
        if len(results) == 2:
            finished.set()

    handler._gift_code_service.redeem_gift_code_remote.side_effect = redeem
    task = asyncio.create_task(handler._run_bulk_redemption(
        gift_code="CODE", registered_players=[player(0), player(2), player(4)], actor_user_id=123,
        guild_id=456, channel_id=789, progress=progress,
    ))
    try:
        await asyncio.wait_for(finished.wait(), timeout=1)
        assert not task.done()
        assert updates[-1][0] == 2
    finally:
        release.set()
        await task
