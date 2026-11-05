import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from db.models import AlliancePowerSnapshot, AlliancePowerTracking
from services.alliance_power_service import AlliancePowerService


@pytest.fixture
def power_setup():
    engine = create_engine("sqlite://")
    AlliancePowerTracking.__table__.create(engine)
    AlliancePowerSnapshot.__table__.create(engine)

    @asynccontextmanager
    async def session():
        with Session(engine, expire_on_commit=False) as sync_session, sync_session.begin():
            async def execute(statement):
                return sync_session.execute(statement)

            yield SimpleNamespace(execute=execute)

    api = SimpleNamespace(rosters={}, profiles={}, active=0, peak=0, full_failure=False, full_errors=set())
    for aid in (10, 20, 30):
        uids = [aid * 100 + index for index in range(12)]
        api.rosters[aid] = {"success": True, "data": {
            "aid": aid, "count": len(uids), "members": [{"uid": uid, "rank": 1 + index % 5} for index, uid in enumerate(uids)],
        }}
        for index, uid in enumerate(uids):
            api.profiles[str(uid)] = {"success": True, "data": {
                "uid": uid, "fid": uid + 100000, "name": f"Player {uid}", "kid": 830,
                "power": index * 100, "stove_lv": 20 + index, "vip": index % 10, "life_tree_level": index,
                "alliance": {"aid": aid, "abbr": "ABC", "name": f"Alliance {aid}"},
            }}

    async def get_player(uid):
        api.active += 1
        api.peak = max(api.peak, api.active)
        try:
            await asyncio.sleep(0)
            return api.profiles[str(uid)]
        finally:
            api.active -= 1

    async def get_alliance(aid, kid):
        assert kid == 830
        return api.rosters[aid]

    async def get_alliance_full(aid, kid):
        # Mirrors /full: the lean roster with each member's profile or an error.
        assert kid == 830
        if api.full_failure:
            return {"success": False, "error_code": "NETWORK_ERROR", "error_message": "timeout"}
        roster = deepcopy(api.rosters[aid])
        if not roster.get("success") or not isinstance(roster.get("data"), dict):
            return roster
        for member in roster["data"]["members"]:
            profile = api.profiles.get(str(member["uid"]))
            if str(member["uid"]) in api.full_errors or not profile.get("success") or profile.get("partial"):
                member["error"] = "gateway timeout"
            else:
                member["player"] = deepcopy(profile["data"])
        return roster

    api.get_alliance = AsyncMock(side_effect=get_alliance)
    api.get_alliance_full = AsyncMock(side_effect=get_alliance_full)
    api.get_player = AsyncMock(side_effect=get_player)
    clock = SimpleNamespace(value=datetime(2026, 9, 5, 12, tzinfo=timezone.utc))
    service = AlliancePowerService(SimpleNamespace(session=session), api, now_provider=lambda: clock.value)
    yield service, api, clock, engine
    engine.dispose()


@pytest.mark.asyncio
async def test_shared_daily_capture_resolves_lean_roster_with_bounded_requests_and_stops_per_guild(power_setup):
    service, api, clock, engine = power_setup
    clock.value = datetime(2026, 9, 6, 0, 30, tzinfo=timezone(timedelta(hours=3)))
    await asyncio.gather(service.track_alliance(1, 830, 10), service.track_alliance(2, 830, 10))
    await service.track_alliance(1, 830, 10)
    await service.collect_due_snapshots({1, 2})

    report = await service.get_report(1)
    assert report["latest"].snapshot_date == date(2026, 9, 5)
    assert report["latest"].alliance_name == "Alliance 10"
    assert report["latest"].alliance_tag == "ABC"
    assert report["latest"].members["1000"] == {
        "name": "Player 1000", "fid": "101000", "power": 0,
        "stove_lv": 20, "vip": 0, "life_tree_level": 0, "rank": 1,
    }
    assert report["latest"].members["1006"]["rank"] == 2
    assert report["latest"].total_power == sum(index * 100 for index in range(12))
    assert report["daily"] is None and report["weekly"] is None
    assert report["previous"] is None and report["changes"] is None
    assert report["inactive"] == {7: None, 14: None}
    # One paced /full call replaces the lean roster plus one profile call per member.
    assert api.get_alliance_full.await_count == 1
    assert api.get_alliance.await_count == 0 and api.get_player.await_count == 0

    assert not await service.stop_tracking(1, 830, 20)
    assert not await service.stop_tracking(1, 900, 10)
    assert (await service.get_report(1))["alliance_id"] == 10
    assert await service.stop_tracking(1, 830, 10)
    assert not await service.stop_tracking(1, 830, 10)
    assert await service.get_report(1) is None
    assert (await service.get_report(2))["latest"].total_power == report["latest"].total_power
    with Session(engine) as session:
        assert len(session.scalars(select(AlliancePowerSnapshot)).all()) == 1


