"""The shared gift-code list, kingdom rosters, permissions, resumable auto-redemption and job guards via the handler."""

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
from services.gift_redemption_service import FailingPlayer, RedemptionJob
from services.interaction_tracking_service import InteractionTrackingService
from services.player_info_service import PlayerLookupUnavailable
from services.player_registry_service import PlayerRegistryService
from tests.sqlite_helpers import SqliteDb

GUILD, OTHER_GUILD, NO_KINGDOM_GUILD = 1001, 2002, 3003
KINGDOM, OTHER_KINGDOM = 830, 795
# The kingdom each server chose in /settings. It decides which accounts of the shared
# list the server shows in its roster and gets summaries about.
SERVER_KINGDOMS = {GUILD: KINGDOM, OTHER_GUILD: OTHER_KINGDOM}
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


def make_handler(
    db, *, info=None, responses=None, guild_settings=(), auto_channels=(), channels=None, kingdoms=None,
):
    """``kingdoms`` is ``{guild_id: kingdom}`` (default ``SERVER_KINGDOMS``); it is read on
    every lookup, so a test can change a server's kingdom by changing the dict."""
    channels = channels or {}
    kingdoms = SERVER_KINGDOMS if kingdoms is None else kingdoms

    async def get_settings(guild_id):
        return SimpleNamespace(manager_role_id=None, kingdom_id=kingdoms.get(guild_id))

    bot = SimpleNamespace(
        tree=SimpleNamespace(get_command=lambda name: None),
        user=SimpleNamespace(id=BOT_ID, name="bot", discriminator="0", display_name="Bot"),
        get_channel=lambda channel_id: channels.get(channel_id),
    )
    config = SimpleNamespace(
        admin_user_ids={OPERATOR}, max_self_registered_players=2,
        auto_redeem_channels=set(auto_channels),
    )
    guild_config = SimpleNamespace(
        list_all=AsyncMock(return_value=list(guild_settings)),
        get_settings=AsyncMock(side_effect=get_settings),
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


_SERVER_KINGDOM = object()


async def register(handler, guild_id, owner, *player_ids, kingdom=_SERVER_KINGDOM):
    """Put accounts on the shared list as accounts of ``guild_id``'s kingdom.

    The list has no server column: the server only picks the stored kingdom, which is
    what puts an account on that server's roster. ``kingdom=None`` stores none.
    """
    if kingdom is _SERVER_KINGDOM:
        kingdom = SERVER_KINGDOMS.get(guild_id)
    stored = str(kingdom) if kingdom is not None else None
    return await handler._player_registry_service.register_players(
        [{"player_id": pid, "player_name": f"Gov {pid}", "kingdom": stored} for pid in player_ids],
        added_by_user_id=owner,
    )


def gift_server(guild_id, channel_id):
    """A server's settings row: its gift-code channel and its kingdom, if it chose one."""
    return SimpleNamespace(
        guild_id=guild_id, gift_code_channel_id=channel_id, kingdom_id=SERVER_KINGDOMS.get(guild_id),
    )


async def enabled_ids(handler):
    """Who a gift code is redeemed for."""
    return [player.player_id for player in await handler._player_registry_service.get_auto_redeem_targets()]


async def roster_ids(handler, kingdom):
    return [entry.player_id for entry in await handler._player_registry_service.list_kingdom_roster(kingdom)]


def field(embed, prefix):
    return next(f for f in embed.fields if f.name.startswith(prefix))


# ------------------------------------------------------------------ /gift add


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
    assert "roster of servers set to kingdom `830`" in embed.description
    assert "You have added 1/2 players." in embed.description
    # One list for every server: the account is redeemed for, and shows in its kingdom's roster.
    roster = await handler._player_registry_service.list_kingdom_roster(KINGDOM)
    assert [(e.player_id, e.added_by_user_id, e.enabled) for e in roster] == [("101", MEMBER, True)]
    assert await roster_ids(handler, OTHER_KINGDOM) == []
    assert await enabled_ids(handler) == ["101"]


@pytest.mark.asyncio
async def test_member_limit_counts_accounts_added_from_any_server_and_managers_have_none(db):
    info = FakePlayerInfo()
    handler = make_handler(db, info=info)
    await handler._handle_add_player_slash(interaction(MEMBER), "111")
    await handler._handle_add_player_slash(interaction(MEMBER, guild_id=OTHER_GUILD), "112")

    third = interaction(MEMBER, guild_id=NO_KINGDOM_GUILD)
    await handler._handle_add_player_slash(third, "113")

    embed = third.followup.sent[0]["embed"]
    assert embed.title == "❌ No Players Added"
    assert "`113`" in field(embed, "Not added — limit reached (1)").value
    assert "You have added 2/2 players." in embed.description
    assert info.calls == ["111", "112"]
    assert await handler._player_registry_service.count_owned(MEMBER) == 2

    # A manager adds a whole alliance: no personal limit, and no cap on a server's roster.
    manager = interaction(MANAGER, manage=True)
    await handler._handle_add_player_slash(manager, "121 122 123 124 125 126")

    embed = manager.followup.sent[0]["embed"]
    assert embed.title == "✅ Added 6 Player(s)"
    assert not any(f.name.startswith("Not added") for f in embed.fields)
    assert "You have added" not in embed.description
    assert await handler._player_registry_service.count_owned(MANAGER) == 6


@pytest.mark.asyncio
async def test_account_someone_else_added_is_already_on_the_list_and_keeps_its_owner(db):
    info = FakePlayerInfo()
    handler = make_handler(db, info=info)
    await register(handler, GUILD, MEMBER, "131")

    action = interaction(OTHER_MEMBER, guild_id=OTHER_GUILD)
    await handler._handle_add_player_slash(action, "131")

    embed = action.followup.sent[0]["embed"]
    assert embed.title == "ℹ️ Already on the Gift-Code List"
    assert "`131`" in field(embed, "Already on the list (1)").value
    assert info.calls == []  # a listed account is not looked up again
    assert (await handler._player_registry_service.get_entry("131")).added_by_user_id == MEMBER
    assert await handler._player_registry_service.count_owned(OTHER_MEMBER) == 0
    assert await enabled_ids(handler) == ["131"]


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
    player_ids = ",".join(ids + [f"bad{i}" * 10 for i in range(40)])

    member = interaction(MEMBER)
    await handler._handle_add_player_slash(member, player_ids)

    embed = member.followup.sent[0]["embed"]
    assert len(embed) < 6000
    assert all(len(f.value) <= 1024 for f in embed.fields)
    not_found = field(embed, "Not found (2)")  # the personal limit of 2 limits lookups
    assert "more" not in not_found.value
    over = field(embed, "Not added — limit reached (48)").value
    assert "…and" in over and over.endswith("more")
    assert field(embed, "Invalid (40)")
    assert field(embed, "Ignored (10)")

    # A manager has no limit: every ID one command accepts is looked up.
    manager = interaction(MANAGER, manage=True)
    await handler._handle_add_player_slash(manager, player_ids)

    embed = manager.followup.sent[0]["embed"]
    assert len(embed) < 6000
    assert all(len(f.value) <= 1024 for f in embed.fields)
    not_found = field(embed, "Not found (50)").value
    assert "…and" in not_found and not_found.endswith("more")
    assert not any(f.name.startswith("Not added") for f in embed.fields)
    assert field(embed, "Ignored (10)")


@pytest.mark.asyncio
async def test_addplayer_outside_a_server_is_denied(db):
    action = interaction(MEMBER, guild_id=None)
    await make_handler(db)._handle_add_player_slash(action, "101")
    assert action.response.sent[0]["embed"].title == "🔒 Not allowed"
    assert action.response.sent[0]["ephemeral"] is True


# --------------------------------------------------------------- /gift remove


@pytest.mark.asyncio
async def test_remove_is_denied_to_strangers_and_to_managers_of_other_kingdoms(db):
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "301")
    await register(handler, GUILD, MEMBER, "302", kingdom=None)

    refused = [
        (interaction(OTHER_MEMBER), "301"),  # a member of the account's kingdom who did not add it
        (interaction(MANAGER, guild_id=OTHER_GUILD, manage=True), "301"),  # manages another kingdom's server
        (interaction(MANAGER, guild_id=NO_KINGDOM_GUILD, manage=True), "301"),  # its server chose no kingdom
        # An account without a stored kingdom belongs to no server's roster, and "no
        # kingdom" on both sides is not a match.
        (interaction(MANAGER, manage=True), "302"),
        (interaction(MANAGER, guild_id=NO_KINGDOM_GUILD, manage=True), "302"),
    ]
    for action, player_id in refused:
        await handler._handle_remove_player_slash(action, player_id)
        reply = action.followup.sent[0]
        assert reply["embed"].title == "🔒 Not allowed" and reply["ephemeral"] is True
        assert "that server's kingdom" in reply["embed"].description
        assert "view" not in reply

    unknown = interaction(MANAGER, manage=True)
    await handler._handle_remove_player_slash(unknown, "399")
    assert unknown.followup.sent[0]["embed"].title == "❌ Player Not Found"
    assert await enabled_ids(handler) == ["301", "302"]


