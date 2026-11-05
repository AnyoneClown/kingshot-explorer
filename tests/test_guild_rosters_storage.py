"""Roster, target-selection and gift-code state queries against a real (SQLite) database."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from db.models import (
    GiftCode,
    GiftCodeRedemption,
    GuildPlayerRegistration,
    RegisteredPlayer,
    TranslationLog,
    User,
)
from repositories.discord_repositories import GiftCodeRedemptionRepository, GiftCodeRepository
from services.gift_code_service import GiftCodeService
from services.gift_redemption_service import FINAL_PLAYER_ERROR_CODES, GiftRedemptionService
from services.interaction_tracking_service import InteractionTrackingService
from services.player_registry_service import PlayerRegistryService
from tests.sqlite_helpers import SqliteDb

GUILD_A, GUILD_B = 1001, 2002


@pytest.fixture
def db():
    database = SqliteDb()
    with Session(database.engine) as session, session.begin():
        session.add_all([User(id=uid, username=f"user{uid}") for uid in (1, 2, 3, 99)])
    return database


def seed(db, *players, registrations=()):
    with Session(db.engine) as session, session.begin():
        for player_id, enabled in players:
            session.add(RegisteredPlayer(player_id=player_id, player_name=f"P{player_id}", kingdom="830",
                                         enabled=enabled, added_by_user_id=1))
        session.flush()
        for guild_id, player_id, enabled, owner in registrations:
            session.add(GuildPlayerRegistration(guild_id=guild_id, player_id=player_id, enabled=enabled,
                                                added_by_user_id=owner))


def profile(player_id, **extra):
    return {"player_id": player_id, "player_name": f"Name {player_id}", "kingdom": "830", **extra}


@pytest.mark.asyncio
async def test_auto_redeem_targets_include_legacy_and_enabled_registrations_once(db):
    seed(
        db,
        ("100", True),   # legacy: no registration -> included
        ("200", True),   # enabled in A and B -> included once
        ("300", True),   # only disabled registrations -> excluded
        ("400", False),  # paused globally -> excluded even though enabled in A
        ("500", False),  # disabled legacy -> excluded
        ("600", True),   # enabled in B, disabled in A -> included
        registrations=[
            (GUILD_A, "200", True, 1), (GUILD_B, "200", True, 2),
            (GUILD_A, "300", False, 1),
            (GUILD_A, "400", True, 1),
            (GUILD_A, "600", False, 1), (GUILD_B, "600", True, 2),
        ],
    )
    registry = PlayerRegistryService(db)

    targets = [player.player_id for player in await registry.get_auto_redeem_targets()]
    assert targets == ["100", "200", "600"]

    assert [p.player_id for p in await registry.get_guild_redeem_targets(GUILD_A)] == ["200"]
    assert [p.player_id for p in await registry.get_guild_redeem_targets(GUILD_B)] == ["200", "600"]
    assert await registry.get_enabled_guilds_for_players(["200", "300", "600"]) == {
        "200": {GUILD_A, GUILD_B},
        "600": {GUILD_B},
    }


@pytest.mark.asyncio
async def test_rosters_are_scoped_per_server_and_unassigned_lists_legacy(db):
    seed(db, ("100", True), ("200", True), ("300", False),
         registrations=[(GUILD_A, "200", False, 2), (GUILD_B, "200", True, 3)])
    registry = PlayerRegistryService(db)

    roster_a = await registry.list_guild_roster(GUILD_A)
    assert [(e.player_id, e.enabled, e.added_by_user_id, e.guild_id) for e in roster_a] == [("200", False, 2, GUILD_A)]
    roster_b = await registry.list_guild_roster(GUILD_B)
    assert [(e.player_id, e.enabled, e.added_by_user_id) for e in roster_b] == [("200", True, 3)]

    unassigned = await registry.list_unassigned_players()
    assert [(e.player_id, e.enabled, e.unassigned) for e in unassigned] == [("100", True, True), ("300", False, True)]


@pytest.mark.asyncio
async def test_registration_respects_personal_and_server_caps(db):
    seed(db, ("900", True), registrations=[(GUILD_A, "900", True, 2)])
    registry = PlayerRegistryService(db)

    outcome = await registry.register_players_for_guild(
        GUILD_A,
        [profile("101"), profile("102"), profile("900"), profile("103"), profile("101")],
        added_by_user_id=1,
        max_total=300,
        max_owned=2,
    )
    assert outcome.added == ["101", "102"]
    assert outcome.already_registered == ["900"]
    assert outcome.over_limit == ["103"]
    assert outcome.limit_kind == "personal"
    assert outcome.roster_size == 3
    # Writes go through the retried transaction helper (read-then-insert upsert).
    assert db.transactions[-1] is True

    # Another user hits the server cap (4) instead of a personal one.
    outcome = await registry.register_players_for_guild(
        GUILD_A, [profile("104"), profile("105")], added_by_user_id=3, max_total=4, max_owned=3
    )
    assert outcome.added == ["104"] and outcome.over_limit == ["105"] and outcome.limit_kind == "server"

    # The shared profile is created once and reused by another server.
    outcome = await registry.register_players_for_guild(
        GUILD_B, [profile("101")], added_by_user_id=3, max_total=300
    )
    assert outcome.added == ["101"]
    with Session(db.engine) as session:
        assert session.scalars(select(RegisteredPlayer).where(RegisteredPlayer.player_id == "101")).one().added_by_user_id == 1
    assert await registry.count_guild_registrations(GUILD_A, owner_id=1) == (4, 2)


@pytest.mark.asyncio
async def test_existing_disabled_entry_is_reenabled_only_on_request(db):
    seed(db, ("200", True), registrations=[(GUILD_A, "200", False, 2)])
    registry = PlayerRegistryService(db)

    outcome = await registry.register_players_for_guild(GUILD_A, [profile("200")], added_by_user_id=1, max_total=300)
    assert outcome.reenabled == [] and (await registry.list_guild_roster(GUILD_A))[0].enabled is False

    outcome = await registry.register_players_for_guild(
        GUILD_A, [profile("200")], added_by_user_id=1, max_total=300, enable_existing=True
    )
    assert outcome.reenabled == ["200"] and (await registry.list_guild_roster(GUILD_A))[0].enabled is True


@pytest.mark.asyncio
async def test_new_profiles_are_enabled_even_when_a_lookup_created_them_disabled(db):
    seed(db, ("777", False))  # e.g. created by a /stats lookup
    registry = PlayerRegistryService(db)
    await registry.register_players_for_guild(GUILD_A, [profile("777")], added_by_user_id=1, max_total=300)
    assert [p.player_id for p in await registry.get_guild_redeem_targets(GUILD_A)] == ["777"]


@pytest.mark.asyncio
async def test_removal_is_per_server_and_deletes_profile_after_last_registration(db):
    seed(db, ("200", True), registrations=[(GUILD_A, "200", True, 2), (GUILD_B, "200", True, 3)])
    registry = PlayerRegistryService(db)

    first = await registry.remove_player_from_guild(GUILD_A, "200")
    assert first.removed and not first.profile_deleted
    assert [e.player_id for e in await registry.list_guild_roster(GUILD_B)] == ["200"]

    missing = await registry.remove_player_from_guild(GUILD_A, "200")
    assert not missing.removed

    last = await registry.remove_player_from_guild(GUILD_B, "200")
    assert last.removed and last.profile_deleted
    # The profile is gone, so it cannot turn into an auto-redeemed legacy player.
    assert await registry.get_auto_redeem_targets() == []
    assert await registry.list_unassigned_players() == []


@pytest.mark.asyncio
async def test_toggle_and_assign_unassigned_within_cap(db):
    seed(db, ("100", True), ("101", True), ("102", False), ("200", True),
         registrations=[(GUILD_A, "200", True, 2)])
    registry = PlayerRegistryService(db)

    assert await registry.set_guild_players_enabled(GUILD_A, ["200", "999"], False) == 1
    assert (await registry.list_guild_roster(GUILD_A))[0].enabled is False
    assert await registry.set_profile_enabled("102", True) is True

    assigned, left = await registry.assign_unassigned_to_guild(GUILD_A, max_total=3)
    assert (assigned, left) == (2, 1)
    roster = {e.player_id: e for e in await registry.list_guild_roster(GUILD_A)}
    assert set(roster) == {"100", "101", "200"}
    assert roster["100"].added_by_user_id == 1  # the original owner keeps control
    assert [e.player_id for e in await registry.list_unassigned_players()] == ["102"]


@pytest.mark.asyncio
async def test_new_codes_are_pending_until_completed_and_duplicate_strings_are_tolerated(db):
    service = GiftCodeService(db)
    created = datetime(2026, 11, 1, tzinfo=timezone.utc)

    is_new, code = await service.add_or_update_gift_code(1, "NEWCODE", created)
    assert is_new and code.auto_redeem_status == "pending"
    is_new, _ = await service.add_or_update_gift_code(1, "NEWCODE", created, expires_at=created + timedelta(days=2))
    assert not is_new
    # Same code string re-issued under another upstream ID: no unique violation.
    is_new, _ = await service.add_or_update_gift_code(2, "NEWCODE", created)
    assert not is_new

    with Session(db.engine) as session, session.begin():
        # Rows written without a status (all codes from before the column) use the
        # server default and are never re-run.
        session.execute(text("INSERT INTO gift_codes (id, code, created_at_api) VALUES (3, 'OLD', '2026-11-01')"))

    assert [row.code for row in await service.list_pending_gift_codes()] == ["NEWCODE"]
    assert await service.mark_gift_code_completed(1)
    assert await service.list_pending_gift_codes() == []
    with Session(db.engine) as session:
        stored = session.get(GiftCode, 1)
        assert stored.auto_redeem_status == "completed" and stored.auto_redeem_completed_at is not None
        assert session.get(GiftCode, 3).auto_redeem_status == "completed"


@pytest.mark.asyncio
async def test_recorded_outcomes_pick_the_best_row_and_successful_lookup_tolerates_duplicates(db):
    rows = [
        dict(player_id="1", success=False, error_code="RATE_LIMITED"),      # transient: not settled
        dict(player_id="2", success=False, error_code="INVALID_PLAYER_ID"),  # final failure
        dict(player_id="3", success=True, error_code=None),
        dict(player_id="3", success=False, error_code="ALREADY_REDEEMED_BY_API"),
        dict(player_id="3", success=True, error_code=None),                 # duplicate success rows
        dict(player_id="4", success=False, error_code="ALREADY_REDEEMED_BY_API"),
    ]
    with Session(db.engine) as session, session.begin():
        session.add_all(GiftCodeRedemption(user_id=99, gift_code="CODE", **row) for row in rows)
        session.add(GiftCodeRedemption(user_id=99, gift_code="OTHER", player_id="1", success=True))

    service = GiftRedemptionService(GiftCodeService(db), tracking_service=None)
    outcomes = await service.load_recorded_outcomes("CODE")
    assert {pid: (row.success, row.error_code) for pid, row in outcomes.items()} == {
        "2": (False, "INVALID_PLAYER_ID"),
        "3": (True, None),
        "4": (False, "ALREADY_REDEEMED_BY_API"),
    }
    assert "INVALID_PLAYER_ID" in FINAL_PLAYER_ERROR_CODES

    async with db.session() as session:
        found = await GiftCodeRedemptionRepository(session).find_successful_redemption(3, "CODE")
        assert found is not None and found.success
        assert [c.code for c in await GiftCodeRepository(session).list_recent(5)] == []


@pytest.mark.asyncio
async def test_purge_old_translation_logs_deletes_in_batches(db, monkeypatch):
    now = datetime.now(timezone.utc)
    with Session(db.engine) as session, session.begin():
        for index in range(7):
            session.add(TranslationLog(user_id=1, original_text="a", translated_text="b", target_language="en",
                                       created_at=now - timedelta(days=40 + index)))
        session.add(TranslationLog(user_id=1, original_text="a", translated_text="b", target_language="en",
                                   created_at=now - timedelta(days=1)))
    monkeypatch.setattr("services.interaction_tracking_service.TRANSLATION_PURGE_BATCH_SIZE", 3)
    tracking = InteractionTrackingService(db)

    assert await tracking.purge_old_translation_logs(30) == 7
    with Session(db.engine) as session:
        assert session.query(TranslationLog).count() == 1
    with pytest.raises(ValueError):
        await tracking.purge_old_translation_logs(0)


@pytest.mark.asyncio
async def test_user_and_profile_upserts_use_integrity_retries():
    calls = []

    class RecordingDb:
        async def run_in_transaction(self, operation, *, retry_on_integrity_error=False):
            calls.append(retry_on_integrity_error)
            return "ok"

    tracking = InteractionTrackingService(RecordingDb())
    await tracking.track_user(user_id=1, username="u", discriminator="0", display_name="U")
    await tracking.track_player_lookup(user_id=1, player_id="5", guild_id=7, username="u")
    await tracking.sync_player_metadata_many([{"player_id": "5"}])
    await tracking.log_gift_code_redemptions_many([])
    assert calls == [True, True, True, False]
