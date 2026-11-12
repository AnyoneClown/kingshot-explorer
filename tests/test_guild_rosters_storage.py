"""The shared gift-code list, target selection and gift-code state queries against a real (SQLite) database."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from db.models import (
    GiftCode,
    GiftCodeRedemption,
    RegisteredPlayer,
    TranslationLog,
    User,
)
from repositories.discord_repositories import GiftCodeRedemptionRepository, GiftCodeRepository
from services.gift_code_service import GiftCodeService
from services.gift_redemption_service import FINAL_PLAYER_ERROR_CODES, GiftRedemptionService
from services.interaction_tracking_service import InteractionTrackingService
from services.player_registry_service import PlayerRegistryService, RosterEntry, order_redeem_targets
from tests.sqlite_helpers import SqliteDb

KINGDOM, OTHER_KINGDOM = "830", "795"


@pytest.fixture
def db():
    database = SqliteDb()
    with Session(database.engine) as session, session.begin():
        session.add_all([User(id=uid, username=f"user{uid}") for uid in (1, 2, 3, 99)])
    return database


def seed(db, *players):
    """Rows are ``(player_id, enabled[, kingdom[, owner]])``: kingdom 830 and user 1 unless given."""
    with Session(db.engine) as session, session.begin():
        for player_id, enabled, *rest in players:
            kingdom = rest[0] if rest else KINGDOM
            owner = rest[1] if len(rest) > 1 else 1
            session.add(RegisteredPlayer(player_id=player_id, player_name=f"P{player_id}", kingdom=kingdom,
                                         enabled=enabled, added_by_user_id=owner))


def profile(player_id, **extra):
    return {"player_id": player_id, "player_name": f"Name {player_id}", "kingdom": KINGDOM, **extra}


def stored(db, player_id):
    with Session(db.engine) as session:
        return session.scalars(select(RegisteredPlayer).where(RegisteredPlayer.player_id == player_id)).one()


@pytest.mark.asyncio
async def test_redeem_targets_are_every_enabled_account_whichever_kingdom_and_owner(db):
    seed(
        db,
        ("100", True),                      # this kingdom, user 1 -> included
        ("200", True, OTHER_KINGDOM, 2),    # another kingdom, another member -> included
        ("300", False),                     # switched off -> excluded
        ("400", False, OTHER_KINGDOM, 2),   # switched off elsewhere -> excluded
        ("500", True, None, 3),             # no kingdom stored: in no roster, still redeemed
    )
    registry = PlayerRegistryService(db)

    targets = [player.player_id for player in await registry.get_auto_redeem_targets()]
    assert targets == ["100", "200", "500"]

    # Disabled accounts stay on the list: only redemption skips them.
    everyone = await registry.get_registered_players(enabled_only=False)
    assert [player.player_id for player in everyone] == ["100", "200", "300", "400", "500"]
    assert await registry.get_entries(["200", "300", "999"]) == {
        "200": RosterEntry("200", "P200", None, OTHER_KINGDOM, None, True, 2),
        "300": RosterEntry("300", "P300", None, KINGDOM, None, False, 1),
    }


@pytest.mark.asyncio
async def test_redeem_targets_put_members_own_accounts_before_bulk_additions(db):
    seed(
        db,
        ("901", True, KINGDOM, 3), ("903", True, KINGDOM, 3), ("904", True, KINGDOM, 3),  # a manager's import
        ("902", True, KINGDOM, 1),            # one account of their own
        ("905", True, OTHER_KINGDOM, 2),      # their own, in another kingdom
        ("906", False, OTHER_KINGDOM, 2),     # switched off: not a target, not counted
        ("907", False, KINGDOM, 2),
    )
    registry = PlayerRegistryService(db)

    plain = ["901", "902", "903", "904", "905"]
    assert [p.player_id for p in await registry.get_auto_redeem_targets()] == plain
    ordered = await registry.get_auto_redeem_targets(self_registered_max=2)
    assert [p.player_id for p in ordered] == ["902", "905", "901", "903", "904"]
    # A limit of 0 means members cannot add accounts themselves: there is nobody to put first.
    assert [p.player_id for p in await registry.get_auto_redeem_targets(self_registered_max=0)] == plain
    # Three accounts are within a limit of three, so nothing counts as a bulk addition.
    assert [p.player_id for p in await registry.get_auto_redeem_targets(self_registered_max=3)] == plain


def test_order_redeem_targets_counts_owners_among_the_targets_and_keeps_id_order_inside_a_tier():
    def account(player_id, owner):
        return SimpleNamespace(player_id=player_id, added_by_user_id=owner)

    targets = [account("30", 7), account("9", 8), account("20", 7), account("10", 7), account("40", 9)]

    ordered = order_redeem_targets(targets, 1)
    # Owners 8 and 9 added one target each; owner 7 added three. IDs sort as text, like the query.
    assert [player.player_id for player in ordered] == ["40", "9", "10", "20", "30"]
    assert [player.player_id for player in targets] == ["30", "9", "20", "10", "40"]  # the input is not reordered
    assert [player.player_id for player in order_redeem_targets(targets, 3)] == ["10", "20", "30", "40", "9"]
    assert order_redeem_targets([], 2) == []


@pytest.mark.asyncio
async def test_kingdom_roster_lists_the_kingdom_accounts_whoever_added_them(db):
    seed(
        db,
        ("100", True, KINGDOM, 1),
        ("200", False, KINGDOM, 2),          # another member's, switched off: still listed
        ("300", True, OTHER_KINGDOM, 3),
        ("400", True, None, 1),              # no kingdom stored: in no roster
    )
    registry = PlayerRegistryService(db)

    roster = await registry.list_kingdom_roster(KINGDOM)
    assert [(e.player_id, e.enabled, e.added_by_user_id, e.kingdom) for e in roster] == [
        ("100", True, 1, KINGDOM),
        ("200", False, 2, KINGDOM),
    ]
    assert all(isinstance(entry, RosterEntry) for entry in roster)
    # Server settings hold the kingdom as a number; profiles store it as text.
    assert [e.player_id for e in await registry.list_kingdom_roster(830)] == ["100", "200"]
    assert [e.player_id for e in await registry.list_kingdom_roster(f" {OTHER_KINGDOM} ")] == ["300"]
    assert await registry.list_kingdom_roster("111") == []
    # A server without a kingdom has no roster; it must not match the accounts without one.
    for missing in (None, "", "  "):
        assert await registry.list_kingdom_roster(missing) == []


@pytest.mark.asyncio
async def test_ownership_is_global_and_follows_the_account_between_kingdom_rosters(db):
    seed(db, ("100", True, KINGDOM, 1), ("200", False, OTHER_KINGDOM, 1), ("300", True, KINGDOM, 2))
    registry = PlayerRegistryService(db)

    owned = await registry.list_owned_entries(1)
    assert [(e.player_id, e.kingdom, e.enabled) for e in owned] == [("100", KINGDOM, True), ("200", OTHER_KINGDOM, False)]
    assert await registry.count_owned(1) == 2
    assert await registry.count_owned(2) == 1
    assert await registry.list_owned_entries(3) == [] and await registry.count_owned(3) == 0

    # The account moved kingdoms in game: a refresh moves it to the other roster, the owner stays.
    assert await registry.sync_player_metadata("200", kingdom=KINGDOM, added_by_user_id=3)
    assert [e.player_id for e in await registry.list_kingdom_roster(OTHER_KINGDOM)] == []
    assert [e.player_id for e in await registry.list_kingdom_roster(KINGDOM)] == ["100", "200", "300"]
    assert [e.player_id for e in await registry.list_owned_entries(1)] == ["100", "200"]
    assert (await registry.get_entry("200")).enabled is False


@pytest.mark.asyncio
async def test_registration_respects_the_personal_cap_over_the_whole_list(db):
    seed(db, ("900", True, KINGDOM, 2))
    registry = PlayerRegistryService(db)

    outcome = await registry.register_players(
        [profile("101"), profile("102"), profile("900"), profile("103"), profile("101")],
        added_by_user_id=1,
        max_owned=2,
    )
    assert outcome.added == ["101", "102"]
    assert outcome.already_registered == ["900"]
    assert outcome.over_limit == ["103"]
    assert outcome.reenabled == []
    # Writes go through the retried transaction helper (read-then-insert upsert),
    # serializable so concurrent additions cannot both pass the cap check.
    assert db.transactions[-1] is True
    assert db.isolation_levels[-1] == "SERIALIZABLE"

    # The cap is the member's own over every kingdom, not one per server.
    outcome = await registry.register_players(
        [profile("104", kingdom=OTHER_KINGDOM)], added_by_user_id=1, max_owned=2
    )
    assert outcome.added == [] and outcome.over_limit == ["104"]
    assert await registry.get_entry("104") is None
    # A higher cap leaves room only for the difference.
    outcome = await registry.register_players(
        [profile("104", kingdom=OTHER_KINGDOM), profile("105")], added_by_user_id=1, max_owned=3
    )
    assert outcome.added == ["104"] and outcome.over_limit == ["105"]

    # Without a personal cap (managers) nothing limits the list: there is no server cap.
    bulk = [profile(str(player_id)) for player_id in range(1000, 1400)]
    outcome = await registry.register_players(bulk, added_by_user_id=3)
    assert len(outcome.added) == 400 and outcome.over_limit == []

    assert await registry.count_owned(1) == 3
    assert await registry.count_owned(2) == 1
    assert await registry.count_owned(3) == 400
    assert [e.player_id for e in await registry.list_owned_entries(1)] == ["101", "102", "104"]
    assert len(await registry.get_auto_redeem_targets()) == 404


@pytest.mark.asyncio
async def test_additions_arriving_together_cannot_overshoot_the_personal_cap(db):
    registry = PlayerRegistryService(db)

    first, second = await asyncio.gather(
        registry.register_players([profile("101"), profile("102")], added_by_user_id=1, max_owned=3),
        registry.register_players([profile("103"), profile("104")], added_by_user_id=1, max_owned=3),
    )

    assert len(first.added) + len(second.added) == 3
    assert len(first.over_limit) + len(second.over_limit) == 1
    assert await registry.count_owned(1) == 3
    # On the real database the count and the inserts must share one serializable
    # transaction: the second addition is retried and then sees the first one's rows.
    assert db.isolation_levels[-2:] == ["SERIALIZABLE", "SERIALIZABLE"]
    assert db.transactions[-2:] == [True, True]

    # A cap already used up (or lowered below what the member owns) accepts nothing.
    outcome = await registry.register_players([profile("105")], added_by_user_id=1, max_owned=1)
    assert outcome.added == [] and outcome.over_limit == ["105"]
    # Blank and repeated IDs neither take a place nor show up in the outcome.
    outcome = await registry.register_players(
        [profile("201"), profile(" 201 "), profile(""), {"player_name": "no id"}, profile("202")],
        added_by_user_id=2,
        max_owned=1,
    )
    assert outcome.added == ["201"] and outcome.over_limit == ["202"] and outcome.already_registered == []


@pytest.mark.asyncio
async def test_an_account_already_on_the_list_keeps_its_owner_and_is_refreshed(db):
    seed(db, ("200", True, OTHER_KINGDOM, 2))
    registry = PlayerRegistryService(db)

    # Listed accounts are free for everyone: they do not count against the caller's cap.
    outcome = await registry.register_players(
        [profile("200", castle_level="30"), profile("201")], added_by_user_id=1, max_owned=1
    )
    assert outcome.already_registered == ["200"] and outcome.added == ["201"] and outcome.over_limit == []

    row = stored(db, "200")
    assert row.added_by_user_id == 2
    assert (row.player_name, row.kingdom, row.castle_level, row.enabled) == ("Name 200", KINGDOM, "30", True)
    assert stored(db, "201").added_by_user_id == 1
    assert await registry.count_owned(2) == 1 and await registry.count_owned(1) == 1
    with Session(db.engine) as session:
        assert len(session.scalars(select(RegisteredPlayer)).all()) == 2


@pytest.mark.asyncio
async def test_existing_disabled_account_is_reenabled_only_on_request(db):
    seed(db, ("200", False, KINGDOM, 2), ("201", True, KINGDOM, 2))
    registry = PlayerRegistryService(db)

    outcome = await registry.register_players([profile("200")], added_by_user_id=1)
    assert outcome.already_registered == ["200"] and outcome.reenabled == []
    assert (await registry.get_entry("200")).enabled is False

    outcome = await registry.register_players(
        [profile("200"), profile("201")], added_by_user_id=1, enable_existing=True
    )
    # Only the account that was off is reported; the one already on is just refreshed.
    assert outcome.already_registered == ["200", "201"] and outcome.reenabled == ["200"]
    entry = await registry.get_entry("200")
    assert entry.enabled is True and entry.added_by_user_id == 2
    assert [p.player_id for p in await registry.get_auto_redeem_targets()] == ["200", "201"]


@pytest.mark.asyncio
async def test_a_lookup_does_not_put_a_player_on_the_list_and_refreshes_a_listed_one(db):
    tracking = InteractionTrackingService(db)
    registry = PlayerRegistryService(db)

    # Every profile row is a listed account, so a lookup of an unlisted player stores none:
    # it would show up in the kingdom roster and count against the member who looked.
    await tracking.track_player_lookup(
        user_id=2, player_id="777", player_name="Looked up", kingdom=KINGDOM, username="user2"
    )
    assert await registry.get_entry("777") is None
    assert await registry.count_owned(2) == 0 and await registry.list_kingdom_roster(KINGDOM) == []

    outcome = await registry.register_players([profile("777")], added_by_user_id=1, max_owned=3)
    assert outcome.added == ["777"]
    assert [p.player_id for p in await registry.get_auto_redeem_targets()] == ["777"]

    # A lookup of a listed account refreshes it and leaves its owner and flag alone.
    await tracking.track_player_lookup(
        user_id=2, player_id="777", player_name="Renamed", kingdom=OTHER_KINGDOM, username="user2"
    )
    entry = await registry.get_entry("777")
    assert (entry.player_name, entry.kingdom, entry.added_by_user_id, entry.enabled) == (
        "Renamed", OTHER_KINGDOM, 1, True,
    )


@pytest.mark.asyncio
async def test_removal_takes_the_account_off_the_list_for_every_server(db):
    seed(db, ("200", True, KINGDOM, 2), ("201", True, KINGDOM, 1))
    registry = PlayerRegistryService(db)

    first = await registry.remove_player("200")
    assert first.removed
    # The removed entry is returned for the confirmation message.
    assert first.entry == RosterEntry("200", "P200", None, KINGDOM, None, True, 2)

    missing = await registry.remove_player("200")
    assert not missing.removed and missing.entry is None

    # The profile is gone: no roster lists it, nobody owns it and it is not redeemed.
    assert await registry.get_entry("200") is None
    assert [e.player_id for e in await registry.list_kingdom_roster(KINGDOM)] == ["201"]
    assert await registry.list_owned_entries(2) == [] and await registry.count_owned(2) == 0
    assert [p.player_id for p in await registry.get_auto_redeem_targets()] == ["201"]
    with Session(db.engine) as session:
        assert [p.player_id for p in session.scalars(select(RegisteredPlayer))] == ["201"]

    # It can be added again, by anyone, and then belongs to whoever added it.
    outcome = await registry.register_players([profile("200")], added_by_user_id=3, max_owned=1)
    assert outcome.added == ["200"] and stored(db, "200").added_by_user_id == 3


@pytest.mark.asyncio
async def test_switching_accounts_on_and_off_counts_only_the_accounts_that_changed(db):
    seed(db, ("100", True), ("101", True), ("102", False), ("200", True, OTHER_KINGDOM, 2))
    registry = PlayerRegistryService(db)

    # 102 is already off and 999 is not on the list.
    assert await registry.set_players_enabled(["100", "102", "999"], False) == 1
    assert await registry.set_players_enabled(["100", "102"], False) == 0
    assert await registry.set_players_enabled([], False) == 0
    assert await registry.set_players_enabled(["101", "101"], False) == 1
    assert [p.player_id for p in await registry.get_auto_redeem_targets()] == ["200"]

    assert await registry.set_players_enabled(["100", "102", "200"], True) == 2
    assert [p.player_id for p in await registry.get_auto_redeem_targets()] == ["100", "102", "200"]
    assert (await registry.get_entry("101")).enabled is False

    # The automatic pause is the same switch, for every kingdom at once.
    assert await registry.disable_broken_players(["100", "200", "101", "999"]) == 2
    assert await registry.disable_broken_players([]) == 0
    assert [p.player_id for p in await registry.get_auto_redeem_targets()] == ["102"]
    roster = {e.player_id: e.enabled for e in await registry.list_kingdom_roster(KINGDOM)}
    assert roster == {"100": False, "101": False, "102": True}
    assert stored(db, "200").added_by_user_id == 2


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