@pytest.mark.asyncio
async def test_owner_removes_an_account_from_any_server_and_it_leaves_the_shared_list(db):
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "311", "312")

    # The owner's right follows the account, not the server the command is run in.
    owner = interaction(MEMBER, guild_id=OTHER_GUILD)
    await handler._handle_remove_player_slash(owner, "311")
    prompt = owner.followup.sent[0]
    assert prompt["ephemeral"] is True and isinstance(prompt["view"], RemovePlayerConfirmView)
    assert "shared by every server" in prompt["embed"].description
    assert await roster_ids(handler, KINGDOM) == ["311", "312"]  # nothing yet

    click = interaction(MEMBER, guild_id=OTHER_GUILD)
    await prompt["view"].confirm_button.callback(click)
    assert click.response.deferred == {}  # acknowledged before the database write
    result = click.edit_original_response.call_args.kwargs
    assert result["embed"].title == "✅ Player Removed" and result["view"] is None
    assert "removed from the gift-code list" in result["embed"].description

    # The account itself is gone: off its kingdom's roster in every server, no longer redeemed for.
    assert await handler._player_registry_service.get_entry("311") is None
    assert await roster_ids(handler, KINGDOM) == ["312"]
    assert await enabled_ids(handler) == ["312"]
    with Session(db.engine) as session:
        assert session.scalars(select(RegisteredPlayer.player_id)).all() == ["312"]

    # A confirm prompt that outlived the account says so instead of failing.
    stale = interaction(MEMBER)
    await RemovePlayerConfirmView(handler, MEMBER, prompt["view"].entry).confirm_button.callback(stale)
    assert stale.edit_original_response.call_args.kwargs["embed"].title == "Already Removed"


