"""Regression tests for the gift-code services, data layer and bot core audit."""

import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from db.models import CommandUsageDaily, GuildPlayerRegistration, RegisteredPlayer, User
from services import gift_code_service as gift_code_module
from services.gift_code_service import GiftCodeService
from services.gift_redemption_service import GiftRedemptionService
from services.player_registry_service import PlayerRegistryService
from services.usage_stats_service import UsageStatsService
from tests.sqlite_helpers import SqliteDb

REPO_ROOT = Path(__file__).resolve().parents[1]
GUILD_A, GUILD_B = 1001, 2002


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

    # A null gift-code list must still be a list: /giftcodes iterates it.
    _serve(monkeypatch, _FakeRequest(_FakeResponse(payload={"status": "success", "data": {"giftCodes": None}})))
    assert await GiftCodeService().get_available_gift_codes() == {"success": True, "data": []}

    _serve(monkeypatch, _FakeRequest(_FakeResponse(payload={"status": "success", "data": None})))
    assert await GiftCodeService().get_available_gift_codes() == {"success": True, "data": []}

    _serve(monkeypatch, _FakeRequest(_FakeResponse(payload=["not", "a", "dict"])))
    assert (await GiftCodeService().get_available_gift_codes())["success"] is False


# ----------------------------------------------------------- roster transactions


@pytest.fixture
def db():
    database = SqliteDb()
    with Session(database.engine) as session, session.begin():
        session.add_all([User(id=uid, username=f"user{uid}") for uid in (1, 2)])
    return database


def _seed(db, player_ids, registrations=()):
    with Session(db.engine) as session, session.begin():
        for player_id in player_ids:
            session.add(RegisteredPlayer(player_id=player_id, kingdom="830", enabled=True, added_by_user_id=1))
        session.flush()
        for guild_id, player_id in registrations:
            session.add(GuildPlayerRegistration(guild_id=guild_id, player_id=player_id, added_by_user_id=1))


@pytest.mark.asyncio
async def test_removing_a_roster_entry_runs_serializable(db):
    # The "last entry -> delete the shared profile" decision reads the other servers'
    # entries; under READ COMMITTED two concurrent removals both keep the profile, which
    # then keeps being auto-redeemed as an unassigned legacy profile.
    _seed(db, ["200"], registrations=[(GUILD_A, "200"), (GUILD_B, "200")])
    registry = PlayerRegistryService(db)

    first = await registry.remove_player_from_guild(GUILD_A, "200")
    assert first.removed and not first.profile_deleted
    assert db.isolation_levels[-1] == "SERIALIZABLE"

    last = await registry.remove_player_from_guild(GUILD_B, "200")
    assert last.removed and last.profile_deleted
    assert db.isolation_levels[-1] == "SERIALIZABLE"
    with Session(db.engine) as session:
        assert session.scalars(select(RegisteredPlayer)).all() == []


@pytest.mark.asyncio
async def test_a_repeated_uid_in_one_import_does_not_fail_the_whole_registration():
    # Upstream alliance rosters occasionally repeat a uid. The second row used to rename
    # the first row's new profile, so the first roster entry had no profile: an FK
    # violation (CockroachDB enforces it) that failed the import for everyone.
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    database = SqliteDb(engine)
    with Session(engine) as session, session.begin():
        session.add(User(id=1, username="user1"))

    outcome = await PlayerRegistryService(database).register_players_for_guild(
        GUILD_A,
        [
            {"player_id": "111", "player_uid": "U1", "kingdom": "830"},
            {"player_id": "222", "player_uid": "U1", "kingdom": "830"},
        ],
        added_by_user_id=1,
        max_total=300,
    )

    assert outcome.added == ["111", "222"]
    with Session(engine) as session:
        profiles = {p.player_id: p.player_uid for p in session.scalars(select(RegisteredPlayer))}
        entries = {(r.guild_id, r.player_id) for r in session.scalars(select(GuildPlayerRegistration))}
    assert profiles == {"111": "U1", "222": None}
    assert entries == {(GUILD_A, "111"), (GUILD_A, "222")}


@pytest.mark.asyncio
async def test_assigning_unassigned_profiles_checks_the_cap_serializable(db):
    _seed(db, ["100", "101", "102"], registrations=[(GUILD_A, "102")])
    registry = PlayerRegistryService(db)

    assigned, left = await registry.assign_unassigned_to_guild(GUILD_A, max_total=2)

    assert (assigned, left) == (1, 1)
    assert db.isolation_levels[-1] == "SERIALIZABLE"


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
