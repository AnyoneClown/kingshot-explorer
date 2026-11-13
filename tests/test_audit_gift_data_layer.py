"""Regression tests for the gift-code services, data layer and bot core audit."""

import asyncio
import importlib.util
import json
import logging
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from db.models import CommandUsageDaily, GiftCodeRedemption, RegisteredPlayer, User
from services import gift_code_service as gift_code_module
from services.gift_code_service import GiftCodeService
from services.gift_redemption_service import GiftRedemptionService
from services.player_registry_service import PlayerRegistryService
from services.usage_stats_service import UsageStatsService
from tests.sqlite_helpers import SqliteDb

REPO_ROOT = Path(__file__).resolve().parents[1]
GUILD_A, GUILD_B = 1001, 2002
SHARED_LIST_MIGRATION = REPO_ROOT / "alembic" / "versions" / "20261114120000_share_gift_code_list_across_servers.py"


# ------------------------------------------------- gift-code list (kingshot.net)


class _FakeResponse:
    def __init__(self, *, status=200, payload=None, error=None):
        self.status = status
        self._payload = payload
        self._error = error

    async def json(self):
        if self._error is not None:
            raise self._error
        return self._payload


class _FakeRequest:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error

    async def __aenter__(self):
        if self._error is not None:
            raise self._error
        return self._response

    async def __aexit__(self, *exc):
        return False


class _FakeClientSession:
    def __init__(self, request):
        self._request = request

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, timeout=None):
        return self._request


def _serve(monkeypatch, request):
    monkeypatch.setattr(gift_code_module.aiohttp, "ClientSession", lambda *a, **k: _FakeClientSession(request))


@pytest.mark.asyncio
async def test_gift_code_list_timeout_is_a_network_failure_not_an_unexpected_error(monkeypatch, caplog):
    # aiohttp's ClientTimeout(total=...) raises a bare TimeoutError, which is not an
    # aiohttp.ClientError: it used to be logged as "Unexpected error" with a traceback.
    _serve(monkeypatch, _FakeRequest(error=asyncio.TimeoutError()))

    with caplog.at_level(logging.WARNING, logger=gift_code_module.__name__):
        result = await GiftCodeService().get_available_gift_codes()

    assert result == {"success": False, "message": "Gift code API timed out"}
    assert not [record for record in caplog.records if record.exc_info]
    assert "Unexpected error" not in caplog.text


@pytest.mark.asyncio
async def test_gift_code_list_with_unreadable_or_odd_payload_never_crashes(monkeypatch):
    _serve(monkeypatch, _FakeRequest(_FakeResponse(error=json.JSONDecodeError("Expecting value", "<html>", 0))))
    result = await GiftCodeService().get_available_gift_codes()
    assert result == {"success": False, "message": "Gift code API returned an invalid response"}

    # A null gift-code list must still be a list: the active-codes view iterates it.
    _serve(monkeypatch, _FakeRequest(_FakeResponse(payload={"status": "success", "data": {"giftCodes": None}})))
    assert await GiftCodeService().get_available_gift_codes() == {"success": True, "data": []}

    _serve(monkeypatch, _FakeRequest(_FakeResponse(payload={"status": "success", "data": None})))
    assert await GiftCodeService().get_available_gift_codes() == {"success": True, "data": []}

    _serve(monkeypatch, _FakeRequest(_FakeResponse(payload=["not", "a", "dict"])))
    assert (await GiftCodeService().get_available_gift_codes())["success"] is False


# ------------------------------------------------------- gift-code list transactions


@pytest.fixture
def db():
    database = SqliteDb()
    with Session(database.engine) as session, session.begin():
        session.add_all([User(id=uid, username=f"user{uid}") for uid in (1, 2)])
    return database


def _seed(db, player_ids):
    with Session(db.engine) as session, session.begin():
        for player_id in player_ids:
            session.add(RegisteredPlayer(player_id=player_id, kingdom="830", enabled=True, added_by_user_id=1))