@pytest.mark.asyncio
async def test_kingdom_managers_and_operators_remove_accounts_they_did_not_add(db):
    kingdoms = dict(SERVER_KINGDOMS)
    handler = make_handler(db, kingdoms=kingdoms)
    await register(handler, GUILD, MEMBER, "321", "322", "323")

    manager = interaction(MANAGER, manage=True)
    await handler._handle_remove_player_slash(manager, "321")
    click = interaction(MANAGER, manage=True)
    await manager.followup.sent[0]["view"].confirm_button.callback(click)
    assert click.edit_original_response.call_args.kwargs["embed"].title == "✅ Player Removed"
    assert await enabled_ids(handler) == ["322", "323"]

    # An operator needs neither Manage Server nor a server of the account's kingdom.
    operator = interaction(OPERATOR, guild_id=OTHER_GUILD)
    await handler._handle_remove_player_slash(operator, "322")
    click = interaction(OPERATOR, guild_id=OTHER_GUILD)
    await operator.followup.sent[0]["view"].confirm_button.callback(click)
    assert click.edit_original_response.call_args.kwargs["embed"].title == "✅ Player Removed"
    assert await enabled_ids(handler) == ["323"]

    # The confirm click checks again: the server moved to another kingdom in between.
    manager = interaction(MANAGER, manage=True)
    await handler._handle_remove_player_slash(manager, "323")
    kingdoms[GUILD] = OTHER_KINGDOM
    click = interaction(MANAGER, manage=True)
    await manager.followup.sent[0]["view"].confirm_button.callback(click)
    assert click.response.sent[0]["embed"].title == "🔒 Not allowed"
    click.edit_original_response.assert_not_called()
    assert await enabled_ids(handler) == ["323"]


