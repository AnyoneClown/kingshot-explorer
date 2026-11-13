"""Member-facing gift-code features and the automatic run's reliability rules."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import GiftCode, GiftCodeRedemption, RegisteredPlayer
from handlers.gift_panel import GiftPanelView, build_panel_embed
from services.gift_redemption_service import FailingPlayer, find_broken_players, is_retryable_result
from services.player_registry_service import order_redeem_targets
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
# ``register`` stores accounts with kingdom 830; a server follows the kingdom in its settings.
KINGDOM, OTHER_KINGDOM = 830, 795
THIRD_GUILD = 3003


@pytest.fixture(autouse=True)
def no_retry_jitter(monkeypatch):
    """Retries add random jitter; without it the outage tests finish instantly."""
    monkeypatch.setattr("services.gift_redemption_service.random.uniform", lambda low, high: 0.0)


def no_retry_delays(handler):
    handler._redemption.REDEEM_RETRY_DELAY_SECONDS = 0
    handler._redemption.REDEEM_RETRY_MAX_DELAY_SECONDS = 0
    return handler


def server(guild_id, channel_id, kingdom):
    """A server's settings row: its gift-code channel and the kingdom it follows (None: the whole list)."""
    return SimpleNamespace(guild_id=guild_id, gift_code_channel_id=channel_id, kingdom_id=kingdom)


def add_account(db, player_id, owner):  # noqa: F811
    """Put an account of the other kingdom on the shared list."""
    with Session(db.engine) as session, session.begin():
        session.add(RegisteredPlayer(
            player_id=player_id, player_name=f"Gov {player_id}", kingdom=str(OTHER_KINGDOM),
            enabled=True, added_by_user_id=owner,
        ))


# ----------------------------------------------------------------- run order


def test_self_registered_accounts_are_redeemed_before_bulk_imports():
    owners = {
        "300": MEMBER,                                   # one account of their own: self-registered
        "200": MANAGER, "400": MANAGER, "500": MANAGER,  # a manager's import
        "100": OTHER_MEMBER, "600": OTHER_MEMBER,        # two of their own: still within the limit
    }
    players = [SimpleNamespace(player_id=pid, added_by_user_id=owner) for pid, owner in sorted(owners.items())]
    ordered = order_redeem_targets(players, self_registered_max=2)
    # Own accounts (100, 300, 600), then the import (200, 400, 500).
    assert [player.player_id for player in ordered] == ["100", "300", "600", "200", "400", "500"]


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
    whole_list = Channel(channel_id=888, guild_id=OTHER_GUILD)
    elsewhere = Channel(channel_id=999, guild_id=THIRD_GUILD)
    operator_dm = SimpleNamespace(send=AsyncMock())
    handler = no_retry_delays(make_handler(
        db,
        responses={951: MISMATCH, 953: MISMATCH},
        guild_settings=[
            server(GUILD, 777, KINGDOM),
            server(OTHER_GUILD, 888, None),  # no kingdom chosen: follows the whole list
            server(THIRD_GUILD, 999, 111),   # a kingdom without accounts
        ],
        channels={777: channel, 888: whole_list, 999: elsewhere},
    ))
    handler._bot.get_user = lambda user_id: operator_dm if user_id == OPERATOR else None
    await register(handler, GUILD, MEMBER, "951", "952")
    add_account(db, "953", OTHER_MEMBER)

    for index, code in enumerate(("ONE", "TWO", "THREE"), start=1):
        add_code(db, index, code)
        code_row = next(row for row in await handler._gift_code_service.list_pending_gift_codes() if row.code == code)
        assert await handler._run_auto_redemption(code_row) is True
        with Session(db.engine) as session:
            flags = dict(session.execute(select(RegisteredPlayer.player_id, RegisteredPlayer.enabled)).all())
        # Two failures are not enough; the third code pauses both broken accounts, whichever kingdom.
        assert flags == {"951": index < 3, "953": index < 3, "952": True}

    report = channel.sent[-1]["embed"]
    assert report.title == "⏸️ Paused 1 Account(s) That Keep Failing"
    assert "`951`" in report.description and "kingdom does not match (3 codes)" in report.description
    assert "These accounts (kingdom 830)" in report.description
    assert "953" not in report.description  # an account of another kingdom
    everything = whole_list.sent[-1]["embed"]
    assert everything.title == "⏸️ Paused 2 Account(s) That Keep Failing"
    assert "`951`" in everything.description and "`953`" in everything.description
    assert elsewhere.sent == []
    operator_report = operator_dm.send.await_args.kwargs["embed"]
    assert "`951`" in operator_report.description and "`953`" in operator_report.description
    assert "(every kingdom)" in operator_report.description
    # Paused accounts are no longer redeemed for.
    targets = await handler._player_registry_service.get_auto_redeem_targets()
    assert [player.player_id for player in targets] == ["952"]