@pytest.mark.asyncio
async def test_report_uses_exact_daily_weekly_dates_and_does_not_fill_gaps(power_setup):
    service, api, clock, _ = power_setup
    await service.track_alliance(1, 830, 10)
    clock.value += timedelta(days=1)
    api.profiles["1000"]["data"]["power"] = 200
    await service.collect_due_snapshots({1})
    report = await service.get_report(1)
    assert report["latest"].total_power - report["daily"].total_power == 200
    assert report["weekly"] is None

    clock.value += timedelta(days=6)
    api.profiles["1000"]["data"]["power"] = 700
    await service.collect_due_snapshots({1})
    report = await service.get_report(1)
    assert report["daily"] is None
    assert report["weekly"].snapshot_date == date(2026, 9, 5)
    assert report["latest"].total_power - report["weekly"].total_power == 700
    assert [item.snapshot_date for item in report["history"]] == [date(2026, 9, 5), date(2026, 9, 6), date(2026, 9, 12)]

    clock.value += timedelta(days=2)
    await service.collect_due_snapshots({1})
    report = await service.get_report(1)
    assert report["daily"] is None and report["weekly"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "api_failure", "partial_roster", "partial_profile", "null_power", "duplicate_uid", "count_mismatch",
    "wrong_uid", "wrong_kingdom", "wrong_alliance",
])
async def test_incomplete_capture_keeps_prior_selection_and_history_until_successful_retry(power_setup, failure):
    service, api, clock, engine = power_setup
    await service.track_alliance(1, 830, 10)
    original_roster = deepcopy(api.rosters[20])
    original_profile = deepcopy(api.profiles["2000"])
    roster = api.rosters[20]
    profile = api.profiles["2000"]
    if failure == "api_failure":
        roster["success"] = False
    elif failure == "partial_roster":
        roster["data"]["partial"] = True
    elif failure == "partial_profile":
        profile["partial"] = True
    elif failure == "null_power":
        profile["data"]["power"] = None
    elif failure == "duplicate_uid":
        roster["data"]["members"][1] = roster["data"]["members"][0]
    elif failure == "count_mismatch":
        roster["data"]["count"] += 1
    elif failure == "wrong_uid":
        profile["data"]["uid"] = 999
    elif failure == "wrong_kingdom":
        profile["data"]["kid"] = 999
    elif failure == "wrong_alliance":
        profile["data"]["alliance"]["aid"] = 999

    with pytest.raises(ValueError, match="existing history is preserved"):
        await service.track_alliance(1, 830, 20)
    assert (await service.get_report(1))["alliance_id"] == 10
    with Session(engine) as session:
        assert [snapshot.alliance_id for snapshot in session.scalars(select(AlliancePowerSnapshot))] == [10]

    api.rosters[20] = original_roster
    api.profiles["2000"] = original_profile
    clock.value += timedelta(days=1)
    await service.track_alliance(1, 830, 20)
    assert (await service.get_report(1))["latest"].snapshot_date == date(2026, 9, 6)
    assert (await service.get_report(1))["alliance_id"] == 20
    with Session(engine) as session:
        assert {snapshot.alliance_id for snapshot in session.scalars(select(AlliancePowerSnapshot))} == {10, 20}