# ---------------------------------------------------------------- /gift roster


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
    assert [e.player_id for e in view.roster] == ["401", "402"]  # this server's kingdom only
    assert view.health_button not in view.children
    assert [o.value for o in view.status_select.options] == ["all", "enabled", "disabled"]

    assert view.selected_player.player_id == "401" and not view.toggle_button.disabled
    await view.toggle_button.callback(interaction(MEMBER))
    assert view.selected_player.enabled is False and view.toggle_button.label == "Enable"
    assert await enabled_ids(handler) == ["402", "999"]

    view.player_select._values = ["1"]
    await view.player_select.callback(interaction(MEMBER))
    assert view.selected_player.player_id == "402" and view.toggle_button.disabled
    denied = interaction(MEMBER)
    await view.toggle_button.callback(denied)
    assert denied.response.sent[0]["embed"].title == "🔒 Not allowed"
    assert view.selected_player.enabled is True
    assert await enabled_ids(handler) == ["402", "999"]

    manager = interaction(MANAGER, manage=True)
    await handler._handle_list_players_slash(manager)
    manager_view = manager.followup.sent[0]["view"]
    assert manager_view.health_button in manager_view.children
    assert [e.player_id for e in manager_view.roster] == ["402", "401"]  # enabled first
    assert manager_view.selected_player.player_id == "402" and not manager_view.toggle_button.disabled
    await manager_view.toggle_button.callback(interaction(MANAGER, manage=True))
    assert await enabled_ids(handler) == ["999"]


@pytest.mark.asyncio
async def test_an_open_roster_stops_managing_a_kingdom_the_server_no_longer_has(db):
    kingdoms = {GUILD: KINGDOM}
    handler = make_handler(db, kingdoms=kingdoms)
    await register(handler, GUILD, MEMBER, "421", "422")

    manager = interaction(MANAGER, manage=True)
    await handler._handle_list_players_slash(manager)
    view = manager.followup.sent[0]["view"]
    kingdoms[GUILD] = OTHER_KINGDOM

    # The panel still shows kingdom 830, but this server manages 795 now.
    denied = interaction(MANAGER, manage=True)
    await view.toggle_button.callback(denied)
    assert denied.response.sent[0]["embed"].title == "🔒 Not allowed"

    health = RosterHealthView(handler, MANAGER, [SimpleNamespace(player_id="421"), SimpleNamespace(player_id="422")])
    await health.disable_button.callback(interaction(MANAGER, manage=True))
    assert await enabled_ids(handler) == ["421", "422"]

    # The owner of an account may always switch it.
    await view.toggle_button.callback(interaction(MEMBER))
    assert await enabled_ids(handler) == ["422"]


@pytest.mark.asyncio
async def test_roster_lists_the_accounts_of_the_servers_kingdom_whoever_added_them(db):
    handler = make_handler(db, kingdoms={GUILD: KINGDOM, OTHER_GUILD: OTHER_KINGDOM, NO_KINGDOM_GUILD: KINGDOM})
    await register(handler, GUILD, MEMBER, "411")
    await register(handler, GUILD, MANAGER, "412")
    await register(handler, OTHER_GUILD, MEMBER, "413")
    await register(handler, OTHER_GUILD, OTHER_MEMBER, "414")

    async def roster_of(guild_id):
        action = interaction(OTHER_MEMBER, guild_id=guild_id)
        await handler._handle_list_players_slash(action)
        reply = action.followup.sent[0]
        assert reply["ephemeral"] is True
        return reply["view"]

    view = await roster_of(GUILD)
    assert view.kingdom == "830"
    assert [(e.player_id, e.added_by_user_id) for e in view.roster] == [("411", MEMBER), ("412", MANAGER)]
    embed = view.build_embed()
    assert embed.title == "📋 Kingdom 830 Gift-Code Roster"
    assert "2 total · 2 enabled · 0 disabled" in embed.description
    assert "whoever added it" in embed.description

    # Every server set to the kingdom shows the same roster; another kingdom's server its own.
    assert [e.player_id for e in (await roster_of(NO_KINGDOM_GUILD)).roster] == ["411", "412"]
    other = await roster_of(OTHER_GUILD)
    assert other.kingdom == "795"
    assert [(e.player_id, e.added_by_user_id) for e in other.roster] == [("413", MEMBER), ("414", OTHER_MEMBER)]


