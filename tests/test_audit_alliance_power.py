"""Audit regressions: alliance power capture fallbacks and incomplete member profiles."""

import asyncio
import logging
from datetime import date, timedelta

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from db.models import AlliancePowerSnapshot
from services.alliance_power_service import AlliancePowerService
from services.kingshot_data_service import KingshotDataService
from tests.test_alliance_power_service import power_setup  # noqa: F401 - shared fixture

LOGGER = "services.alliance_power_service"


def _records(caplog, text):
    return [record for record in caplog.records if text in record.getMessage()]


@pytest.mark.asyncio
async def test_full_roster_timeout_falls_back_with_one_line_log(power_setup, caplog):  # noqa: F811
    """A /full timeout is an expected fallback: log the cause once, not a ValueError traceback."""
    service, api, _, _ = power_setup
    api.get_alliance_full.side_effect = None
    api.get_alliance_full.return_value = {
        "success": False, "status_code": None, "error_code": "NETWORK_ERROR", "error_message": "Timed out: slow",
    }
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert await service.track_alliance(1, 830, 10) is True

    [record] = _records(caplog, "Full roster unavailable")
    assert record.exc_info is None
    assert "NETWORK_ERROR: Timed out: slow" in record.getMessage()
    assert api.get_player.await_count == 12  # fell back to member profiles


@pytest.mark.asyncio
async def test_unexpected_full_roster_error_still_logs_traceback(power_setup, caplog):  # noqa: F811
    service, api, _, _ = power_setup
    api.get_alliance_full.side_effect = RuntimeError("bug")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert await service.track_alliance(1, 830, 10) is True

    [record] = _records(caplog, "Full roster failed unexpectedly")
    assert record.levelno == logging.WARNING and record.exc_info is not None


@pytest.mark.asyncio
async def test_profiles_incomplete_after_last_round_store_nothing_and_keep_history(power_setup, caplog):  # noqa: F811
    """All-or-nothing: no snapshot with a missing/zero member, so no fake drop or departure."""
    service, api, clock, engine = power_setup
    assert await service.track_alliance(1, 830, 10) is True  # 2026-09-05, complete
    day_one_total = (await service.get_report(1))["latest"].total_power

    clock.value += timedelta(days=1)
    api.full_failure = True
    stuck = "1011"  # the strongest member (power 1100)
    real_profile = api.profiles[stuck]
    api.profiles[stuck] = {"success": True, "data": {"uid": 1011, "fid": None, "name": None, "power": None, "kid": None}}
    api.get_player.reset_mock()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert await service.collect_due_snapshots({1}) == []

    stuck_calls = [call.args[0] for call in api.get_player.await_args_list].count(stuck)
    assert stuck_calls == AlliancePowerService.MEMBER_FETCH_ROUNDS
    with Session(engine) as session:
        assert [row.snapshot_date for row in session.scalars(select(AlliancePowerSnapshot))] == [date(2026, 9, 5)]
    report = await service.get_report(1)
    assert report["latest"].snapshot_date == date(2026, 9, 5) and report["latest"].total_power == day_one_total
    [failure] = _records(caplog, "Incomplete power snapshot")
    assert "stayed incomplete after 6 rounds" in failure.getMessage() and not failure.exc_info
    [retry] = _records(caplog, "retrying next hour")
    assert not retry.exc_info

    # Next hourly pass, the profile is back: a complete snapshot for the same UTC day.
    api.profiles[stuck] = real_profile
    clock.value += timedelta(hours=1)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert await service.collect_due_snapshots({1}) == [(830, 10)]
    assert _records(caplog, "Saved power snapshot 2026-09-06 for kingdom 830 alliance 10 (12 members)")
    report = await service.get_report(1)
    assert report["latest"].snapshot_date == date(2026, 9, 6)
    assert report["latest"].members[stuck]["power"] == 1100
    assert report["latest"].total_power == report["daily"].total_power
    assert report["changes"].is_empty


def test_full_roster_timeout_is_a_warning_not_an_error(caplog):
    class TimeoutClient:
        async def request(self, **kwargs):
            raise httpx.ReadTimeout("slow")

        async def aclose(self):
            pass

    service = KingshotDataService(api_key="key", base_url="https://example.test")
    client = TimeoutClient()

    async def ensure_client():
        return client

    service.ensure_client = ensure_client
    with caplog.at_level(logging.WARNING, logger="services.kingshot_data_service"):
        result = asyncio.run(service.get_alliance_full(1, 2))

    assert result["error_code"] == "NETWORK_ERROR"
    [record] = _records(caplog, "not retried")
    assert record.levelno == logging.WARNING