@pytest.mark.asyncio
async def test_removing_an_account_deletes_its_profile_so_nothing_is_left_to_redeem(db):
    # With per-server rosters a removal had to decide whether another server still
    # listed the player, and a wrong decision left a profile that kept being
    # auto-redeemed. The list is shared now: a removal deletes the one profile, in one
    # transaction, and there is nothing left to redeem for.
    _seed(db, ["200", "201"])
    registry = PlayerRegistryService(db)

    first = await registry.remove_player("200")
    assert first.removed and first.entry.player_id == "200"
    assert len(db.transactions) == 1
    with Session(db.engine) as session:
        assert [player.player_id for player in session.scalars(select(RegisteredPlayer))] == ["201"]
    assert [player.player_id for player in await registry.get_auto_redeem_targets()] == ["201"]

    # A second removal (two managers confirming at once) finds nothing and changes nothing.
    again = await registry.remove_player("200")
    assert not again.removed and again.entry is None
    assert [player.player_id for player in await registry.get_auto_redeem_targets()] == ["201"]


@pytest.mark.asyncio
async def test_a_repeated_uid_in_one_import_does_not_fail_the_whole_registration():
    # Upstream alliance rosters occasionally repeat a uid. The second row used to rename
    # the first row's new profile, so the first Governor ID was left without one: with
    # per-server rosters that was an FK violation that failed the import for everyone,
    # and on the shared list it would report an account as added that is not listed.
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    database = SqliteDb(engine)
    with Session(engine) as session, session.begin():
        session.add(User(id=1, username="user1"))
    registry = PlayerRegistryService(database)

    outcome = await registry.register_players(
        [
            {"player_id": "111", "player_uid": "U1", "kingdom": "830"},
            {"player_id": "222", "player_uid": "U1", "kingdom": "830"},
        ],
        added_by_user_id=1,
    )

    assert outcome.added == ["111", "222"]
    with Session(engine) as session:
        profiles = {p.player_id: p.player_uid for p in session.scalars(select(RegisteredPlayer))}
    assert profiles == {"111": "U1", "222": None}
    assert [player.player_id for player in await registry.get_auto_redeem_targets()] == ["111", "222"]
    assert [entry.player_id for entry in await registry.list_owned_entries(1)] == ["111", "222"]


@pytest.mark.asyncio
async def test_adding_accounts_checks_the_personal_cap_serializable(db):
    # The cap is read and the rows are inserted by the same transaction; under READ
    # COMMITTED two additions by one member both read the old count and overshoot it.
    _seed(db, ["100", "101"])
    registry = PlayerRegistryService(db)

    outcome = await registry.register_players(
        [{"player_id": "102", "kingdom": "830"}, {"player_id": "103", "kingdom": "830"}],
        added_by_user_id=1,
        max_owned=3,
    )

    assert (outcome.added, outcome.over_limit) == (["102"], ["103"])
    assert await registry.count_owned(1) == 3
    assert db.isolation_levels[-1] == "SERIALIZABLE"
    # A unique violation (the same Governor ID added from two servers at once) is retried.
    assert db.transactions[-1] is True


# ------------------------------------------------------------------- auto-pause


class _CodesService:
    def __init__(self, recent, rows):
        self._recent = recent
        self._rows = rows
        self.requested_codes = None

    async def get_recent_gift_codes(self, limit):
        return [SimpleNamespace(code=code) for code in self._recent[:limit]]

    async def get_redemption_rows(self, gift_codes, player_ids):
        self.requested_codes = list(gift_codes)
        return [row for row in self._rows if row.gift_code in gift_codes]


def _failure(code, player_id="7"):
    return SimpleNamespace(gift_code=code, player_id=player_id, success=False, error_code="INVALID_PLAYER_ID")