@pytest.mark.asyncio
async def test_roster_of_a_server_without_a_kingdom_asks_which_kingdom(db):
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "421")

    member = interaction(MEMBER, guild_id=NO_KINGDOM_GUILD)
    await handler._handle_list_players_slash(member)
    reply = member.followup.sent[0]
    assert reply["embed"].title == "📋 Which Kingdom?"
    assert reply["ephemeral"] is True and "view" not in reply
    assert "Ask a server manager to set it in `/settings`." in reply["embed"].description
    assert "`/gift accounts`" in reply["embed"].description  # where the member's own accounts are

    manager = interaction(MANAGER, guild_id=NO_KINGDOM_GUILD, manage=True)
    await handler._handle_list_players_slash(manager)
    description = manager.followup.sent[0]["embed"].description
    assert manager.followup.sent[0]["embed"].title == "📋 Which Kingdom?"
    assert "Set it in `/settings`." in description and "Ask a server manager" not in description

    # A kingdom nobody registered an account of is a different, empty state.
    empty = interaction(MEMBER, guild_id=OTHER_GUILD)
    await handler._handle_list_players_slash(empty)
    assert empty.followup.sent[0]["embed"].title == "📋 No Players Yet"
    assert "kingdom `795`" in empty.followup.sent[0]["embed"].description


@pytest.mark.asyncio
async def test_roster_kingdom_parameter_is_for_bot_operators_only(db):
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "431")
    await register(handler, OTHER_GUILD, OTHER_MEMBER, "432")

    for action in (interaction(MEMBER), interaction(MANAGER, manage=True)):
        await handler._handle_list_players_slash(action, kingdom=OTHER_KINGDOM)
        denial = action.response.sent[0]
        assert denial["embed"].title == "🔒 Not allowed" and denial["ephemeral"] is True
        assert "Only bot operators" in denial["embed"].description
        assert action.followup.sent == []

    # The operator is in a server of kingdom 830 and holds no Manage Server there.
    operator = interaction(OPERATOR)
    await handler._handle_list_players_slash(operator, kingdom=OTHER_KINGDOM)
    view = operator.followup.sent[0]["view"]
    assert view.kingdom == "795" and [e.player_id for e in view.roster] == ["432"]
    assert view.health_button in view.children and not view.toggle_button.disabled
    await view.toggle_button.callback(interaction(OPERATOR))
    assert await enabled_ids(handler) == ["431"]

    # In a server without a kingdom the parameter is the operator's way in.
    elsewhere = interaction(OPERATOR, guild_id=NO_KINGDOM_GUILD)
    await handler._handle_list_players_slash(elsewhere, kingdom=KINGDOM)
    assert [e.player_id for e in elsewhere.followup.sent[0]["view"].roster] == ["431"]


@pytest.mark.asyncio
async def test_profiles_need_no_assignment_and_the_status_filter_narrows_the_roster(db):
    # Rows as an older bot left them: nothing ties them to a server but the stored kingdom.
    with Session(db.engine) as session, session.begin():
        session.add_all([
            RegisteredPlayer(player_id="501", player_name="Legacy", kingdom="830", enabled=True, added_by_user_id=MEMBER),
            RegisteredPlayer(player_id="502", player_name="Lookup", kingdom="830", enabled=False, added_by_user_id=MEMBER),
            RegisteredPlayer(player_id="503", player_name="Nowhere", enabled=True, added_by_user_id=MEMBER),
        ])
    handler = make_handler(db)

    action = interaction(OTHER_MEMBER)
    await handler._handle_list_players_slash(action)
    view = action.followup.sent[0]["view"]
    assert [e.player_id for e in view.roster] == ["501", "502"]
    assert "2 total · 1 enabled · 1 disabled" in view.build_embed().description
    assert [o.value for o in view.status_select.options] == ["all", "enabled", "disabled"]

    view.status_select._values = ["disabled"]
    await view.status_select.callback(interaction(OTHER_MEMBER))
    assert [e.player_id for e in view.filtered_players] == ["502"]
    assert view.selected_player.player_id == "502" and view.toggle_button.disabled  # not this member's account
    assert "**1 matches** · 2 total" in view.build_embed().description
    assert "Filter: **Disabled**" in view.build_embed().description

    # An account without a stored kingdom is on no roster, but codes are redeemed for it
    # and its owner still has it.
    assert await roster_ids(handler, None) == []
    assert await enabled_ids(handler) == ["501", "503"]
    owned = await handler._player_registry_service.list_owned_entries(MEMBER)
    assert [(e.player_id, e.kingdom) for e in owned] == [("501", "830"), ("502", "830"), ("503", None)]