@pytest.mark.asyncio
async def test_paused_account_notices_are_split_by_the_kingdom_each_server_follows(db):  # noqa: F811
    channels = {
        777: Channel(777, GUILD), 888: Channel(888, OTHER_GUILD), 999: Channel(999, THIRD_GUILD),
    }
    operator_dm = SimpleNamespace(send=AsyncMock())
    handler = make_handler(
        db,
        guild_settings=[
            server(GUILD, 777, KINGDOM),
            server(OTHER_GUILD, 888, str(OTHER_KINGDOM)),
            server(THIRD_GUILD, 999, None),
            server(4004, 1111, KINGDOM),  # its channel is gone: skipped, the others still hear
        ],
        channels=channels,
    )
    handler._bot.get_user = lambda user_id: operator_dm if user_id == OPERATOR else None
    failing = [FailingPlayer(pid, f"Gov {pid}", 3, "KINGDOM_MISMATCH") for pid in ("1", "2", "3")]

    # Kingdoms come as the profiles store them (text), as numbers, or not at all.
    await handler._notify_disabled_accounts(failing, {"1": "830", "2": OTHER_KINGDOM, "3": None})

    def reported(channel_id):
        (message,) = channels[channel_id].sent
        return [pid for pid in ("1", "2", "3") if f"`{pid}`" in message["embed"].description]

    assert reported(777) == ["1"]
    assert reported(888) == ["2"]
    # An account without a stored kingdom only reaches the servers that follow the whole list.
    assert reported(999) == ["1", "2", "3"]
    assert "(every registered player)" in channels[999].sent[0]["embed"].description
    operator_report = operator_dm.send.await_args.kwargs["embed"]
    assert operator_report.title == "⏸️ Paused 3 Account(s) That Keep Failing"


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
        guild_settings=[server(GUILD, 777, KINGDOM)],
        channels={777: channel},
    )
    handler.LIVE_ANNOUNCE_MIN_REMAINING_SECONDS = 2.0  # at least one player still to go
    handler.LIVE_ANNOUNCE_INTERVAL_SECONDS = 3600.0
    await register(handler, GUILD, MEMBER, "801", "802")
    await register(handler, GUILD, MANAGER, "803", "804")
    # Redeemed in the same run, but not this server's kingdom: not in its counts.
    add_account(db, "805", OTHER_MEMBER)
    add_code(db, 1, "LIVE", expires_in_days=2)
    code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]

    assert await handler._run_auto_redemption(code_row) is True

    # One message: posted as "redeeming" after the first success, then edited into the summary.
    assert len(channel.sent) == 1
    live = channel.sent[0]["embed"]
    assert live.title == "🎁 New Gift Code — Redeeming…"
    assert "Redeeming for kingdom 830: **1/4** players processed" in live.description
    assert field(live, "Gift Code").value.startswith("`LIVE` · expires <t:")
    assert "Governor ID" in field(live, "Don't want to wait?").value
    # The live message has no picture; the summary replaces the text results with the card.
    assert "files" not in channel.sent[0]
    edited = posted.edit.await_args.kwargs
    final = edited["embed"]
    assert final.title == "🎁 New Gift Code Redeemed!" and final.image.url == "attachment://gift-code.png"
    assert [file.filename for file in edited["attachments"]] == ["gift-code.png"]
    assert gift_cards[-1].stats.counts["success"] == 4 and gift_cards[-1].title == "Gift code redeemed"
    assert gift_cards[-1].stats.total == 4
    assert await handler._gift_code_service.get_announcements("LIVE") == {777: 4242}
    assert len(handler._gift_code_service.calls) == 5


