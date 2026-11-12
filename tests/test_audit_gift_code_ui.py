"""Audit regressions: gift-code Discord UI deadlines, shutdown races and alliance tags."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from db.models import GiftCode, GiftCodeRedemption
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
)

KINGDOM = 830


def kingdom_handler(db):  # noqa: F811
    """A handler whose server follows ``KINGDOM``: its roster lists that kingdom's accounts."""
    handler = make_handler(db)
    handler._guild_configuration_service.get_settings = AsyncMock(
        return_value=SimpleNamespace(guild_id=GUILD, manager_role_id=None, kingdom_id=KINGDOM)
    )
    return handler


async def register(handler, owner, *player_ids):
    return await handler._player_registry_service.register_players(
        [{"player_id": pid, "player_name": f"Gov {pid}", "kingdom": str(KINGDOM)} for pid in player_ids],
        added_by_user_id=owner,
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


# ------------------------------------------- acknowledge before database access


@pytest.mark.asyncio
async def test_roster_toggle_is_acknowledged_before_the_database_write(db):  # noqa: F811
    handler = kingdom_handler(db)
    registry = handler._player_registry_service
    await register(handler, MEMBER, "401")
    opened = interaction(MEMBER)
    await handler._handle_list_players_slash(opened)
    view = opened.followup.sent[0]["view"]

    click = interaction(MEMBER)
    looked_up = record_ack_state(registry, "get_entry", click)
    written = record_ack_state(registry, "set_players_enabled", click)
    await view.toggle_button.callback(click)

    # A write slower than 3 s no longer fails the click (and leaves a stale label).
    assert looked_up == [True] and written == [True]
    assert click.response.deferred == {} and click.response.edits == []
    edited = click.edit_original_response.call_args.kwargs
    assert edited["view"] is view and view.toggle_button.label == "Enable"
    assert await registry.get_auto_redeem_targets() == []

    # A player removed meanwhile is reported privately after the acknowledgement,
    # and nothing is written for them.
    await registry.remove_player("401")
    gone = interaction(MEMBER)
    await view.toggle_button.callback(gone)
    assert gone.response.deferred == {}
    assert gone.followup.sent[0]["content"].startswith("This player is no longer on the gift-code list")
    assert gone.followup.sent[0]["ephemeral"] is True
    assert len(written) == 1 and gone.edit_original_response.call_count == 0


@pytest.mark.asyncio
async def test_remove_confirmation_is_acknowledged_before_the_database_write(db):  # noqa: F811
    handler = kingdom_handler(db)
    registry = handler._player_registry_service
    await register(handler, MEMBER, "301")
    owner = interaction(MEMBER)
    await handler._handle_remove_player_slash(owner, "301")

    click = interaction(MEMBER)
    seen = record_ack_state(registry, "remove_player", click)
    await owner.followup.sent[0]["view"].confirm_button.callback(click)

    assert seen == [True]
    assert click.response.deferred == {}
    assert click.edit_original_response.call_args.kwargs["embed"].title == "✅ Player Removed"
    assert await registry.get_entry("301") is None
    assert await registry.list_kingdom_roster(KINGDOM) == []


@pytest.mark.asyncio
async def test_disable_failing_players_is_acknowledged_before_the_database_write(db):  # noqa: F811
    handler = kingdom_handler(db)
    registry = handler._player_registry_service
    await register(handler, MEMBER, "601", "602")
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
    assert [player.player_id for player in health_view.failing] == ["602"]

    click = interaction(MANAGER, manage=True)
    seen = record_ack_state(registry, "set_players_enabled", click)
    await health_view.disable_button.callback(click)

    assert seen == [True]
    assert click.response.deferred == {}
    assert click.edit_original_response.call_args.kwargs["view"] is health_view
    assert health_view.disable_button.disabled
    done = click.followup.sent[0]["embed"]
    assert done.title == "✅ Players Disabled"
    assert done.description.startswith("Disabled 1 player(s) on the gift-code list.")
    assert [p.player_id for p in await registry.get_auto_redeem_targets()] == ["601"]


@pytest.mark.asyncio
async def test_loading_the_roster_health_is_acknowledged_first(db):  # noqa: F811
    handler = kingdom_handler(db)
    registry = handler._player_registry_service
    await register(handler, MEMBER, "701")
    manager = interaction(MANAGER, manage=True)
    await handler._handle_list_players_slash(manager)
    view = manager.followup.sent[0]["view"]

    # The report reads the kingdom's whole roster and its redemption history.
    click = interaction(MANAGER, manage=True)
    seen = record_ack_state(registry, "list_kingdom_roster", click)
    await view.health_button.callback(click)

    assert seen == [True]
    assert click.response.deferred == {"thinking": True, "ephemeral": True}
    assert click.followup.sent[0]["embed"] is not None and click.followup.sent[0]["ephemeral"] is True


@pytest.mark.asyncio
async def test_status_filter_answers_in_place_without_touching_the_database(db):  # noqa: F811
    handler = kingdom_handler(db)
    registry = handler._player_registry_service
    await register(handler, MEMBER, "501", "502")
    await registry.set_players_enabled(["502"], False)
    opened = interaction(OPERATOR)
    await handler._handle_list_players_slash(opened)
    view = opened.followup.sent[0]["view"]

    # Every filter works on the rows the view already holds; an operator gets no
    # filter that would load other rows (the old "unassigned" list did, behind a deferral).
    assert [option.value for option in view.status_select.options] == ["all", "enabled", "disabled"]
    registry.list_kingdom_roster = AsyncMock(side_effect=AssertionError("a filter change reloaded the roster"))

    click = interaction(OPERATOR)
    view.status_select._values = ["disabled"]
    await view.status_select.callback(click)

    assert click.response.deferred is None and click.edit_original_response.call_count == 0
    assert click.response.edits[0]["view"] is view
    assert [entry.player_id for entry in view.filtered_players] == ["502"]


# --------------------------------------------------------- shutdown mid-command


@pytest.mark.asyncio
async def test_redeem_that_reaches_the_job_start_during_shutdown_starts_nothing(db):  # noqa: F811
    handler = kingdom_handler(db)
    await register(handler, MEMBER, "801")
    registry = handler._player_registry_service
    load_targets = registry.get_auto_redeem_targets

    async def targets_then_shutdown(*args, **kwargs):
        targets = await load_targets(*args, **kwargs)
        handler._closing = True  # shutdown starts while the list loads
        return targets

    registry.get_auto_redeem_targets = targets_then_shutdown
    action = interaction(MANAGER, manage=True)
    await handler._handle_redeem_gift_code_slash(action, "LATE")

    assert handler._jobs.get("LATE") is None and handler._jobs.active_jobs() == []
    assert handler._gift_code_service.calls == [] and action.channel.sent == []
    assert "restarting" in action.followup.sent[-1]["content"]


@pytest.mark.asyncio
async def test_submitted_code_reaching_the_probe_during_shutdown_is_not_probed(db):  # noqa: F811
    handler = kingdom_handler(db)
    handler._ensure_pending_worker = lambda: None
    await register(handler, MEMBER, "802")
    registry = handler._player_registry_service
    list_owned = registry.list_owned_entries
    owners = []

    async def owned_then_shutdown(owner_id):
        owners.append(owner_id)
        entries = await list_owned(owner_id)
        handler._closing = True
        return entries

    registry.list_owned_entries = owned_then_shutdown
    action = interaction(MEMBER)
    await handler._handle_submit_code(action, "LATECODE")

    # The submitter's own accounts are looked up on the shared list, not per server.
    assert owners == [MEMBER]
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

    await handler._notify_disabled_accounts(failing, {"951": str(KINGDOM)})

    report = operator_dm.send.await_args.kwargs["embed"]
    assert "`951`" in report.description