@pytest.mark.asyncio
async def test_roster_health_reports_reach_and_disables_failing_players(db):
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "601", "602")
    await register(handler, OTHER_GUILD, MEMBER, "699")  # fails the same way in another kingdom
    with Session(db.engine) as session, session.begin():
        for index, code in enumerate(("NEW", "OLD")):
            session.add(GiftCode(id=index + 1, code=code, created_at_api=datetime(2026, 11, 2 - index, tzinfo=timezone.utc),
                                 auto_redeem_status="completed"))
            session.add(GiftCodeRedemption(user_id=BOT_ID, player_id="601", gift_code=code, success=True))
            for player_id in ("602", "699"):
                session.add(GiftCodeRedemption(user_id=BOT_ID, player_id=player_id, gift_code=code, success=False,
                                               error_code="INVALID_PLAYER_ID"))

    manager = interaction(MANAGER, manage=True)
    await handler._handle_list_players_slash(manager)
    view = manager.followup.sent[0]["view"]
    click = interaction(MANAGER, manage=True)
    await view.health_button.callback(click)
    report = click.followup.sent[0]
    assert report["embed"].title == "🩺 Roster Health"
    assert "`NEW` — reached **1**/2 · 1 failed" in report["embed"].description
    failing = report["embed"].fields[0].value
    assert "`602`" in failing and "`699`" not in failing  # the report covers this kingdom's roster
    health_view = report["view"]
    assert isinstance(health_view, RosterHealthView) and health_view.disable_button.label == "Disable these (1)"

    disable = interaction(MANAGER, manage=True)
    await health_view.disable_button.callback(disable)
    assert "Disabled 1 player(s)" in disable.followup.sent[0]["embed"].description
    assert await enabled_ids(handler) == ["601", "699"]

    member_click = interaction(MEMBER)
    await view.health_button.callback(member_click)
    assert member_click.response.sent[0]["embed"].title == "🔒 Not allowed"