@pytest.mark.asyncio
async def test_collector_limits_to_connected_guilds_and_continues_after_failed_alliance(power_setup):
    service, api, clock, _ = power_setup
    for guild_id, alliance_id in [(1, 10), (2, 20), (3, 30)]:
        await service.track_alliance(guild_id, 830, alliance_id)
    api.get_alliance.reset_mock()
    api.get_alliance_full.reset_mock()
    clock.value += timedelta(days=1)
    api.rosters[10]["success"] = False
    assert await service.collect_due_snapshots(set()) == []
    api.get_alliance_full.assert_not_awaited()
    assert await service.collect_due_snapshots({1, 2}) == [(830, 20)]

    assert [call.args for call in api.get_alliance_full.await_args_list] == [(10, 830), (20, 830)]
    assert [call.args for call in api.get_alliance.await_args_list] == [(10, 830)]  # Fallback after /full failed.
    assert (await service.get_report(1))["latest"].snapshot_date == date(2026, 9, 5)
    assert (await service.get_report(2))["latest"].snapshot_date == date(2026, 9, 6)
    assert (await service.get_report(3))["latest"].snapshot_date == date(2026, 9, 5)
    api.rosters[10]["success"] = True
    assert await service.collect_due_snapshots({1, 2}) == [(830, 10)]
    assert await service.collect_due_snapshots({1, 2}) == []  # Only fresh captures are reported.
    assert (await service.get_report(1))["daily"].snapshot_date == date(2026, 9, 5)


@pytest.mark.asyncio
async def test_capture_falls_back_to_member_profiles_when_full_roster_fails(power_setup):
    service, api, clock, _ = power_setup
    api.full_failure = True
    assert await service.track_alliance(1, 830, 10) is True
    report = await service.get_report(1)
    assert api.get_alliance_full.await_count == 1 and api.get_alliance.await_count == 1
    assert api.get_player.await_count == 12 and api.peak == 5
    assert report["latest"].members["1011"]["stove_lv"] == 31
    assert await service.track_alliance(1, 830, 10) is False  # Same UTC day: no new snapshot.


@pytest.mark.asyncio
async def test_failed_full_members_are_refetched_individually(power_setup):
    service, api, clock, _ = power_setup
    api.full_errors = {"1003", "1007"}
    await service.track_alliance(1, 830, 10)
    assert sorted(call.args[0] for call in api.get_player.await_args_list) == ["1003", "1007"]
    assert api.get_alliance.await_count == 0
    members = (await service.get_report(1))["latest"].members
    assert len(members) == 12 and members["1003"]["vip"] == 3 and members["1003"]["rank"] == 4


@pytest.mark.asyncio
async def test_full_roster_rows_with_inline_profiles_and_missing_optional_fields(power_setup):
    service, api, clock, _ = power_setup
    inline = deepcopy(api.rosters[30])
    for member in inline["data"]["members"]:
        member.update(deepcopy(api.profiles[str(member["uid"])]["data"]))
        del member["stove_lv"]
    api.get_alliance_full.side_effect = None
    api.get_alliance_full.return_value = inline
    await service.track_alliance(1, 830, 30)
    member = (await service.get_report(1))["latest"].members["3000"]
    assert member["stove_lv"] is None and member["vip"] == 0 and member["power"] == 0
    api.get_player.assert_not_awaited()


def _insert_snapshot(engine, snapshot_date, members, *, kingdom_id=830, alliance_id=10):
    with Session(engine) as session, session.begin():
        session.add(AlliancePowerSnapshot(
            kingdom_id=kingdom_id, alliance_id=alliance_id, snapshot_date=snapshot_date,
            captured_at=datetime.combine(snapshot_date, datetime.min.time(), tzinfo=timezone.utc),
            alliance_name="Alliance", alliance_tag="ABC",
            total_power=sum(member["power"] for member in members.values()), members=members,
        ))