@pytest.mark.asyncio
async def test_a_restart_edits_the_stored_announcement_instead_of_posting_again(db):  # noqa: F811
    partial = SimpleNamespace(edit=AsyncMock())
    channel = Channel(channel_id=777, guild_id=GUILD)
    channel.get_partial_message = lambda message_id: partial if message_id == 4242 else None
    other_kingdom = Channel(channel_id=888, guild_id=OTHER_GUILD)
    handler = make_handler(
        db,
        guild_settings=[server(GUILD, 777, KINGDOM), server(OTHER_GUILD, 888, OTHER_KINGDOM)],
        channels={777: channel, 888: other_kingdom},
    )
    await register(handler, GUILD, MEMBER, "811")
    # A previous process posted the first summary and stored its message ID.
    await handler._gift_code_service.save_announcement("AGAIN", 777, 4242)

    await handler._announce_auto_redemption(
        "AGAIN", [{"player_id": "811", "status_category": "success", "success": True}], final=True
    )

    assert channel.sent == []
    partial.edit.assert_awaited_once()
    # Called without the run's targets: the account's kingdom is read from the list, so
    # the summary still counts it for its kingdom's server, and only there.
    edited = partial.edit.await_args.kwargs["embed"]
    assert edited.title == "🎁 New Gift Code Redeemed!" and "finished for kingdom 830" in edited.description
    assert other_kingdom.sent == []
    assert handler._auto_announcements == {} and handler._announcements_loaded == set()


@pytest.mark.asyncio
async def test_a_server_channel_that_is_also_a_legacy_channel_gets_only_the_server_summary(db, monkeypatch, gift_cards):  # noqa: F811
    monkeypatch.setattr("discord.TextChannel", Channel)
    channel = Channel(channel_id=777, guild_id=GUILD)
    handler = make_handler(
        db,
        guild_settings=[server(GUILD, 777, KINGDOM)],
        auto_channels={777},
        channels={777: channel},
    )
    await register(handler, GUILD, MEMBER, "821")
    add_account(db, "822", MEMBER)
    add_code(db, 1, "SHARED")
    code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]

    assert await handler._run_auto_redemption(code_row) is True

    assert len(channel.sent) == 1
    assert "finished for kingdom 830" in channel.sent[0]["embed"].description
    # One card, of the server's kingdom: the summary of all 2 accounts was not drawn for this channel.
    assert [card.stats.total for card in gift_cards] == [1]


# ------------------------------------------------------------- submit a code


@pytest.mark.asyncio
async def test_a_member_submitted_code_is_checked_with_their_account_and_queued(db):  # noqa: F811
    handler = make_handler(db)
    started = []
    handler._ensure_pending_worker = lambda: started.append(True)
    await register(handler, GUILD, MANAGER, "701")
    # Their account plays in another kingdom than this server follows; it is theirs all the same.
    add_account(db, "702", MEMBER)

    action = interaction(MEMBER)
    await handler._handle_enter_code(action, "  FOUND2026 ")

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
    await handler._handle_enter_code(again, "FOUND2026")
    assert again.followup.sent[0]["embed"].title == "ℹ️ The Bot Already Has This Code"


