"""Audit regressions: gift-code Discord UI deadlines, shutdown races and alliance tags."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from db.models import GiftCode, GiftCodeRedemption, RegisteredPlayer
from handlers.gift_code_handler import GiftCodeHandler
from services.gift_redemption_service import FailingPlayer
from tests.test_gift_code_rosters import (
    BOT_ID,
    GUILD,
    MANAGER,
    MEMBER,
    OPERATOR,
    db,  # noqa: F401  (pytest fixture)
    interaction,
    make_handler,
    register,
)


def record_ack_state(target, name, click):
    """Wrap ``target.name`` to record whether ``click`` was acknowledged when it ran."""
    original = getattr(target, name)
    seen = []

    async def wrapper(*args, **kwargs):
        seen.append(click.response.is_done())
        return await original(*args, **kwargs)

    setattr(target, name, wrapper)
    return seen


# ------------------------------------------- acknowledge before database writes


@pytest.mark.asyncio
async def test_roster_toggle_is_acknowledged_before_the_database_write(db):  # noqa: F811
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "401")
    opened = interaction(MEMBER)
    await handler._handle_list_players_slash(opened)
    view = opened.followup.sent[0]["view"]

    click = interaction(MEMBER)
    seen = record_ack_state(handler._player_registry_service, "set_guild_players_enabled", click)
    await view.toggle_button.callback(click)

    # A write slower than 3 s no longer fails the click (and leaves a stale label).
    assert seen == [True]
    assert click.response.deferred == {} and click.response.edits == []
    edited = click.edit_original_response.call_args.kwargs
    assert edited["view"] is view and view.toggle_button.label == "Enable"
    assert await handler._player_registry_service.get_guild_redeem_targets(GUILD) == []

    # A player removed meanwhile is reported privately after the acknowledgement.
    await handler._player_registry_service.remove_player_from_guild(GUILD, "401")
    gone = interaction(MEMBER)
    await view.toggle_button.callback(gone)
    assert gone.followup.sent[0]["content"].startswith("This player is no longer in the roster")
    assert gone.followup.sent[0]["ephemeral"] is True


@pytest.mark.asyncio
async def test_remove_confirmation_is_acknowledged_before_the_database_write(db):  # noqa: F811
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "301")
    owner = interaction(MEMBER)
    await handler._handle_remove_player_slash(owner, "301")

    click = interaction(MEMBER)
    seen = record_ack_state(handler._player_registry_service, "remove_player_from_guild", click)
    await owner.followup.sent[0]["view"].confirm_button.callback(click)

    assert seen == [True]
    assert click.edit_original_response.call_args.kwargs["embed"].title == "✅ Player Removed"
    assert await handler._player_registry_service.list_guild_roster(GUILD) == []


@pytest.mark.asyncio
async def test_disable_failing_players_is_acknowledged_before_the_database_write(db):  # noqa: F811
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "601", "602")
    with Session(db.engine) as session, session.begin():
        for index, code in enumerate(("NEW", "OLD")):
            session.add(GiftCode(id=index + 1, code=code, auto_redeem_status="completed",
                                 created_at_api=datetime(2026, 11, 2 - index, tzinfo=timezone.utc)))
            session.add(GiftCodeRedemption(user_id=BOT_ID, player_id="602", gift_code=code, success=False,
                                           error_code="INVALID_PLAYER_ID"))
    manager = interaction(MANAGER, manage=True)
    await handler._handle_list_players_slash(manager)
    health = interaction(MANAGER, manage=True)
    await manager.followup.sent[0]["view"].health_button.callback(health)
    health_view = health.followup.sent[0]["view"]

    click = interaction(MANAGER, manage=True)
    seen = record_ack_state(handler._player_registry_service, "set_guild_players_enabled", click)
    await health_view.disable_button.callback(click)

    assert seen == [True]
    assert click.edit_original_response.call_args.kwargs["view"] is health_view
    assert health_view.disable_button.disabled
    assert click.followup.sent[0]["embed"].title == "✅ Players Disabled"
    assert [p.player_id for p in await handler._player_registry_service.get_guild_redeem_targets(GUILD)] == ["601"]


@pytest.mark.asyncio
async def test_loading_the_unassigned_list_is_acknowledged_first(db):  # noqa: F811
    with Session(db.engine) as session, session.begin():
        session.add(RegisteredPlayer(player_id="501", player_name="Legacy", enabled=True, added_by_user_id=MEMBER))
    handler = make_handler(db)
    opened = interaction(OPERATOR)
    await handler._handle_list_players_slash(opened)
    view = opened.followup.sent[0]["view"]

    click = interaction(OPERATOR)
    seen = record_ack_state(handler._player_registry_service, "list_unassigned_players", click)
    view.status_select._values = ["unassigned"]
    await view.status_select.callback(click)

    assert seen == [True]
    assert click.edit_original_response.call_args.kwargs["view"] is view
    assert [entry.player_id for entry in view.filtered_players] == ["501"]

    # Already loaded: plain filter changes still answer by editing in place.
    again = interaction(OPERATOR)
    view.status_select._values = ["all"]
    await view.status_select.callback(again)
    assert again.response.edits and again.response.deferred is None


# --------------------------------------------------------- shutdown mid-command


@pytest.mark.asyncio
async def test_redeem_that_reaches_the_job_start_during_shutdown_starts_nothing(db):  # noqa: F811
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "801")
    registry = handler._player_registry_service
    load_targets = registry.get_guild_redeem_targets

    async def targets_then_shutdown(guild_id):
        targets = await load_targets(guild_id)
        handler._closing = True  # shutdown starts while the roster loads
        return targets

    registry.get_guild_redeem_targets = targets_then_shutdown
    action = interaction(MANAGER, manage=True)
    await handler._handle_redeem_gift_code_slash(action, "LATE")

    assert handler._jobs.get("LATE") is None and handler._jobs.active_jobs() == []
    assert handler._gift_code_service.calls == [] and action.channel.sent == []
    assert "restarting" in action.followup.sent[-1]["content"]


@pytest.mark.asyncio
async def test_submitted_code_reaching_the_probe_during_shutdown_is_not_probed(db):  # noqa: F811
    handler = make_handler(db)
    handler._ensure_pending_worker = lambda: None
    await register(handler, GUILD, MEMBER, "802")
    registry = handler._player_registry_service
    list_owned = registry.list_owned_entries

    async def owned_then_shutdown(guild_id, owner_id):
        entries = await list_owned(guild_id, owner_id)
        handler._closing = True
        return entries

    registry.list_owned_entries = owned_then_shutdown
    action = interaction(MEMBER)
    await handler._handle_submit_code(action, "LATECODE")

    assert handler._gift_code_service.calls == [] and handler._jobs.active_jobs() == []
    assert action.followup.sent[0]["embed"].title == "⏳ Restarting"
    assert await handler._gift_code_service.get_gift_code("LATECODE") is None


# ------------------------------------------------------------- alliance tags


def test_a_typed_numeric_three_character_tag_selects_that_alliance():
    alliances = [
        {"aid": "83900009", "abbr": "777", "power": 200, "rank": 1},
        {"aid": "83900004", "abbr": "FKA", "power": 100, "rank": 2},
    ]
    assert GiftCodeHandler._find_alliance_selection(alliances, "777")["aid"] == "83900009"
    assert GiftCodeHandler._find_alliance_selection(alliances, "fka")["aid"] == "83900004"
    # Hidden autocomplete values (alliance IDs) still resolve by ID.
    assert GiftCodeHandler._find_alliance_selection(alliances, "83900004")["abbr"] == "FKA"
    assert GiftCodeHandler._find_alliance_selection(alliances, "123") is None
    assert GiftCodeHandler._find_alliance_selection(alliances, "0") is None


# ------------------------------------------------------------- auto-pause report


@pytest.mark.asyncio
async def test_operators_hear_about_paused_accounts_when_server_settings_fail_to_load(db):  # noqa: F811
    handler = make_handler(db)
    handler._guild_configuration_service.list_all = AsyncMock(side_effect=RuntimeError("database down"))
    operator_dm = SimpleNamespace(send=AsyncMock())
    handler._bot.get_user = lambda user_id: operator_dm if user_id == OPERATOR else None
    failing = [FailingPlayer("951", "Gov 951", 3, "KINGDOM_MISMATCH")]

    await handler._notify_disabled_accounts(failing, {"951": {GUILD}})

    report = operator_dm.send.await_args.kwargs["embed"]
    assert "`951`" in report.description
