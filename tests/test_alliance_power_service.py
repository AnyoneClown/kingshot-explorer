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

    api = SimpleNamespace(rosters={}, profiles={}, active=0, peak=0)
    for aid in (10, 20, 30):
        uids = [aid * 100 + index for index in range(12)]
        api.rosters[aid] = {"success": True, "data": {
            "aid": aid, "count": len(uids), "members": [{"uid": uid, "rank": 1} for uid in uids],
        }}
        for index, uid in enumerate(uids):
            api.profiles[str(uid)] = {"success": True, "data": {
                "uid": uid, "fid": uid + 100000, "name": f"Player {uid}", "kid": 830,
                "power": index * 100,
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

    api.get_alliance = AsyncMock(side_effect=get_alliance)
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
    assert report["latest"].members["1000"] == {"name": "Player 1000", "fid": "101000", "power": 0}
    assert report["latest"].total_power == sum(index * 100 for index in range(12))
    assert report["daily"] is None and report["weekly"] is None
    assert api.get_alliance.await_count == 1
    assert api.get_player.await_count == 12 and api.peak == 5

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
    clock.value += timedelta(days=1)
    api.rosters[10]["success"] = False
    await service.collect_due_snapshots(set())
    api.get_alliance.assert_not_awaited()
    await service.collect_due_snapshots({1, 2})

    assert [call.args for call in api.get_alliance.await_args_list] == [(10, 830), (20, 830)]
    assert (await service.get_report(1))["latest"].snapshot_date == date(2026, 9, 5)
    assert (await service.get_report(2))["latest"].snapshot_date == date(2026, 9, 6)
    assert (await service.get_report(3))["latest"].snapshot_date == date(2026, 9, 5)
    api.rosters[10]["success"] = True
    await service.collect_due_snapshots({1, 2})
    assert (await service.get_report(1))["daily"].snapshot_date == date(2026, 9, 5)