@pytest.mark.asyncio
async def test_rejected_unverifiable_and_rate_limited_submissions_are_not_queued(db):  # noqa: F811
    handler = no_retry_delays(make_handler(db))
    responses = handler._gift_code_service.responses
    handler._ensure_pending_worker = lambda: None
    await register(handler, GUILD, MEMBER, "711")

    bad_format = interaction(MEMBER)
    await handler._handle_enter_code(bad_format, "two words")
    assert bad_format.followup.sent[0]["embed"].title == "❌ That Does Not Look Like a Gift Code"
    assert handler._gift_code_service.calls == []

    responses[711] = {
        "success": False, "error_code": "GIFT_CODE_NOT_FOUND", "message": "Gift code was not found.",
        "global_code_error": True,
    }
    unknown = interaction(MEMBER)
    await handler._handle_enter_code(unknown, "NOPE")
    assert unknown.followup.sent[0]["embed"].title == "❌ The Code Does Not Work"

    # One submission per minute per member.
    hurried = interaction(MEMBER)
    await handler._handle_enter_code(hurried, "NOPE2")
    assert hurried.followup.sent[0]["embed"].title == "⏳ Slow Down"

    responses[711] = OUTAGE
    handler._submit_cooldowns.clear()
    down = interaction(MEMBER)
    await handler._handle_enter_code(down, "LATER")
    assert down.followup.sent[0]["embed"].title == "⚠️ Could Not Check the Code"

    responses[711] = MISMATCH
    handler._submit_cooldowns.clear()
    broken = interaction(MEMBER)
    await handler._handle_enter_code(broken, "MAYBE")
    assert "not confirmed" in broken.followup.sent[0]["embed"].description

    # A requirement the test account does not meet still proves the code exists.
    responses[711] = {
        "success": False, "error_code": "SPEND_REQUIREMENT_NOT_MET",
        "message": "The player does not meet this code's spending requirement.",
    }
    handler._submit_cooldowns.clear()
    vip = interaction(MEMBER)
    await handler._handle_enter_code(vip, "VIPONLY")
    assert vip.followup.sent[0]["embed"].title == "✅ Code Accepted — Thank You!"
    assert pending_codes(db) == ["VIPONLY"]
    assert handler._jobs.active_jobs() == []


# --------------------------------------------------------- panel, my accounts


@pytest.mark.asyncio
async def test_my_accounts_lists_own_accounts_of_every_kingdom_with_recent_results(db):  # noqa: F811
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "601")
    add_account(db, "602", MEMBER)  # their second account plays in another kingdom
    await register(handler, GUILD, MANAGER, "603")
    assert await handler._player_registry_service.set_players_enabled(["602"], False) == 1
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

    assert action.response.deferred["ephemeral"] is True
    assert action.followup.sent[0]["ephemeral"] is True
    embed = action.followup.sent[0]["embed"]
    assert "2 account(s) on the gift-code list" in embed.description
    # The account the buttons act on is marked.
    assert [f.name for f in embed.fields] == ["▸ Gov 601", "Gov 602"]
    view = action.followup.sent[0]["view"]
    assert [item.label for item in view.children if hasattr(item, "label")] == ["Add account", "Pause", "Remove"]
    first, second = embed.fields
    assert "`601` · Kingdom 830 · ✅ receiving codes" in first.value
    assert "`NEW` ✅ redeemed" in first.value and "`OLD` ☑️ already claimed" in first.value
    assert "`602` · Kingdom 795 · ⏸️ paused" in second.value
    assert "`NEW` — no result yet" in second.value and "`OLD` ❌ kingdom does not match" in second.value

    # The list is shared: the same accounts show wherever the member asks.
    elsewhere = interaction(MEMBER, guild_id=OTHER_GUILD)
    await handler._handle_my_accounts(elsewhere)
    assert elsewhere.followup.sent[0]["embed"].to_dict() == embed.to_dict()

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
    handler._handle_active_codes = AsyncMock()
    view = GiftPanelView(handler)
    action = interaction(MEMBER)

    assert await view.interaction_check(action)
    await view.mine_button.callback(action)
    handler._handle_my_accounts.assert_awaited_once_with(action)
    await view.codes_button.callback(action)
    handler._handle_active_codes.assert_awaited_once_with(action)

    handler._access._banned_user_ids = {MEMBER}
    banned = interaction(MEMBER)
    assert not await view.interaction_check(banned)
    assert banned.response.sent[0]["embed"].title == "🔒 Not allowed"
    assert "Add my ID" in build_panel_embed(0).description and "up to" not in build_panel_embed(0).description


