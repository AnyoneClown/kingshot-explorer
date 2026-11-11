"""Member-facing gift-code features and the automatic run's reliability rules."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import GiftCode, GiftCodeRedemption, GuildPlayerRegistration, RegisteredPlayer
from handlers.gift_panel import GiftPanelView, build_panel_embed
from services.gift_redemption_service import find_broken_players, is_retryable_result
from services.player_registry_service import order_auto_redeem_targets
from tests.test_gift_code_rosters import (
    BOT_ID,
    GUILD,
    MANAGER,
    MEMBER,
    OPERATOR,
    OTHER_GUILD,
    OTHER_MEMBER,
    Channel,
    add_code,
    db,  # noqa: F401  (pytest fixture)
    field,
    interaction,
    make_handler,
    pending_codes,
    register,
)

OUTAGE = {"success": False, "error_code": "API_ERROR", "message": "Service unavailable"}
MISMATCH = {"success": False, "error_code": "KINGDOM_MISMATCH", "message": "The kingdom does not match this player."}


@pytest.fixture(autouse=True)
def no_retry_jitter(monkeypatch):
    """Retries add random jitter; without it the outage tests finish instantly."""
    monkeypatch.setattr("services.gift_redemption_service.random.uniform", lambda low, high: 0.0)


def no_retry_delays(handler):
    handler._redemption.REDEEM_RETRY_DELAY_SECONDS = 0
    handler._redemption.REDEEM_RETRY_MAX_DELAY_SECONDS = 0
    return handler


# ----------------------------------------------------------------- run order


def test_self_registered_accounts_are_redeemed_before_imports_and_legacy_profiles():
    players = [SimpleNamespace(player_id=pid) for pid in ("100", "200", "300", "400", "500")]
    owners = [
        ("300", GUILD, MEMBER),          # one account of their own: self-registered
        ("200", GUILD, MANAGER), ("400", GUILD, MANAGER), ("500", GUILD, MANAGER),  # a manager's import
        ("500", OTHER_GUILD, OTHER_MEMBER),  # also someone's own account elsewhere
    ]
    ordered = order_auto_redeem_targets(players, owners, self_registered_max=2)
    # Own accounts (300, 500), then the import (200, 400), then the unassigned profile (100).
    assert [player.player_id for player in ordered] == ["300", "500", "200", "400", "100"]


@pytest.mark.asyncio
async def test_auto_targets_keep_plain_id_order_without_a_self_registration_limit(db):  # noqa: F811
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "902")
    await register(handler, GUILD, MANAGER, "901", "903", "904")
    registry = handler._player_registry_service
    assert [p.player_id for p in await registry.get_auto_redeem_targets()] == ["901", "902", "903", "904"]
    ordered = await registry.get_auto_redeem_targets(self_registered_max=2)
    assert [p.player_id for p in ordered] == ["902", "901", "903", "904"]


# ----------------------------------------------------------- broken accounts


def test_broken_players_need_repeated_failures_including_the_current_code():
    players = [SimpleNamespace(player_id=pid, player_name=f"Gov {pid}") for pid in ("1", "2", "3", "4")]
    rows = [
        # 1: failed as invalid on three codes -> flagged.
        *(SimpleNamespace(gift_code=code, player_id="1", success=False, error_code="KINGDOM_MISMATCH")
          for code in ("C", "B", "A")),
        # 2: same failures, but one older code reached the player -> healthy.
        *(SimpleNamespace(gift_code=code, player_id="2", success=False, error_code="KINGDOM_MISMATCH")
          for code in ("C", "B", "A")),
        SimpleNamespace(gift_code="A", player_id="2", success=True, error_code=None),
        # 3: failed before, but not on the current code.
        *(SimpleNamespace(gift_code=code, player_id="3", success=False, error_code="INVALID_PLAYER_ID")
          for code in ("B", "A", "Z")),
        # 4: a spending requirement is not a broken account.
        *(SimpleNamespace(gift_code=code, player_id="4", success=False, error_code="SPEND_REQUIREMENT_NOT_MET")
          for code in ("C", "B", "A")),
    ]
    failing = find_broken_players(["C", "B", "A", "Z"], players, rows, current_code="C", min_failures=3)
    assert [(player.player_id, player.failed_codes, player.error_code) for player in failing] == [
        ("1", 3, "KINGDOM_MISMATCH")
    ]
    assert find_broken_players(["C", "B", "A"], players, rows, current_code="C", min_failures=4) == []
    assert find_broken_players([], players, rows) == []


@pytest.mark.asyncio
async def test_accounts_failing_on_three_codes_are_paused_and_reported(db):  # noqa: F811
    channel = Channel(channel_id=777, guild_id=GUILD)
    operator_dm = SimpleNamespace(send=AsyncMock())
    handler = no_retry_delays(make_handler(
        db,
        responses={951: MISMATCH, 953: MISMATCH},
        guild_settings=[SimpleNamespace(guild_id=GUILD, gift_code_channel_id=777)],
        channels={777: channel},
    ))
    handler._bot.get_user = lambda user_id: operator_dm if user_id == OPERATOR else None
    await register(handler, GUILD, MEMBER, "951", "952")
    with Session(db.engine) as session, session.begin():
        session.add(RegisteredPlayer(player_id="953", kingdom="830", enabled=True, added_by_user_id=MEMBER))  # legacy

    for index, code in enumerate(("ONE", "TWO", "THREE"), start=1):
        add_code(db, index, code)
        code_row = next(row for row in await handler._gift_code_service.list_pending_gift_codes() if row.code == code)
        assert await handler._run_auto_redemption(code_row) is True
        with Session(db.engine) as session:
            flags = {
                "roster": session.scalar(select(GuildPlayerRegistration.enabled).where(
                    GuildPlayerRegistration.player_id == "951")),
                "legacy": session.scalar(select(RegisteredPlayer.enabled).where(RegisteredPlayer.player_id == "953")),
                "healthy": session.scalar(select(GuildPlayerRegistration.enabled).where(
                    GuildPlayerRegistration.player_id == "952")),
            }
        # Two failures are not enough; the third code pauses both broken accounts.
        assert flags == {"roster": index < 3, "legacy": index < 3, "healthy": True}

    report = channel.sent[-1]["embed"]
    assert report.title == "⏸️ Paused 1 Account(s) That Keep Failing"
    assert "`951`" in report.description and "kingdom does not match (3 codes)" in report.description
    assert "953" not in report.description  # not on this server's list
    operator_report = operator_dm.send.await_args.kwargs["embed"]
    assert "`951`" in operator_report.description and "`953`" in operator_report.description
    # Paused accounts are no longer redeemed for.
    targets = await handler._player_registry_service.get_auto_redeem_targets()
    assert [player.player_id for player in targets] == ["952"]


# --------------------------------------------------- outages and retry rules


def test_only_http_style_statuses_are_temporary():
    def result(code, message="Rejected"):
        return {"success": False, "error_code": code, "message": message, "status_category": "api_rejected"}

    assert is_retryable_result(result("503")) and is_retryable_result(result("429"))
    assert is_retryable_result(result("PLAYER_LOOKUP_UNAVAILABLE"))
    # The gift-code API's own five-digit business codes say nothing about an outage.
    assert not is_retryable_result(result("40021")) and not is_retryable_result(result("40004"))


@pytest.mark.asyncio
async def test_a_code_is_not_abandoned_while_an_outage_keeps_every_pass_from_starting(db):  # noqa: F811
    responses = {player_id: OUTAGE for player_id in (961, 962, 963)}
    handler = no_retry_delays(make_handler(db, responses=responses))
    await register(handler, GUILD, MANAGER, "961", "962", "963")
    add_code(db, 1, "OUTAGE")

    for _ in range(handler.MAX_AUTO_TRANSIENT_PASSES + 2):
        code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]
        assert await handler._run_auto_redemption(code_row) is False
    # Each pass stopped at the probe: nobody else was requested and the code is still pending.
    assert {player_id for player_id, _ in handler._gift_code_service.calls} == {961}
    assert pending_codes(db) == ["OUTAGE"]

    responses.clear()
    code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]
    assert await handler._run_auto_redemption(code_row) is True
    with Session(db.engine) as session:
        redeemed = session.scalars(
            select(GiftCodeRedemption.player_id).where(GiftCodeRedemption.success.is_(True))
        ).all()
    assert sorted(redeemed) == ["961", "962", "963"]


@pytest.mark.asyncio
async def test_a_pending_code_without_expiry_is_given_up_after_the_time_limit(db):  # noqa: F811
    handler = no_retry_delays(make_handler(db, responses={971: OUTAGE, 972: OUTAGE}))
    await register(handler, GUILD, MANAGER, "971", "972")
    with Session(db.engine) as session, session.begin():
        session.add(GiftCode(
            id=5, code="STALE", created_at_api=datetime.now(timezone.utc) - timedelta(days=5),
            discovered_at=datetime.now(timezone.utc) - timedelta(hours=handler.MAX_AUTO_PENDING_HOURS + 1),
            expires_at=None, auto_redeem_status="pending",
        ))
    code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]
    assert await handler._run_auto_redemption(code_row) is True
    assert pending_codes(db) == []


@pytest.mark.asyncio
async def test_an_outage_that_starts_mid_run_ends_the_pass_early(db):  # noqa: F811
    player_ids = list(range(981, 993))  # 12 players
    responses = {player_id: OUTAGE for player_id in player_ids[1:]}  # only the probe works
    handler = no_retry_delays(make_handler(db, responses=responses))
    handler._config.max_players_per_guild = 50
    await register(handler, GUILD, MANAGER, *(str(player_id) for player_id in player_ids))
    add_code(db, 1, "STORM")
    code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]

    assert await handler._run_auto_redemption(code_row) is False
    requested = {player_id for player_id, _ in handler._gift_code_service.calls}
    # Probe + two batches of three temporary failures, then the pass ends; five players are untouched.
    assert requested == set(player_ids[:7])
    assert pending_codes(db) == ["STORM"]


# --------------------------------------------------------- live announcements


@pytest.mark.asyncio
async def test_long_runs_announce_the_code_while_redeeming_and_then_edit_the_summary(db, gift_cards):  # noqa: F811
    channel = Channel(channel_id=777, guild_id=GUILD)
    posted = SimpleNamespace(id=4242, edit=AsyncMock())

    async def send(**kwargs):
        channel.sent.append(kwargs)
        return posted

    channel.send = send
    handler = make_handler(
        db,
        guild_settings=[SimpleNamespace(guild_id=GUILD, gift_code_channel_id=777)],
        channels={777: channel},
    )
    handler.LIVE_ANNOUNCE_MIN_REMAINING_SECONDS = 2.0  # at least one player still to go
    handler.LIVE_ANNOUNCE_INTERVAL_SECONDS = 3600.0
    await register(handler, GUILD, MEMBER, "801", "802")
    await register(handler, GUILD, MANAGER, "803", "804")
    add_code(db, 1, "LIVE", expires_in_days=2)
    code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]

    assert await handler._run_auto_redemption(code_row) is True

    # One message: posted as "redeeming" after the first success, then edited into the summary.
    assert len(channel.sent) == 1
    live = channel.sent[0]["embed"]
    assert live.title == "🎁 New Gift Code — Redeeming…"
    assert "**1/4** players processed" in live.description
    assert field(live, "Gift Code").value.startswith("`LIVE` · expires <t:")
    assert "Governor ID" in field(live, "Don't want to wait?").value
    # The live message has no picture; the summary replaces the text results with the card.
    assert "files" not in channel.sent[0]
    edited = posted.edit.await_args.kwargs
    final = edited["embed"]
    assert final.title == "🎁 New Gift Code Redeemed!" and final.image.url == "attachment://gift-code.png"
    assert [file.filename for file in edited["attachments"]] == ["gift-code.png"]
    assert gift_cards[-1].stats.counts["success"] == 4 and gift_cards[-1].title == "Gift code redeemed"
    assert await handler._gift_code_service.get_announcements("LIVE") == {777: 4242}


@pytest.mark.asyncio
async def test_a_restart_edits_the_stored_announcement_instead_of_posting_again(db):  # noqa: F811
    partial = SimpleNamespace(edit=AsyncMock())
    channel = Channel(channel_id=777, guild_id=GUILD)
    channel.get_partial_message = lambda message_id: partial if message_id == 4242 else None
    handler = make_handler(
        db,
        guild_settings=[SimpleNamespace(guild_id=GUILD, gift_code_channel_id=777)],
        channels={777: channel},
    )
    await register(handler, GUILD, MEMBER, "811")
    # A previous process posted the first summary and stored its message ID.
    await handler._gift_code_service.save_announcement("AGAIN", 777, 4242)

    await handler._announce_auto_redemption(
        "AGAIN", [{"player_id": "811", "status_category": "success", "success": True}], final=True
    )

    assert channel.sent == []
    partial.edit.assert_awaited_once()
    assert handler._auto_announcements == {} and handler._announcements_loaded == set()


@pytest.mark.asyncio
async def test_a_server_channel_that_is_also_a_legacy_channel_gets_only_the_server_summary(db, monkeypatch, gift_cards):  # noqa: F811
    monkeypatch.setattr("discord.TextChannel", Channel)
    channel = Channel(channel_id=777, guild_id=GUILD)
    handler = make_handler(
        db,
        guild_settings=[SimpleNamespace(guild_id=GUILD, gift_code_channel_id=777)],
        auto_channels={777},
        channels={777: channel},
    )
    await register(handler, GUILD, MEMBER, "821")
    with Session(db.engine) as session, session.begin():
        session.add(RegisteredPlayer(player_id="822", kingdom="830", enabled=True, added_by_user_id=MEMBER))
    add_code(db, 1, "SHARED")
    code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]

    assert await handler._run_auto_redemption(code_row) is True

    assert len(channel.sent) == 1
    assert "this server's roster" in channel.sent[0]["embed"].description
    assert gift_cards[-1].stats.total == 1


# ------------------------------------------------------------- submit a code


@pytest.mark.asyncio
async def test_a_member_submitted_code_is_checked_with_their_account_and_queued(db):  # noqa: F811
    handler = make_handler(db)
    started = []
    handler._ensure_pending_worker = lambda: started.append(True)
    await register(handler, GUILD, MANAGER, "701")
    await register(handler, GUILD, MEMBER, "702")

    action = interaction(MEMBER)
    await handler._handle_submit_code(action, "  FOUND2026 ")

    assert action.response.deferred["ephemeral"] is True
    embed = action.followup.sent[0]["embed"]
    assert embed.title == "✅ Code Accepted — Thank You!"
    assert "Your account already received it" in embed.description and "2 account(s)" in embed.description
    # The member's own account was the test account, and its result is stored for the run.
    assert handler._gift_code_service.calls == [(702, "FOUND2026")]
    assert pending_codes(db) == ["FOUND2026"] and started == [True]
    with Session(db.engine) as session:
        stored = session.scalars(select(GiftCode).where(GiftCode.code == "FOUND2026")).one()
        assert stored.id < 0  # local ID; the upstream list uses positive ones

    # The automatic run skips the already redeemed account.
    code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]
    assert await handler._run_auto_redemption(code_row) is True
    assert handler._gift_code_service.calls == [(702, "FOUND2026"), (701, "FOUND2026")]

    # When the upstream list publishes the same code later, it is the same row.
    is_new, row = await handler._gift_code_service.add_or_update_gift_code(
        code_id=77, code="FOUND2026", created_at_api=datetime.now(timezone.utc)
    )
    assert is_new is False and row.id == stored.id

    again = interaction(OTHER_MEMBER)
    await handler._handle_submit_code(again, "FOUND2026")
    assert again.followup.sent[0]["embed"].title == "ℹ️ The Bot Already Has This Code"


@pytest.mark.asyncio
async def test_rejected_unverifiable_and_rate_limited_submissions_are_not_queued(db):  # noqa: F811
    handler = no_retry_delays(make_handler(db))
    responses = handler._gift_code_service.responses
    handler._ensure_pending_worker = lambda: None
    await register(handler, GUILD, MEMBER, "711")

    bad_format = interaction(MEMBER)
    await handler._handle_submit_code(bad_format, "two words")
    assert bad_format.followup.sent[0]["embed"].title == "❌ That Does Not Look Like a Gift Code"
    assert handler._gift_code_service.calls == []

    responses[711] = {
        "success": False, "error_code": "GIFT_CODE_NOT_FOUND", "message": "Gift code was not found.",
        "global_code_error": True,
    }
    unknown = interaction(MEMBER)
    await handler._handle_submit_code(unknown, "NOPE")
    assert unknown.followup.sent[0]["embed"].title == "❌ The Code Does Not Work"

    # One submission per minute per member.
    hurried = interaction(MEMBER)
    await handler._handle_submit_code(hurried, "NOPE2")
    assert hurried.followup.sent[0]["embed"].title == "⏳ Slow Down"

    responses[711] = OUTAGE
    handler._submit_cooldowns.clear()
    down = interaction(MEMBER)
    await handler._handle_submit_code(down, "LATER")
    assert down.followup.sent[0]["embed"].title == "⚠️ Could Not Check the Code"

    responses[711] = MISMATCH
    handler._submit_cooldowns.clear()
    broken = interaction(MEMBER)
    await handler._handle_submit_code(broken, "MAYBE")
    assert "not confirmed" in broken.followup.sent[0]["embed"].description

    # A requirement the test account does not meet still proves the code exists.
    responses[711] = {
        "success": False, "error_code": "SPEND_REQUIREMENT_NOT_MET",
        "message": "The player does not meet this code's spending requirement.",
    }
    handler._submit_cooldowns.clear()
    vip = interaction(MEMBER)
    await handler._handle_submit_code(vip, "VIPONLY")
    assert vip.followup.sent[0]["embed"].title == "✅ Code Accepted — Thank You!"
    assert pending_codes(db) == ["VIPONLY"]
    assert handler._jobs.active_jobs() == []


# --------------------------------------------------------- panel, my accounts


@pytest.mark.asyncio
async def test_my_accounts_lists_own_entries_with_recent_results(db):  # noqa: F811
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "601", "602")
    await register(handler, GUILD, MANAGER, "603")
    await handler._player_registry_service.set_guild_players_enabled(GUILD, ["602"], False)
    add_code(db, 1, "OLD", status="completed")
    add_code(db, 2, "NEW", status="completed")
    with Session(db.engine) as session, session.begin():
        session.add_all([
            GiftCodeRedemption(user_id=BOT_ID, player_id="601", gift_code="NEW", success=True),
            GiftCodeRedemption(user_id=BOT_ID, player_id="601", gift_code="OLD", success=False,
                               error_code="ALREADY_REDEEMED_BY_API"),
            GiftCodeRedemption(user_id=BOT_ID, player_id="602", gift_code="OLD", success=False,
                               error_code="KINGDOM_MISMATCH"),
        ])

    action = interaction(MEMBER)
    await handler._handle_my_accounts(action)

    assert action.followup.sent[0]["ephemeral"] is True
    embed = action.followup.sent[0]["embed"]
    assert "2 account(s)" in embed.description and [f.name for f in embed.fields] == ["Gov 601", "Gov 602"]
    first, second = embed.fields
    assert "`601` · Kingdom 830 · ✅ receiving codes" in first.value
    assert "`NEW` ✅ redeemed" in first.value and "`OLD` ☑️ already claimed" in first.value
    assert "⏸️ paused" in second.value
    assert "`NEW` — no result yet" in second.value and "`OLD` ❌ kingdom does not match" in second.value

    newcomer = interaction(OTHER_MEMBER)
    await handler._handle_my_accounts(newcomer)
    assert "have not added an account" in newcomer.followup.sent[0]["embed"].description


@pytest.mark.asyncio
async def test_gift_panel_is_posted_by_managers_and_claims_an_unset_channel(db):  # noqa: F811
    handler = make_handler(db)
    updates = []

    async def update_for_guild(guild_id, **changes):
        updates.append((guild_id, changes))

    handler._guild_configuration_service.update_for_guild = update_for_guild
    handler._guild_configuration_service.get_settings = AsyncMock(
        return_value=SimpleNamespace(manager_role_id=None, gift_code_channel_id=None)
    )

    member = interaction(MEMBER)
    await handler._handle_gift_panel(member)
    assert member.response.sent[0]["embed"].title == "🔒 Not allowed" and member.channel.sent == []

    manager = interaction(MANAGER, manage=True)
    await handler._handle_gift_panel(manager)
    panel = manager.channel.sent[0]
    assert panel["embed"].title == "🎁 Gift codes" and "up to 2 accounts" in panel["embed"].description
    assert isinstance(panel["view"], GiftPanelView) and panel["view"].is_persistent()
    assert [item.label for item in panel["view"].children] == [
        "Add my ID", "My accounts", "Active codes", "Submit a code",
    ]
    assert updates == [(GUILD, {"gift_code_channel_id": 789})]
    assert "announcements will be posted in <#789>" in manager.followup.sent[0]["embed"].description


@pytest.mark.asyncio
async def test_panel_buttons_open_the_member_workflows_and_refuse_banned_users(db):  # noqa: F811
    handler = make_handler(db)
    handler._handle_my_accounts = AsyncMock()
    handler._handle_list_gift_codes_slash = AsyncMock()
    view = GiftPanelView(handler)
    action = interaction(MEMBER)

    assert await view.interaction_check(action)
    await view.mine_button.callback(action)
    handler._handle_my_accounts.assert_awaited_once_with(action)
    await view.codes_button.callback(action)
    handler._handle_list_gift_codes_slash.assert_awaited_once_with(action, ephemeral=True)

    handler._access._banned_user_ids = {MEMBER}
    banned = interaction(MEMBER)
    assert not await view.interaction_check(banned)
    assert banned.response.sent[0]["embed"].title == "🔒 Not allowed"
    assert "Add my ID" in build_panel_embed(0).description and "up to" not in build_panel_embed(0).description


@pytest.mark.asyncio
async def test_redeem_is_refused_where_the_bot_cannot_post_the_progress_card(db):  # noqa: F811
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "731")
    action = interaction(MANAGER, manage=True)
    action.channel.guild.me = SimpleNamespace(id=BOT_ID)
    action.channel.permissions_for = lambda member: discord.Permissions(view_channel=True, send_messages=False)

    await handler._handle_redeem_gift_code_slash(action, "CODE")

    assert action.followup.sent[0]["embed"].title == "❌ Channel Unavailable"
    assert handler._jobs.get("CODE") is None and handler._gift_code_service.calls == []


# ------------------------------------------------------- second review fixes


@pytest.mark.asyncio
async def test_only_a_lookup_outage_is_temporary_not_an_unknown_player():
    from services.gift_redemption_service import GiftRedemptionService

    player = SimpleNamespace(player_id="1", player_name="Gov", kingdom="830", player_uid=None, castle_level=None)

    async def run(lookup_response):
        gift = SimpleNamespace(redeem_gift_code_remote=AsyncMock(return_value=dict(MISMATCH)))
        data = SimpleNamespace(get_player_by_fid=AsyncMock(return_value=lookup_response))
        service = GiftRedemptionService(gift, SimpleNamespace(), data)

        async def no_wait(player_id, delay):
            return None

        return await service._redeem_player(player, 1, "CODE", rate_limit_wait=no_wait, stop_event=None)

    # Not found, a bad key: the mismatch stays final, so the run moves on to the next player.
    for response in (
        {"success": False, "status_code": 404, "error_code": "HTTP_ERROR", "error_message": "not found"},
        {"success": False, "status_code": None, "error_code": "MISSING_API_KEY"},
    ):
        result = await run(response)
        assert result["error_code"] == "KINGDOM_MISMATCH" and not is_retryable_result(result)
    # The lookup service itself is down: retried later.
    for response in (
        {"success": False, "status_code": None, "error_code": "NETWORK_ERROR"},
        {"success": False, "status_code": 503, "error_code": "HTTP_ERROR"},
    ):
        result = await run(response)
        assert result["error_code"] == "KINGDOM_REFRESH_UNAVAILABLE" and is_retryable_result(result)


@pytest.mark.asyncio
async def test_outage_passes_never_complete_a_code_that_has_not_expired(db):  # noqa: F811
    # One account: every pass "reaches" it, but an outage settles nobody.
    handler = no_retry_delays(make_handler(db, responses={991: OUTAGE}))
    await register(handler, GUILD, MEMBER, "991")
    with Session(db.engine) as session, session.begin():
        session.add(GiftCode(
            id=9, code="WEEKLONG", created_at_api=datetime.now(timezone.utc) - timedelta(days=4),
            discovered_at=datetime.now(timezone.utc) - timedelta(hours=handler.MAX_AUTO_PENDING_HOURS + 5),
            expires_at=datetime.now(timezone.utc) + timedelta(days=3), auto_redeem_status="pending",
        ))
    for _ in range(handler.MAX_AUTO_TRANSIENT_PASSES + 2):
        code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]
        assert await handler._run_auto_redemption(code_row) is False
    # Still valid for three days: neither the pass limit nor the 72-hour limit ends it.
    assert pending_codes(db) == ["WEEKLONG"]


def test_an_interrupted_pass_never_reads_as_finished():
    from handlers.gift_code_handler import GiftCodeHandler

    handler = GiftCodeHandler(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(admin_user_ids=set()))
    untouched = handler._build_auto_redemption_embed("CODE", 0, [], scope="this server's roster", final=False)
    assert untouched.title == "🎁 New Gift Code — Redeeming…"
    assert "interrupted" in untouched.description and "finished" not in untouched.description.split("when it")[0]
    partly = handler._build_auto_redemption_embed(
        "CODE", 1, [{"status_category": "success"}], scope="this server's roster", final=False
    )
    assert partly.title == "🎁 New Gift Code Redeemed!" and "continues automatically" in partly.description
    done = handler._build_auto_redemption_embed("CODE", 0, [], scope="this server's roster", final=True)
    assert done.title == "ℹ️ Gift Code Finished Without New Rewards"


@pytest.mark.asyncio
async def test_details_of_an_all_servers_run_are_for_the_requester_and_operators(db):  # noqa: F811
    from handlers.gift_code_handler import RedemptionResultsView

    handler = make_handler(db)
    embed = discord.Embed(title="Summary")
    issues = [{"player_id": "1", "status_category": "invalid_id", "message": "x"}]
    local = RedemptionResultsView(embed, issues, MEMBER, access_policy=handler._access, timeout=60)
    everywhere = RedemptionResultsView(
        embed, issues, OPERATOR, access_policy=handler._access, timeout=60, all_servers=True
    )
    assert await local.interaction_check(interaction(MANAGER, manage=True))
    assert not await local.interaction_check(interaction(OTHER_MEMBER))
    assert not await everywhere.interaction_check(interaction(MANAGER, manage=True))
    assert await everywhere.interaction_check(interaction(OPERATOR))


def test_submission_budgets_are_per_member_and_per_server():
    from handlers.gift_code_handler import GiftCodeHandler

    handler = GiftCodeHandler(SimpleNamespace(), SimpleNamespace(), SimpleNamespace(), SimpleNamespace(admin_user_ids=set()))
    handler.SUBMIT_USER_COOLDOWN_SECONDS = 0
    for _ in range(handler.SUBMIT_USER_MAX_PER_HOUR):
        assert handler._claim_submit_slot(1, GUILD) is None
    assert "several codes" in handler._claim_submit_slot(1, GUILD)
    # Other members of the server still can, until the server's own budget is used up...
    for user_id in range(2, 2 + handler.SUBMIT_GUILD_MAX_PER_HOUR - handler.SUBMIT_USER_MAX_PER_HOUR):
        assert handler._claim_submit_slot(user_id, GUILD) is None
    assert "This server" in handler._claim_submit_slot(50, GUILD)
    # ...and another server is not affected.
    assert handler._claim_submit_slot(50, OTHER_GUILD) is None


def test_threads_use_their_own_send_permission_and_are_not_announcement_channels():
    from handlers.setup_prompts import bot_can_post, bot_send_permission, is_announcement_target

    thread = object.__new__(discord.Thread)
    read_only_parent = discord.Permissions(view_channel=True, send_messages=False, send_messages_in_threads=True,
                                           embed_links=True)
    assert bot_send_permission(thread, read_only_parent) is True
    assert bot_send_permission(SimpleNamespace(), read_only_parent) is False
    assert is_announcement_target(thread) is False
    assert is_announcement_target(object.__new__(discord.TextChannel)) is True
    assert is_announcement_target(SimpleNamespace(id=1)) is True

    def broken(member):
        raise discord.ClientException("Parent channel not found")

    uncached = SimpleNamespace(guild=SimpleNamespace(me=object()), permissions_for=broken)
    assert bot_can_post(uncached) is True  # cannot be checked: do not block the command