# --------------------------------------------------------------------- /giftadmin redeem


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
async def test_redeem_covers_every_enabled_account_whichever_server_starts_it(db, monkeypatch):
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "801")
    await register(handler, OTHER_GUILD, OTHER_MEMBER, "802", "804")
    await register(handler, GUILD, MEMBER, "803", kingdom=None)
    await handler._player_registry_service.set_players_enabled(["804"], False)
    captured = []

    async def fake_job(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(handler, "_run_manual_redemption_job", fake_job)

    member = interaction(MEMBER)
    await handler._handle_redeem_gift_code_slash(member, "CODE")
    assert member.followup.sent[0]["embed"].title == "🔒 Not allowed"
    assert captured == []

    # A manager's run is not narrowed to the kingdom of the server it is started in.
    manager = interaction(MANAGER, manage=True)
    await handler._handle_redeem_gift_code_slash(manager, "CODE")
    await handler._jobs.get("CODE").task
    assert [p.player_id for p in captured[0]["registered_players"]] == ["801", "802", "803"]
    assert captured[0]["job"].total == 3 and captured[0]["job"].guild_id == GUILD
    assert "all 3 enabled players on the list" in manager.followup.sent[0]["embed"].description

    # Neither a server without a kingdom nor a bot operator gets a different run.
    unset = interaction(MANAGER, guild_id=NO_KINGDOM_GUILD, manage=True)
    await handler._handle_redeem_gift_code_slash(unset, "SECOND")
    await handler._jobs.get("SECOND").task
    operator = interaction(OPERATOR, guild_id=OTHER_GUILD)
    await handler._handle_redeem_gift_code_slash(operator, "THIRD")
    await handler._jobs.get("THIRD").task
    for run in captured[1:]:
        assert [p.player_id for p in run["registered_players"]] == ["801", "802", "803"]
    assert [run["guild_id"] for run in captured] == [GUILD, NO_KINGDOM_GUILD, OTHER_GUILD]


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
async def test_poll_resumes_pending_codes_completes_expired_ones_and_skips_busy_codes(db, monkeypatch, gift_cards):
    monkeypatch.setattr("discord.TextChannel", Channel)
    legacy, server_a, server_b = Channel(555, guild_id=None), Channel(777, GUILD), Channel(888, OTHER_GUILD)
    handler = make_handler(
        db,
        responses={902: {"success": False, "error_code": "INVALID_PLAYER_ID", "message": "Player ID does not exist."}},
        guild_settings=[gift_server(GUILD, 777), gift_server(OTHER_GUILD, 888)],
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

    # Global summary for the legacy channel; a server gets the summary of its kingdom,
    # and only where one of that kingdom's players succeeded.
    global_card, server_card = gift_cards
    assert len(legacy.sent) == 1 and global_card.stats.total == 3 and global_card.stats.counts["success"] == 2
    assert len(server_a.sent) == 1
    assert server_a.sent[0]["embed"].description == "Auto-redemption finished for kingdom 830."
    assert server_card.stats.total == 2
    assert server_b.sent == []  # its kingdom's only player failed


def three_servers():
    """Gift channels of a server per kingdom and of one that chose no kingdom."""
    channels = {777: Channel(777, GUILD), 888: Channel(888, OTHER_GUILD), 999: Channel(999, NO_KINGDOM_GUILD)}
    settings = [gift_server(GUILD, 777), gift_server(OTHER_GUILD, 888), gift_server(NO_KINGDOM_GUILD, 999)]
    return channels, settings


@pytest.mark.asyncio
async def test_auto_summaries_are_split_by_kingdom_and_a_server_without_one_gets_every_account(db, gift_cards):
    channels, settings = three_servers()
    handler = make_handler(
        db,
        responses={952: {"success": False, "error_code": "INVALID_PLAYER_ID", "message": "Player ID does not exist."}},
        guild_settings=settings,
        channels=channels,
    )
    await register(handler, GUILD, MEMBER, "951", "952")
    await register(handler, OTHER_GUILD, OTHER_MEMBER, "953")
    await register(handler, GUILD, MANAGER, "954", kingdom=111)  # no server follows this kingdom
    add_code(db, 1, "SPLIT")
    code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]

    assert await handler._run_auto_redemption(code_row) is True

    # The code went to everyone, whichever server the account was added from.
    assert sorted(str(player_id) for player_id, _ in handler._gift_code_service.calls) == ["951", "952", "953", "954"]
    summaries = {channel_id: channel.sent for channel_id, channel in channels.items()}
    assert [len(sent) for sent in summaries.values()] == [1, 1, 1]
    assert summaries[777][0]["embed"].description == "Auto-redemption finished for kingdom 830."
    assert summaries[888][0]["embed"].description == "Auto-redemption finished for kingdom 795."
    assert summaries[999][0]["embed"].description == "Auto-redemption finished for every registered player."
    # No legacy channel is configured, so the cards are the three servers' in settings order.
    assert [(card.stats.total, card.stats.counts["success"]) for card in gift_cards] == [(2, 1), (1, 1), (4, 3)]


@pytest.mark.asyncio
async def test_live_announcement_counts_each_servers_kingdom(db):
    channels, settings = three_servers()
    channels[666] = Channel(666, 4004)
    settings.append(SimpleNamespace(guild_id=4004, gift_code_channel_id=666, kingdom_id=111))
    handler = make_handler(db, guild_settings=settings, channels=channels)
    await register(handler, GUILD, MEMBER, "961", "962")
    await register(handler, OTHER_GUILD, OTHER_MEMBER, "963")
    targets = await handler._player_registry_service.get_auto_redeem_targets()

    await handler._announce_auto_progress("LIVE", targets, [{"player_id": "961", "status_category": "success"}])

    def live(channel_id):
        (message,) = channels[channel_id].sent
        return message["embed"]

    assert "Redeeming for kingdom 830: **1/2** players processed." in live(777).description
    assert "Redeeming for kingdom 795: **0/1** players processed." in live(888).description
    assert field(live(888), "Results so far").value == "Waiting for this roster's turn."
    assert "Redeeming for every registered player: **1/3** players processed." in live(999).description
    assert channels[666].sent == []  # nobody of its kingdom is on the list


@pytest.mark.asyncio
async def test_paused_accounts_are_reported_to_the_servers_of_their_kingdom(db):
    channels, settings = three_servers()
    handler = make_handler(db, guild_settings=settings, channels=channels)
    operator_dm = SimpleNamespace(send=AsyncMock())
    handler._bot.get_user = lambda user_id: operator_dm if user_id == OPERATOR else None
    await register(handler, GUILD, MEMBER, "971")
    await register(handler, OTHER_GUILD, OTHER_MEMBER, "972")
    targets = await handler._player_registry_service.get_auto_redeem_targets()
    failing = [
        FailingPlayer("971", "Gov 971", 3, "INVALID_PLAYER_ID"),
        FailingPlayer("972", "Gov 972", 3, "KINGDOM_MISMATCH"),
    ]

    await handler._notify_disabled_accounts(failing, handler._kingdoms_of(targets))

    def notice(channel_id):
        (message,) = channels[channel_id].sent
        return message["embed"].description

    assert "(kingdom 830)" in notice(777) and "`971`" in notice(777) and "`972`" not in notice(777)
    assert "(kingdom 795)" in notice(888) and "`972`" in notice(888) and "`971`" not in notice(888)
    assert "(every registered player)" in notice(999) and "`971`" in notice(999) and "`972`" in notice(999)
    report = operator_dm.send.await_args.kwargs["embed"].description
    assert "(every kingdom)" in report and "`971`" in report and "`972`" in report


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
    """The auto poller can claim the code while /giftadmin redeem awaits the database."""
    handler = make_handler(db)
    await register(handler, GUILD, MEMBER, "711")
    original_targets = handler._player_registry_service.get_auto_redeem_targets

    async def targets_while_auto_claims(*args, **kwargs):
        handler._jobs.claim(RedemptionJob(gift_code="RACE", kind="auto"))
        return await original_targets(*args, **kwargs)

    monkeypatch.setattr(handler._player_registry_service, "get_auto_redeem_targets", targets_while_auto_claims)
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


@pytest.mark.asyncio
async def test_retry_pass_edits_the_first_announcement_instead_of_posting_again(db, gift_cards):
    outage = {"success": False, "error_code": "API_ERROR", "message": "Service unavailable"}
    responses = {942: outage}
    channel = Channel(channel_id=789, guild_id=GUILD)
    handler = make_handler(
        db,
        responses=responses,
        guild_settings=[gift_server(GUILD, 789)],
        channels={789: channel},
    )
    handler._redemption.REDEEM_RETRY_DELAY_SECONDS = 0
    handler._redemption.REDEEM_RETRY_MAX_DELAY_SECONDS = 0
    await register(handler, GUILD, MEMBER, "941", "942")
    add_code(db, 8, "WOBBLY")
    posted = SimpleNamespace(edit=AsyncMock())

    async def send(**kwargs):
        channel.sent.append(kwargs)
        return posted

    channel.send = send

    code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]
    assert await handler._run_auto_redemption(code_row) is False

    # The first pass posts at once and says the summary is still being updated.
    assert len(channel.sent) == 1
    first = channel.sent[0]["embed"]
    assert "retried automatically" in first.description
    assert gift_cards[0].stats.counts["success"] == 1 and gift_cards[0].title == "Redeeming · retrying 1"

    responses.pop(942)
    code_row = (await handler._gift_code_service.list_pending_gift_codes())[0]
    assert await handler._run_auto_redemption(code_row) is True

    # The final pass edits that message rather than posting a second summary.
    assert len(channel.sent) == 1
    posted.edit.assert_awaited_once()
    final = posted.edit.await_args.kwargs["embed"]
    assert final.description == "Auto-redemption finished for kingdom 830."
    assert gift_cards[-1].stats.counts["success"] == 2 and gift_cards[-1].title == "Gift code redeemed"
    assert handler._auto_announcements == {}