@pytest.mark.asyncio
async def test_gift_command_opens_the_same_panel_privately(db):  # noqa: F811
    handler = make_handler(db)
    action = interaction(MEMBER)
    await handler._handle_gift(action)
    panel = action.response.sent[0]
    assert panel["ephemeral"] is True and panel["embed"].title == "🎁 Gift codes"
    assert isinstance(panel["view"], GiftPanelView)
    assert action.channel.sent == []  # nothing is posted in the channel

    outside = interaction(MEMBER, guild_id=None)
    await handler._handle_gift(outside)
    assert outside.response.sent[0]["embed"].title == "🔒 Not allowed"


@pytest.mark.asyncio
async def test_active_codes_are_a_private_copyable_list_without_controls(db):  # noqa: F811
    handler = make_handler(db)
    handler._gift_code_service.get_available_gift_codes = AsyncMock(return_value={"success": True, "data": [
        {"code": "ALPHA", "expiresAt": "2099-01-01T00:00:00Z"}, {"code": "BE`TA"}, {"id": 3},
    ]})
    for action in (interaction(MEMBER), interaction(MANAGER, manage=True)):
        await handler._handle_active_codes(action)
        reply = action.followup.sent[0]
        assert action.response.deferred["ephemeral"] is True and reply["ephemeral"] is True
        assert reply["content"] == "`ALPHA`\n`BETA`"
        assert "view" not in reply  # managers enter a code to run it again; there is no button here
        assert [f.name for f in reply["embed"].fields] == ["ALPHA", "BE`TA"]
        assert reply["embed"].fields[0].value.startswith("Expires <t:")
        assert "centurygame.com" in reply["embed"].description

    handler._gift_code_service.get_available_gift_codes = AsyncMock(return_value={"success": True, "data": []})
    empty = interaction(MEMBER)
    await handler._handle_active_codes(empty)
    assert empty.followup.sent[0]["embed"].title == "📋 No Active Gift Codes"


@pytest.mark.asyncio
async def test_an_entered_code_is_checked_with_up_to_three_accounts(db):  # noqa: F811
    invalid = {"success": False, "error_code": "INVALID_PLAYER_ID", "message": "Player not found."}
    handler = make_handler(db, responses={741: invalid, 742: invalid})
    handler._ensure_pending_worker = lambda: None
    await register(handler, GUILD, MANAGER, "741", "742", "743", "744")

    # Two test accounts are broken; the third proves the code.
    action = interaction(MEMBER)
    await handler._handle_enter_code(action, "TRIPLE")
    assert action.followup.sent[0]["embed"].title == "✅ Code Accepted — Thank You!"
    assert handler._gift_code_service.calls == [(741, "TRIPLE"), (742, "TRIPLE"), (743, "TRIPLE")]
    assert pending_codes(db) == ["TRIPLE"]

    # When none of the three can use it, the code is not queued and nobody else is tried.
    handler._gift_code_service.responses[743] = invalid
    handler._gift_code_service.calls.clear()
    unsure = interaction(OTHER_MEMBER)
    await handler._handle_enter_code(unsure, "UNSURE")
    embed = unsure.followup.sent[0]["embed"]
    assert embed.title == "⚠️ Could Not Check the Code" and "was not queued" in embed.description
    assert len(handler._gift_code_service.calls) == 3
    assert pending_codes(db) == ["TRIPLE"]


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
    untouched = handler._build_auto_redemption_embed("CODE", 0, [], scope="kingdom 830", final=False)
    assert untouched.title == "🎁 New Gift Code — Redeeming…"
    assert "interrupted" in untouched.description and "finished" not in untouched.description.split("when it")[0]
    partly = handler._build_auto_redemption_embed(
        "CODE", 1, [{"status_category": "success"}], scope="kingdom 830", final=False
    )
    assert partly.title == "🎁 New Gift Code Redeemed!" and "continues automatically" in partly.description
    done = handler._build_auto_redemption_embed("CODE", 0, [], scope="kingdom 830", final=True)
    assert done.title == "ℹ️ Gift Code Finished Without New Rewards"


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