@pytest.mark.asyncio
async def test_auto_pause_window_is_the_last_five_codes_including_the_current_one():
    # The current code is older than the five newest ones (resumed late). It used to be
    # prepended, so six codes were counted and C4+C5 completed "3 of the last 5".
    recent = ["C1", "C2", "C3", "C4", "C5"]
    gift = _CodesService(recent, [_failure("C0"), _failure("C4"), _failure("C5")])
    service = GiftRedemptionService(gift, tracking_service=None)
    players = [SimpleNamespace(player_id="7", player_name="Seven")]

    failing = await service.find_broken_players(players, current_code="C0", code_limit=5, min_failures=3)

    assert failing == []
    assert gift.requested_codes == ["C0", "C1", "C2", "C3", "C4"]

    # Three failures inside the window still pause the account.
    gift = _CodesService(recent, [_failure("C0"), _failure("C1"), _failure("C2")])
    service = GiftRedemptionService(gift, tracking_service=None)
    failing = await service.find_broken_players(players, current_code="C0", code_limit=5, min_failures=3)
    assert [player.player_id for player in failing] == ["7"]


# ---------------------------------------------------------------- usage counters


@pytest.mark.asyncio
async def test_cancelled_usage_flush_keeps_the_counts_for_the_final_flush():
    # Shutdown cancels the 5-minute flush loop; a flush cancelled mid-write used to drop
    # the counts it had already taken out of the buffer.
    started = asyncio.Event()

    class SlowDb:
        async def run_in_transaction(self, operation, **_):
            started.set()
            await asyncio.sleep(10)

    stats = UsageStatsService(SlowDb())
    stats.record("redeem", 5)
    stats.record("redeem", 5)
    task = asyncio.create_task(stats.flush())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert stats.buffered == 2

    db = SqliteDb()
    stats._db_manager = db
    assert await stats.flush() == 2
    with Session(db.engine) as session:
        row = session.scalars(select(CommandUsageDaily)).one()
    assert (row.guild_id, row.command_name, row.uses) == (5, "redeem", 2)
    assert isinstance(row.usage_date, date)


# ----------------------------------------------------------------------- alembic


def test_alembic_accepts_a_database_url_with_percent_encoded_characters():
    # ConfigParser interpolation rejected a "%" (for example a password with %40) and
    # the migration job crashed before running anything. Offline (--sql) mode renders
    # the first migration without connecting to any database.
    env = {**os.environ, "COCKROACHDB_URL": "postgresql://user:p%40ss@localhost:1/db"}
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "14e0e20140cd", "--sql"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    assert "CREATE TABLE users" in completed.stdout



