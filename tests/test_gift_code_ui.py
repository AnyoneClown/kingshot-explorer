from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from handlers.gift_code_handler import (
    GiftCodeHandler,
    MyAccountsView,
    PlayerListPaginationView,
    PlayerSearchModal,
)


def make_handler():
    stats = SimpleNamespace(callback=AsyncMock())
    return GiftCodeHandler(
        gift_code_service=SimpleNamespace(
            get_redeemed_players=AsyncMock(return_value=set()),
            redeem_gift_code_remote=AsyncMock(return_value={"success": True, "message": "OK"}),
        ),
        player_info_service=SimpleNamespace(),
        bot=SimpleNamespace(tree=SimpleNamespace(get_command=lambda name: stats if name == "player" else None)),
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
async def test_roster_pages_filters_and_searches():
    handler = make_handler()
    view = PlayerListPaginationView(handler, [player(i) for i in range(24)], 123)
    action = interaction()
    assert view.page_count == 3
    assert "Castle: Unavailable" in view.build_embed().description
    # The dead "View player" and the separate "Clear search" buttons are gone.
    assert sorted(item.label for item in view.children if hasattr(item, "label")) == [
        "Disable", "Next", "Previous", "Remove", "Roster health", "Search",
    ]
    await view.next_button.callback(action)
    view.player_select._values = ["2"]
    await view.player_select.callback(action)
    assert view.selected_player.player_id == "12"
    assert view.toggle_button.label == "Disable" and not view.remove_button.disabled

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
    assert view.player_select.disabled and view.toggle_button.disabled and view.remove_button.disabled
    assert "No players match" in view.build_embed().description
    # An empty search shows everyone again.
    modal.query._value = ""
    await modal.on_submit(action)
    assert len(view.filtered_players) == 12
    view.status_select._values = ["enabled"]
    await view.status_select.callback(action)
    view.query = "22"
    view.refresh()
    assert [value.player_id for value in view.filtered_players] == ["22"]


@pytest.mark.asyncio
async def test_roster_remove_asks_first_and_redraws_the_roster_afterwards():
    handler = make_handler()
    entries = [player(2), player(4)]
    view = PlayerListPaginationView(handler, entries, 123, kingdom="830")
    view.message = SimpleNamespace(edit=AsyncMock())
    handler._player_registry_service.remove_player.return_value = SimpleNamespace(removed=True)

    click = interaction()
    click.original_response = AsyncMock()
    await view.remove_button.callback(click)
    prompt = click.response.send_message.call_args.kwargs
    assert prompt["ephemeral"] is True and "Remove" in prompt["embed"].title
    handler._player_registry_service.remove_player.assert_not_awaited()  # nothing happens before the confirm

    confirm = interaction()
    confirm.edit_original_response = AsyncMock()
    await prompt["view"].confirm_button.callback(confirm)
    handler._player_registry_service.remove_player.assert_awaited_once_with("2")
    assert "Removed" in confirm.edit_original_response.call_args.kwargs["embed"].title
    assert [entry.player_id for entry in view.roster] == ["4"]
    view.message.edit.assert_awaited_once()


def account(player_id, *, enabled=True, owner=123):
    return SimpleNamespace(
        player_id=str(player_id), player_name=f"Player {player_id}", enabled=enabled,
        kingdom="830", castle_level=None, added_by_user_id=owner,
    )


def accounts_handler(entries):
    handler = make_handler()
    handler._gift_code_service.get_recent_gift_codes = AsyncMock(return_value=[SimpleNamespace(code="CODE1")])
    handler._gift_code_service.get_redemption_rows = AsyncMock(return_value=[])
    handler._gift_code_service.get_available_gift_codes = AsyncMock(return_value={"success": True, "data": []})
    registry = handler._player_registry_service
    registry.list_owned_entries.return_value = entries
    registry.get_entry.side_effect = lambda player_id: next(
        (entry for entry in entries if entry.player_id == str(player_id)), None
    )
    return handler


@pytest.mark.asyncio
async def test_my_accounts_is_one_private_panel_with_add_pause_and_remove():
    entries = [account(1), account(2, enabled=False)]
    handler = accounts_handler(entries)
    action = interaction()
    await handler._handle_my_accounts(action)
    sent = action.followup.send.call_args.kwargs
    assert sent["ephemeral"] is True
    view = sent["view"]
    assert isinstance(view, MyAccountsView)
    assert [item.label for item in view.children if hasattr(item, "label")] == ["Add account", "Pause", "Remove"]
    # Two accounts: a menu picks the one the buttons act on, and the embed marks it.
    assert [option.value for option in view.account_select.options] == ["1", "2"]
    assert [field.name for field in sent["embed"].fields] == ["▸ Player 1", "Player 2"]
    assert "⏸️ paused" in sent["embed"].fields[1].value

    pick = interaction()
    pick.edit_original_response = AsyncMock()
    view.account_select._values = ["2"]
    await view.account_select.callback(pick)
    assert view.toggle_button.label == "Resume"

    resume = interaction()
    resume.edit_original_response = AsyncMock()
    await view.toggle_button.callback(resume)
    handler._player_registry_service.set_players_enabled.assert_awaited_once_with(["2"], True)
    assert entries[1].enabled is True and view.toggle_button.label == "Pause"
    assert "✅ receiving codes" in resume.edit_original_response.call_args.kwargs["embed"].fields[1].value


@pytest.mark.asyncio
async def test_my_accounts_with_one_or_no_account_has_no_menu():
    handler = accounts_handler([account(1)])
    action = interaction()
    await handler._handle_my_accounts(action)
    view = action.followup.send.call_args.kwargs["view"]
    assert view.account_select not in view.children
    assert view.selected.player_id == "1" and not view.toggle_button.disabled

    handler = accounts_handler([])
    action = interaction()
    await handler._handle_my_accounts(action)
    sent = action.followup.send.call_args.kwargs
    assert "Add account" in sent["embed"].description
    view = sent["view"]
    assert view.toggle_button.disabled and view.remove_button.disabled and not view.add_button.disabled
    await view.add_button.callback(action)
    action.response.send_modal.assert_awaited_once()


@pytest.mark.asyncio
async def test_my_accounts_refuses_someone_elses_account_and_reloads_after_a_removal():
    entries = [account(1), account(2)]
    handler = accounts_handler(entries)
    handler._access = SimpleNamespace(
        is_super_admin=lambda user_id: False, can_manage=AsyncMock(return_value=False)
    )
    view = MyAccountsView(handler, 123, entries)
    view.message = SimpleNamespace(edit=AsyncMock())
    entries[0].added_by_user_id = 999  # no longer theirs
    denied = interaction()
    await view.toggle_button.callback(denied)
    handler._player_registry_service.set_players_enabled.assert_not_awaited()

    handler._player_registry_service.list_owned_entries.return_value = [entries[1]]
    await view.reload()
    assert [entry.player_id for entry in view.entries] == ["2"]
    assert view.account_select not in view.children
    view.message.edit.assert_awaited_once()


@pytest.mark.asyncio
async def test_bulk_reports_cached_results_rate_limit_wait_and_persistence(monkeypatch):
    handler = make_handler()
    handler._gift_code_service.get_redeemed_players.return_value = {"0"}
    handler._gift_code_service.redeem_gift_code_remote.side_effect = [
        {"success": False, "error_code": "RATE_LIMITED", "message": "Too frequent"},
        {"success": True, "message": "OK"},
    ]
    monkeypatch.setattr("services.gift_redemption_service.asyncio.sleep", AsyncMock())
    monkeypatch.setattr("services.gift_redemption_service.random.uniform", lambda *args: 0)
    updates = []

    async def progress(results, phase):
        updates.append((len(results), phase))

    results = await handler._redemption.run_bulk(
        gift_code="CODE", players=[player(0), player(2)], actor_user_id=123,
        guild_id=456, channel_id=789, progress=progress,
    )
    assert updates[0] == (1, "Redeeming players")
    assert any("Rate limited" in phase and "1 waiting" in phase for _, phase in updates)
    assert updates[-1] == (2, "Finishing")
    assert [row["status_category"] for row in results] == ["already_redeemed", "success"]
    assert handler._gift_code_service.redeem_gift_code_remote.await_count == 2
    handler._tracking_service.log_gift_code_redemptions_many.assert_awaited_once()


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
    task = asyncio.create_task(handler._redemption.run_bulk(
        gift_code="CODE", players=[player(0), player(2), player(4)], actor_user_id=123,
        guild_id=456, channel_id=789, progress=progress,
    ))
    try:
        await asyncio.wait_for(finished.wait(), timeout=1)
        assert not task.done()
        assert updates[-1][0] == 2
    finally:
        release.set()
        await task