@pytest.mark.asyncio
async def test_roster_changes_compare_the_two_latest_complete_snapshots_across_gaps(power_setup):
    service, api, clock, engine = power_setup
    with Session(engine) as session, session.begin():
        session.add(AlliancePowerTracking(guild_id=1, kingdom_id=830, alliance_id=10))
    # Legacy snapshot shape: only name/fid/power.
    _insert_snapshot(engine, date(2026, 9, 1), {
        "1": {"name": "Stays", "fid": "11", "power": 500},
        "2": {"name": "Leaves", "fid": "12", "power": 900},
        "3": {"name": "OldName", "fid": "13", "power": 300},
        "4": {"name": "Player 4", "fid": None, "power": 100},
    })
    # 2026-09-02 capture failed: no row. The next comparison spans the gap.
    _insert_snapshot(engine, date(2026, 9, 3), {
        "1": {"name": "Stays", "fid": "11", "power": 600, "stove_lv": 25, "rank": 4},
        "3": {"name": "NewName", "fid": "13", "power": 300},
        "4": {"name": "Real Name", "fid": None, "power": 100},
        "5": {"name": "Joiner", "fid": "15", "power": 800, "stove_lv": 30},
        "6": {"name": "Small joiner", "fid": "16", "power": 10},
    })
    report = await service.get_report(1)
    changes = report["changes"]
    assert report["previous"].snapshot_date == date(2026, 9, 1) and report["daily"] is None
    assert changes.gap_days == 2 and not changes.is_empty
    assert [uid for uid, _ in changes.joined] == ["5", "6"]
    assert [(uid, member["name"]) for uid, member in changes.left] == [("2", "Leaves")]
    assert changes.renamed == [("3", "OldName", "NewName")]  # Fallback labels are not renames.


def _flat_history(engine, days):
    start = date(2026, 9, 1)
    for offset in range(days + 1):
        _insert_snapshot(engine, start + timedelta(days=offset), {
            "1": {"name": "Flat forever", "power": 1000},
            "2": {"name": "Growing", "power": 1000 + offset},
            "3": {"name": "Flat lately", "power": 2000 + min(offset, 5)},
            "4": {"name": "Dipped and back", "power": 3000 - (offset == 10)},
            **({"5": {"name": "New flat", "power": 50}} if offset >= 10 else {}),
        })
    return start + timedelta(days=days)


@pytest.mark.asyncio
async def test_inactivity_flags_flat_power_over_7_and_14_days(power_setup):
    service, api, clock, engine = power_setup
    latest = _flat_history(engine, 14)
    report = await service.get_alliance_report(830, 10)
    seven, fourteen = report["inactive"][7], report["inactive"][14]
    assert [member.name for member in seven] == ["Flat lately", "Flat forever"]
    assert seven[0].since == latest - timedelta(days=7)
    assert [member.name for member in fourteen] == ["Flat forever"]
    assert fourteen[0].since == latest - timedelta(days=14)


@pytest.mark.asyncio
async def test_inactivity_needs_enough_history(power_setup):
    service, api, clock, engine = power_setup
    _flat_history(engine, 5)
    report = await service.get_alliance_report(830, 10)
    assert report["inactive"] == {7: None, 14: None}
    with Session(engine) as session, session.begin():
        session.query(AlliancePowerSnapshot).delete()
    _flat_history(engine, 6)  # One missing day at the window edge is tolerated.
    report = await service.get_alliance_report(830, 10)
    assert [member.name for member in report["inactive"][7]] == ["Dipped and back", "Flat forever"]
    assert report["inactive"][14] is None


@pytest.mark.asyncio
async def test_resolve_alliance_tag_uses_ranked_alliances(power_setup):
    service, api, clock, _ = power_setup
    api.get_ranked_alliances = AsyncMock(return_value={"success": True, "data": [
        {"aid": 10, "abbr": "ABC"}, {"aid": 20, "abbr": "XYZ"}, {"aid": 30, "abbr": "DUP"}, {"aid": 31, "abbr": "dup"},
    ]})
    assert await service.resolve_alliance_tag(830, "abc") == 10
    api.get_ranked_alliances.assert_awaited_with(830, 15, exhaustive=True)
    assert await service.resolve_alliance_tag(830, "DUP") is None  # Ambiguous.
    assert await service.resolve_alliance_tag(830, "NOP") is None
    api.get_ranked_alliances.side_effect = RuntimeError("down")
    assert await service.resolve_alliance_tag(830, "ABC") is None