def _database_with_server_rosters(profiles, registrations, attempted=()):
    """A database from before the shared list.

    ``profiles`` rows are ``(player_id, enabled, owner)``, ``registrations`` rows
    ``(guild_id, player_id, enabled, owner)`` of the per-server roster table, listed
    oldest first. ``attempted`` are the players some gift code was already tried for.
    """
    database = SqliteDb()
    table = sa.Table(
        "guild_player_registrations",
        sa.MetaData(),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("player_id", sa.String(255), sa.ForeignKey(RegisteredPlayer.player_id), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("added_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("guild_id", "player_id"),
    )
    table.create(database.engine)
    with Session(database.engine) as session, session.begin():
        session.add_all([User(id=uid, username=f"user{uid}") for uid in (1, 2)])
        session.add_all(
            RegisteredPlayer(player_id=player_id, kingdom="830", enabled=enabled, added_by_user_id=owner)
            for player_id, enabled, owner in profiles
        )
        session.add_all(
            GiftCodeRedemption(user_id=1, player_id=player_id, gift_code="OLDCODE", success=False)
            for player_id in attempted
        )
        session.flush()
        session.execute(
            table.insert(),
            [
                {
                    "guild_id": guild_id, "player_id": player_id, "enabled": enabled, "added_by_user_id": owner,
                    "created_at": position,
                }
                for position, (guild_id, player_id, enabled, owner) in enumerate(registrations)
            ],
        )
    return database


def _share_the_list(database):
    """Run the migration that replaces the per-server rosters with the shared list."""
    spec = importlib.util.spec_from_file_location("share_gift_code_list_migration", SHARED_LIST_MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with database.engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration.upgrade()


def test_sharing_the_list_keeps_the_set_of_redeemed_accounts():
    # Before the migration a code was redeemed for a profile that was enabled itself and
    # either enabled in at least one server roster or listed by no server at all (legacy).
    # Afterwards only the profile's own flag is left, so it has to say the same.
    database = _database_with_server_rosters(
        profiles=[
            ("100", True, 1),   # legacy: no server listed it -> still redeemed
            ("200", True, 1),   # enabled in both servers -> redeemed, once
            ("300", True, 1),   # listed, but disabled in every server -> no longer redeemed
            ("400", False, 1),  # paused itself although a server has it enabled -> stays paused
            ("500", False, 1),  # disabled legacy that codes were tried for -> stays disabled
            ("600", True, 1),   # disabled in one server, enabled in the other -> redeemed
            ("700", False, 2),  # only cached by a /player lookup: on no roster, never tried -> gone
        ],
        registrations=[
            (GUILD_A, "200", True, 1), (GUILD_B, "200", True, 2),
            (GUILD_A, "300", False, 1), (GUILD_B, "300", False, 2),
            (GUILD_A, "400", True, 1),
            (GUILD_A, "600", False, 1), (GUILD_B, "600", True, 2),
        ],
        attempted=["500"],
    )
    registry = PlayerRegistryService(database)

    _share_the_list(database)

    assert not sa.inspect(database.engine).has_table("guild_player_registrations")
    assert [player.player_id for player in asyncio.run(registry.get_auto_redeem_targets())] == ["100", "200", "600"]
    # No listed account is taken off: paused ones can be switched on again in /giftadmin roster.
    # Only the lookup cache goes, which was never on a list and would now look like one
    # more paused account of the member who looked the player up.
    everyone = asyncio.run(registry.get_registered_players(enabled_only=False))
    assert [player.player_id for player in everyone] == ["100", "200", "300", "400", "500", "600"]
    assert asyncio.run(registry.count_owned(2)) == 0

    # Running it against a database that is already migrated changes nothing.
    _share_the_list(database)
    assert [player.player_id for player in asyncio.run(registry.get_auto_redeem_targets())] == ["100", "200", "600"]


def test_sharing_the_list_keeps_the_member_who_listed_an_account_as_its_owner():
    # User 2 looked the player up (that cached the profile under their name); user 1 then
    # added it to their server's roster and was the one who could remove or pause it.
    database = _database_with_server_rosters(
        profiles=[("700", True, 2)],
        registrations=[(GUILD_A, "700", True, 1)],
    )
    registry = PlayerRegistryService(database)

    _share_the_list(database)

    assert [entry.player_id for entry in asyncio.run(registry.list_owned_entries(1))] == ["700"]
    assert asyncio.run(registry.count_owned(2)) == 0


def test_sharing_the_list_keeps_an_owner_who_listed_the_account_among_several_servers():
    database = _database_with_server_rosters(
        profiles=[
            ("710", True, 2),  # listed by 1 first and by its profile owner 2 later -> stays with 2
            ("720", True, 2),  # the profile owner never listed it; 1 did first, 3 later -> goes to 1
            ("730", True, 2),  # legacy, no server listed it -> stays with 2
            ("740", True, 2),  # listed only by someone the users table does not know -> stays with 2
        ],
        registrations=[
            (GUILD_A, "710", True, 1), (GUILD_B, "710", True, 2),
            (GUILD_B, "720", True, 1), (GUILD_A, "720", True, 3),
            (GUILD_A, "740", True, 404),
        ],
    )
    with Session(database.engine) as session, session.begin():
        session.add(User(id=3, username="user3"))
    registry = PlayerRegistryService(database)

    _share_the_list(database)

    owners = {
        player.player_id: player.added_by_user_id
        for player in asyncio.run(registry.get_registered_players(enabled_only=False))
    }
    assert owners == {"710": 2, "720": 1, "730": 2, "740": 2}
