"""Per-server rosters, permissions, resumable auto-redemption and job guards via the handler."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import GiftCode, GiftCodeRedemption, RegisteredPlayer, User
from handlers.gift_code_handler import (
    GiftCodeHandler,
    PlayerListPaginationView,
    RemovePlayerConfirmView,
    RosterHealthView,
    bounded_join,
)
from services.gift_code_service import GiftCodeService
from services.gift_redemption_service import RedemptionJob
from services.interaction_tracking_service import InteractionTrackingService
from services.player_info_service import PlayerLookupUnavailable
from services.player_registry_service import PlayerRegistryService
from tests.sqlite_helpers import SqliteDb

GUILD, OTHER_GUILD = 1001, 2002
OPERATOR, MEMBER, OTHER_MEMBER, MANAGER = 900, 1, 2, 3
BOT_ID = 99


class FakeGiftCodeService(GiftCodeService):
    """Real persistence on SQLite, scripted upstream API."""

    def __init__(self, db, responses=None):
        super().__init__(db)
        self.responses = responses or {}
        self.calls = []
        self.completed = []

    async def redeem_gift_code_remote(self, player_id, gift_code, kingdom_id=None):
        self.calls.append((player_id, gift_code))
        response = self.responses.get(player_id, {"success": True, "message": "OK"})
        return await response() if callable(response) else response

    async def mark_gift_code_completed(self, code_id):
        self.completed.append(code_id)
        return await super().mark_gift_code_completed(code_id)

    async def close(self):
        return None


class FakePlayerInfo:
    def __init__(self, missing=(), outage_from=None):
        self.missing = set(missing)
        self.outage_from = outage_from
        self.calls = []

    async def get_player_info(self, player_id):
        self.calls.append(player_id)
        if self.outage_from is not None and player_id == self.outage_from:
            raise PlayerLookupUnavailable("down")
        if player_id in self.missing:
            return None
        return {"playerId": player_id, "name": f"Gov {player_id}", "kingdom": 830, "level": 30}


class Response:
    def __init__(self):
        self.done = False
        self.deferred = None
        self.sent = []
        self.edits = []

    async def defer(self, **kwargs):
        self.done, self.deferred = True, kwargs

    async def send_message(self, content=None, **kwargs):
        self.done = True
        self.sent.append({"content": content, **kwargs})

    async def edit_message(self, **kwargs):
        self.done = True
        self.edits.append(kwargs)

    async def send_modal(self, modal):
        self.done = True

    def is_done(self):
        return self.done


class Followup:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append({"content": content, **kwargs})
        return SimpleNamespace(id=len(self.sent), edit=AsyncMock())


class Channel:
    def __init__(self, channel_id=789, guild_id=GUILD):
        self.id = channel_id
        self.guild = SimpleNamespace(id=guild_id)
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(edit=AsyncMock(), jump_url="https://discord.test/m")


def interaction(user_id, *, guild_id=GUILD, manage=False):
    user = SimpleNamespace(
        id=user_id, name=f"user{user_id}", discriminator="0", display_name=f"User {user_id}",
        guild_permissions=SimpleNamespace(manage_guild=manage, administrator=False), roles=[],
    )
    return SimpleNamespace(
        id=5, created_at=None, user=user, guild_id=guild_id,
        guild=SimpleNamespace(id=guild_id, name="Guild") if guild_id else None,
        response=Response(), followup=Followup(), channel=Channel(),
        edit_original_response=AsyncMock(),
    )


@pytest.fixture
def db():
    database = SqliteDb()
    with Session(database.engine) as session, session.begin():
        session.add_all([User(id=uid, username=f"u{uid}") for uid in (OPERATOR, MEMBER, OTHER_MEMBER, MANAGER, BOT_ID)])
    return database


def make_handler(db, *, info=None, responses=None, guild_settings=(), auto_channels=(), channels=None):
    channels = channels or {}
    bot = SimpleNamespace(
        tree=SimpleNamespace(get_command=lambda name: None),
        user=SimpleNamespace(id=BOT_ID, name="bot", discriminator="0", display_name="Bot"),
        get_channel=lambda channel_id: channels.get(channel_id),
    )
    config = SimpleNamespace(
        admin_user_ids={OPERATOR}, max_players_per_guild=5, max_self_registered_players=2,
        auto_redeem_channels=set(auto_channels),
    )
    guild_config = SimpleNamespace(
        list_all=AsyncMock(return_value=list(guild_settings)),
        get_settings=AsyncMock(return_value=SimpleNamespace(manager_role_id=None)),
    )
    return GiftCodeHandler(
        gift_code_service=FakeGiftCodeService(db, responses),
        player_info_service=info or FakePlayerInfo(),
        bot=bot,
        config=config,
        interaction_tracking_service=InteractionTrackingService(db),
        player_registry_service=PlayerRegistryService(db),
        guild_configuration_service=guild_config,
    )


async def register(handler, guild_id, owner, *player_ids):
    return await handler._player_registry_service.register_players_for_guild(
        guild_id, [{"player_id": pid, "player_name": f"Gov {pid}", "kingdom": "830"} for pid in player_ids],
        added_by_user_id=owner, max_total=100,
    )


def field(embed, prefix):
    return next(f for f in embed.fields if f.name.startswith(prefix))


# ------------------------------------------------------------------ /addplayer


@pytest.mark.asyncio
async def test_member_self_registration_cap_and_lookup_results(db):
    info = FakePlayerInfo(missing={"102"})
    handler = make_handler(db, info=info)
    action = interaction(MEMBER)

    await handler._handle_add_player_slash(action, "101, 102 103 abc")

    assert action.response.deferred["ephemeral"] is True
    assert info.calls == ["101", "102"]  # 103 is over the personal cap: no lookup
    embed = action.followup.sent[0]["embed"]
    assert action.followup.sent[0]["ephemeral"] is True
    assert "`101` (Gov 101)" in field(embed, "Added (1)").value
    assert "`102`" in field(embed, "Not found (1)").value
    over = field(embed, "Not added — limit reached (1)").value
    assert "up to 2 players yourself" in over and "`103`" in over
    assert "`abc`" in field(embed, "Invalid (1)").value
    roster = await handler._player_registry_service.list_guild_roster(GUILD)
    assert [(e.player_id, e.added_by_user_id) for e in roster] == [("101", MEMBER)]
    assert await handler._player_registry_service.list_guild_roster(OTHER_GUILD) == []


@pytest.mark.asyncio
async def test_lookup_outage_is_reported_as_retry_later_not_as_not_found(db):
    info = FakePlayerInfo(outage_from="202")
    handler = make_handler(db, info=info)
    action = interaction(MANAGER, manage=True)

    await handler._handle_add_player_slash(action, "201,202,203")

    embed = action.followup.sent[0]["embed"]
    assert info.calls == ["201", "202"]
    assert not any(f.name.startswith("Not found") for f in embed.fields)
    unchecked = field(embed, "Not checked (2)").value
    assert "unavailable right now" in unchecked and "`203`" in unchecked


@pytest.mark.asyncio
async def test_long_result_lists_are_truncated_within_embed_limits(db):
    ids = [f"{index:025d}" for index in range(1, 61)]
    handler = make_handler(db, info=FakePlayerInfo(missing=set(ids)))
    action = interaction(MANAGER, manage=True)

    await handler._handle_add_player_slash(action, ",".join(ids + [f"bad{i}" * 10 for i in range(40)]))

    embed = action.followup.sent[0]["embed"]
    assert len(embed) < 6000
    assert all(len(f.value) <= 1024 for f in embed.fields)
    not_found = field(embed, "Not found (5)")  # capacity 5 limits lookups
    assert "more" not in not_found.value
    over = field(embed, "Not added — limit reached (45)").value
    assert "…and" in over and over.endswith("more")
    assert field(embed, "Ignored (10)")


@pytest.mark.asyncio
async def test_addplayer_outside_a_server_is_denied(db):
    action = interaction(MEMBER, guild_id=None)
    await make_handler(db)._handle_add_player_slash(action, "101")
    assert action.response.sent[0]["embed"].title == "🔒 Not allowed"
    assert action.response.sent[0]["ephemeral"] is True


# --------------------------------------------------------------- /removeplayer


@pytest.mark.asyncio
async def test_remove_requires_owner_or_manager_confirms_and_only_touches_this_server(db):
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "301", "302")
    await register(handler, OTHER_GUILD, OTHER_MEMBER, "302")

    stranger = interaction(OTHER_MEMBER)
    await handler._handle_remove_player_slash(stranger, "301")
    assert stranger.followup.sent[0]["embed"].title == "🔒 Not allowed"
    assert stranger.followup.sent[0]["ephemeral"] is True

    owner = interaction(MEMBER)
    await handler._handle_remove_player_slash(owner, "301")
    prompt = owner.followup.sent[0]
    assert prompt["ephemeral"] is True and isinstance(prompt["view"], RemovePlayerConfirmView)
    assert len(await handler._player_registry_service.list_guild_roster(GUILD)) == 2  # nothing yet

    click = interaction(MEMBER)
    await prompt["view"].confirm_button.callback(click)
    result = click.response.edits[0]
    assert result["embed"].title == "✅ Player Removed" and result["view"] is None
    assert "profile was deleted" in result["embed"].description

    manager = interaction(MANAGER, manage=True)
    await handler._handle_remove_player_slash(manager, "302")
    click = interaction(MANAGER, manage=True)
    await manager.followup.sent[0]["view"].confirm_button.callback(click)
    assert "profile was deleted" not in click.response.edits[0]["embed"].description
    assert await handler._player_registry_service.list_guild_roster(GUILD) == []
    assert [e.player_id for e in await handler._player_registry_service.list_guild_roster(OTHER_GUILD)] == ["302"]


# ---------------------------------------------------------------- /listplayers


@pytest.mark.asyncio
async def test_roster_toggle_is_limited_to_owners_and_managers(db):
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "401")
    await register(handler, GUILD, OTHER_MEMBER, "402")
    await register(handler, OTHER_GUILD, OTHER_MEMBER, "999")

    action = interaction(MEMBER)
    await handler._handle_list_players_slash(action)
    view = action.followup.sent[0]["view"]
    assert isinstance(view, PlayerListPaginationView)
    assert [e.player_id for e in view.roster] == ["401", "402"]  # this server only
    assert view.health_button not in view.children and view.assign_button not in view.children
    assert [o.value for o in view.status_select.options] == ["all", "enabled", "disabled"]

    assert view.selected_player.player_id == "401" and not view.toggle_button.disabled
    await view.toggle_button.callback(interaction(MEMBER))
    assert view.selected_player.enabled is False and view.toggle_button.label == "Enable"
    assert [p.player_id for p in await handler._player_registry_service.get_guild_redeem_targets(GUILD)] == ["402"]

    view.player_select._values = ["1"]
    await view.player_select.callback(interaction(MEMBER))
    assert view.selected_player.player_id == "402" and view.toggle_button.disabled
    denied = interaction(MEMBER)
    await view.toggle_button.callback(denied)
    assert denied.response.sent[0]["embed"].title == "🔒 Not allowed"
    assert view.selected_player.enabled is True

    manager = interaction(MANAGER, manage=True)
    await handler._handle_list_players_slash(manager)
    manager_view = manager.followup.sent[0]["view"]
    assert manager_view.health_button in manager_view.children
    manager_view.player_select._values = ["1"]
    await manager_view.player_select.callback(interaction(MANAGER, manage=True))
    assert not manager_view.toggle_button.disabled


@pytest.mark.asyncio
async def test_operator_can_filter_and_assign_unassigned_legacy_profiles(db):
    with Session(db.engine) as session, session.begin():
        session.add_all([
            RegisteredPlayer(player_id="501", player_name="Legacy", enabled=True, added_by_user_id=MEMBER),
            RegisteredPlayer(player_id="502", player_name="Lookup", enabled=False, added_by_user_id=MEMBER),
        ])
    handler = make_handler(db)

    member = interaction(MEMBER)
    await handler._handle_list_players_slash(member)
    assert member.followup.sent[0]["embed"].title == "📋 No Players Yet"

    action = interaction(OPERATOR)
    await handler._handle_list_players_slash(action)
    view = action.followup.sent[0]["view"]
    assert "unassigned" in [o.value for o in view.status_select.options]
    view.status_select._values = ["unassigned"]
    await view.status_select.callback(interaction(OPERATOR))
    assert [e.player_id for e in view.filtered_players] == ["501", "502"]
    assert "Unassigned" in view.build_embed().title and not view.assign_button.disabled

    click = interaction(OPERATOR)
    await view.assign_button.callback(click)
    assert "Assigned 1" in click.followup.sent[0]["content"]
    assert [(e.player_id, e.added_by_user_id) for e in view.roster] == [("501", MEMBER)]
    assert [e.player_id for e in view.unassigned] == ["502"]


@pytest.mark.asyncio
async def test_roster_health_reports_reach_and_disables_failing_players(db):
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "601", "602")
    with Session(db.engine) as session, session.begin():
        for index, code in enumerate(("NEW", "OLD")):
            session.add(GiftCode(id=index + 1, code=code, created_at_api=datetime(2026, 11, 2 - index, tzinfo=timezone.utc),
                                 auto_redeem_status="completed"))
            session.add(GiftCodeRedemption(user_id=BOT_ID, player_id="601", gift_code=code, success=True))
            session.add(GiftCodeRedemption(user_id=BOT_ID, player_id="602", gift_code=code, success=False,
                                           error_code="INVALID_PLAYER_ID"))

    manager = interaction(MANAGER, manage=True)
    await handler._handle_list_players_slash(manager)
    view = manager.followup.sent[0]["view"]
    click = interaction(MANAGER, manage=True)
    await view.health_button.callback(click)
    report = click.followup.sent[0]
    assert report["embed"].title == "🩺 Roster Health"
    assert "`NEW` — reached **1**/2 · 1 failed" in report["embed"].description
    assert "`602`" in report["embed"].fields[0].value
    health_view = report["view"]
    assert isinstance(health_view, RosterHealthView) and health_view.disable_button.label == "Disable these (1)"

    await health_view.disable_button.callback(interaction(MANAGER, manage=True))
    assert [p.player_id for p in await handler._player_registry_service.get_guild_redeem_targets(GUILD)] == ["601"]

    member_click = interaction(MEMBER)
    await view.health_button.callback(member_click)
    assert member_click.response.sent[0]["embed"].title == "🔒 Not allowed"


# --------------------------------------------------------------------- /redeem


@pytest.mark.asyncio
async def test_manual_redeem_waits_for_a_running_auto_job_on_the_same_code(db):
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "701")
    handler._jobs.claim(RedemptionJob(gift_code="SUMMER", kind="auto"))

    action = interaction(MANAGER, manage=True)
    await handler._handle_redeem_gift_code_slash(action, "summer")

    embed = action.followup.sent[0]["embed"]
    assert embed.title == "⏳ Redemption Job Already Running"
    assert "Auto-redemption is already processing" in embed.description
    assert handler._gift_code_service.calls == []


@pytest.mark.asyncio
async def test_redeem_is_scoped_to_the_server_and_all_servers_needs_an_operator(db, monkeypatch):
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "801")
    await register(handler, OTHER_GUILD, MEMBER, "802")
    with Session(db.engine) as session, session.begin():
        session.add(RegisteredPlayer(player_id="803", enabled=True, added_by_user_id=MEMBER))  # legacy
    captured = []

    async def fake_job(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(handler, "_run_manual_redemption_job", fake_job)

    member = interaction(MEMBER)
    await handler._handle_redeem_gift_code_slash(member, "CODE")
    assert member.followup.sent[0]["embed"].title == "🔒 Not allowed"

    manager = interaction(MANAGER, manage=True)
    await handler._handle_redeem_gift_code_slash(manager, "CODE", all_servers=True)
    assert "bot operator" in manager.followup.sent[0]["embed"].description

    await handler._handle_redeem_gift_code_slash(manager, "CODE")
    await handler._jobs.get("CODE").task
    assert [p.player_id for p in captured[0]["registered_players"]] == ["801"]

    operator = interaction(OPERATOR)
    await handler._handle_redeem_gift_code_slash(operator, "OTHER", all_servers=True)
    await handler._jobs.get("OTHER").task
    assert [p.player_id for p in captured[1]["registered_players"]] == ["801", "802", "803"]
    assert captured[1]["job"].all_servers is True


@pytest.mark.asyncio
async def test_cancel_button_accepts_requester_and_managers_only(db):
    handler = make_handler(db)
    job = RedemptionJob(gift_code="CODE", kind="manual", guild_id=GUILD, requester_id=MEMBER)
    from handlers.gift_code_handler import RedemptionProgressView

    view = RedemptionProgressView(handler, job)
    stranger = interaction(OTHER_MEMBER)
    assert not await view.interaction_check(stranger)
    assert stranger.response.sent[0]["embed"].title == "🔒 Not allowed"
    assert await view.interaction_check(interaction(MANAGER, manage=True))

    click = interaction(MEMBER)
    assert await view.interaction_check(click)
    await view.cancel_button.callback(click)
    assert job.stop_requested and job.stopped_by == MEMBER
    assert view.cancel_button.disabled


@pytest.mark.asyncio
async def test_cancelled_manual_job_posts_a_cancelled_summary_and_a_ping(db):
    handler = make_handler(db)
    channel = Channel()
    job = RedemptionJob(gift_code="CODE", kind="manual", guild_id=GUILD, requester_id=MEMBER, total=3)
    job.request_stop(user_id=MANAGER)
    await handler._send_redemption_results_to_channel(
        channel, MEMBER, "CODE", [{"player_id": "1", "status_category": "success"}], total=3, job=job,
    )
    summary, ping = channel.sent
    assert summary["embed"].title == "⏹️ Redemption Cancelled"
    assert "**Processed:** 1/3" in summary["embed"].description
    assert f"Cancelled by <@{MANAGER}>. 2 player(s) were not processed" in summary["embed"].fields[0].value
    assert ping["content"].startswith(f"<@{MEMBER}> redemption cancelled for `CODE`")
    assert "https://discord.test/m" in ping["content"]


# --------------------------------------------------------------- auto-redemption


def add_code(db, code_id, code, *, expires_in_days=3, status="pending"):
    with Session(db.engine) as session, session.begin():
        session.add(GiftCode(
            id=code_id, code=code, created_at_api=datetime(2026, 11, 1, code_id, tzinfo=timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(days=expires_in_days), auto_redeem_status=status,
        ))


def pending_codes(db):
    with Session(db.engine) as session:
        return [c.code for c in session.scalars(select(GiftCode).where(GiftCode.auto_redeem_status == "pending"))]


@pytest.mark.asyncio
async def test_poll_resumes_pending_codes_completes_expired_ones_and_skips_busy_codes(db, monkeypatch):
    monkeypatch.setattr("discord.TextChannel", Channel)
    legacy, server_a, server_b = Channel(555, guild_id=None), Channel(777, GUILD), Channel(888, OTHER_GUILD)
    handler = make_handler(
        db,
        responses={902: {"success": False, "error_code": "INVALID_PLAYER_ID", "message": "Player ID does not exist."}},
        guild_settings=[SimpleNamespace(guild_id=GUILD, gift_code_channel_id=777),
                        SimpleNamespace(guild_id=OTHER_GUILD, gift_code_channel_id=888)],
        auto_channels={555},
        channels={555: legacy, 777: server_a, 888: server_b},
    )
    await register(handler, GUILD, MEMBER, "901", "903")
    await register(handler, OTHER_GUILD, MEMBER, "902")
    add_code(db, 1, "RESUME")
    add_code(db, 2, "EXPIRED", expires_in_days=-1)
    add_code(db, 3, "BUSY")
    add_code(db, 4, "DONE", status="completed")
    handler._jobs.claim(RedemptionJob(gift_code="BUSY", kind="manual", guild_id=GUILD))
    # 901 got RESUME before a restart; it must not be requested again.
    with Session(db.engine) as session, session.begin():
        session.add(GiftCodeRedemption(user_id=BOT_ID, player_id="901", gift_code="RESUME", success=True))
    handler._gift_code_service.get_available_gift_codes = AsyncMock(return_value={"success": False, "message": "down"})

    await handler._poll_gift_codes_once()

    assert handler._gift_code_service.calls == [(902, "RESUME"), (903, "RESUME")]
    assert pending_codes(db) == ["BUSY"]
    with Session(db.engine) as session:
        logged = session.scalars(select(GiftCodeRedemption.player_id).where(GiftCodeRedemption.gift_code == "RESUME")).all()
    assert sorted(logged) == ["901", "902", "903"]

    # Global summary for the legacy channel; per-server summaries only where a player succeeded.
    assert len(legacy.sent) == 1 and "**✅ Success:** 2" in legacy.sent[0]["embed"].fields[1].value
    assert len(server_a.sent) == 1
    assert "this server's roster" in server_a.sent[0]["embed"].description
    assert "**👥 Total players:** 2" in server_a.sent[0]["embed"].fields[1].value
    assert server_b.sent == []  # its only player failed


@pytest.mark.asyncio
async def test_new_codes_are_discovered_as_pending_and_redeemed(db):
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "911")
    handler._gift_code_service.get_available_gift_codes = AsyncMock(return_value={"success": True, "data": [
        {"id": 50, "code": "FRESH", "createdAt": "2026-11-05T10:00:00Z", "expiresAt": "2099-01-01T00:00:00Z"},
        {"id": 51, "code": "BROKEN", "createdAt": "not-a-date"},
    ]})

    await handler._poll_gift_codes_once()

    assert handler._gift_code_service.calls == [(911, "FRESH")]
    assert pending_codes(db) == []


@pytest.mark.asyncio
async def test_shutdown_keeps_interrupted_auto_code_pending_with_results_saved(db):
    release = asyncio.Event()
    started = asyncio.Event()

    async def blocked():
        started.set()
        await release.wait()
        return {"success": True, "message": "OK"}

    handler = make_handler(db, responses={922: blocked})
    handler.SHUTDOWN_GRACE_SECONDS = 0.05
    await register(handler, GUILD, MEMBER, "921", "922", "923")
    add_code(db, 1, "LONG")
    code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]

    run = asyncio.create_task(handler._run_auto_redemption(code_row))
    await asyncio.wait_for(started.wait(), timeout=1)
    await handler.shutdown()

    assert await asyncio.wait_for(run, timeout=1) is False
    assert pending_codes(db) == ["LONG"]
    assert handler._gift_code_service.completed == []
    with Session(db.engine) as session:
        # The probe and the finished batch peer are saved; the hung player is retried later.
        assert sorted(session.scalars(select(GiftCodeRedemption.player_id)).all()) == ["921", "923"]
    assert handler._jobs.get("LONG") is None
    calls_before = list(handler._gift_code_service.calls)
    await handler._poll_gift_codes_once()  # closing: no new work
    assert handler._gift_code_service.calls == calls_before


def test_shutdown_cleanup_is_registered_with_the_bot(db):
    names = []
    bot = SimpleNamespace(add_cleanup=lambda name, callback: names.append(name))
    GiftCodeHandler(FakeGiftCodeService(db), FakePlayerInfo(), bot, SimpleNamespace(admin_user_ids=set()))
    assert names == ["gift-code API client", "gift-code redemption jobs"]


def test_bounded_join_marks_the_rest():
    assert bounded_join([]) == "None"
    assert bounded_join(["a", "b"], separator=", ") == "a, b"
    joined = bounded_join([f"item-{i:03d}" for i in range(200)], limit=100)
    assert len(joined) <= 100 and joined.endswith("more") and "…and" in joined
    assert len(bounded_join(["x" * 5000], limit=50)) <= 50
    assert discord  # module used for the TextChannel patch target


# ------------------------------------------------------------- review fixes


@pytest.mark.asyncio
async def test_manual_redeem_rechecks_the_guard_after_loading_targets(db, monkeypatch):
    """The auto poller can claim the code while /redeem awaits the database."""
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "711")
    original_targets = handler._player_registry_service.get_guild_redeem_targets

    async def targets_while_auto_claims(guild_id):
        handler._jobs.claim(RedemptionJob(gift_code="RACE", kind="auto"))
        return await original_targets(guild_id)

    monkeypatch.setattr(handler._player_registry_service, "get_guild_redeem_targets", targets_while_auto_claims)
    started = []
    monkeypatch.setattr(handler, "_run_manual_redemption_job", lambda **kwargs: started.append(kwargs))

    action = interaction(MANAGER, manage=True)
    await handler._handle_redeem_gift_code_slash(action, "RACE")

    assert action.followup.sent[-1]["embed"].title == "⏳ Redemption Job Already Running"
    assert started == []
    assert handler._jobs.get("RACE").kind == "auto"


@pytest.mark.asyncio
async def test_redeem_is_refused_while_shutting_down(db):
    handler = make_handler(db)
    handler._closing = True
    action = interaction(MANAGER, manage=True)

    await handler._handle_redeem_gift_code_slash(action, "CODE")

    assert "restarting" in action.followup.sent[0]["content"]
    assert handler._jobs.get("CODE") is None


@pytest.mark.asyncio
async def test_temporary_failures_keep_the_auto_code_pending_and_retry_only_those_players(db):
    outage = {"success": False, "error_code": "API_ERROR", "message": "Service unavailable"}
    responses = {932: outage}
    handler = make_handler(db, responses=responses)
    handler._redemption.REDEEM_RETRY_DELAY_SECONDS = 0
    handler._redemption.REDEEM_RETRY_MAX_DELAY_SECONDS = 0
    await register(handler, GUILD, MEMBER, "931", "932")
    add_code(db, 7, "FLAKY")
    code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]

    assert await handler._run_auto_redemption(code_row) is False
    assert pending_codes(db) == ["FLAKY"]
    assert handler._auto_retry_after.get("flaky") or handler._auto_retry_after.get("FLAKY")

    # The outage is over: the next pass retries only the unsettled player and completes.
    responses.pop(932)
    handler._gift_code_service.calls.clear()
    code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]
    assert await handler._run_auto_redemption(code_row) is True
    assert [str(player_id) for player_id, _ in handler._gift_code_service.calls] == ["932"]
    assert pending_codes(db) == []
