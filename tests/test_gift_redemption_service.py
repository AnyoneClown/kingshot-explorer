"""Resumable bulk redemption, the per-code guard, the single retry layer and labels."""

import asyncio
from types import SimpleNamespace

import pytest

from services.gift_redemption_service import (
    STATUS_ALREADY_REDEEMED,
    STATUS_API_REJECTED,
    STATUS_INVALID_ID,
    STATUS_LABELS,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    STOP_SHUTDOWN,
    CodeJobGuard,
    GiftRedemptionService,
    RedemptionJob,
    categorize_result,
    format_status_counts,
    summarize_roster_health,
)


class FakeGiftCodeService:
    def __init__(self, recorded=(), responses=None):
        self.recorded = list(recorded)
        self.responses = responses or {}
        self.calls = []
        self.redeemed = set()

    async def get_recorded_outcomes(self, gift_code, final_error_codes):
        self.final_error_codes = set(final_error_codes)
        return self.recorded

    async def get_redeemed_players(self, session, gift_code):
        return self.redeemed

    async def redeem_gift_code_remote(self, player_id, gift_code, kingdom_id=None):
        self.calls.append(player_id)
        response = self.responses.get(player_id, {"success": True, "message": "OK"})
        if callable(response):
            return await response()
        return response


class FakeTracking:
    def __init__(self):
        self.log_batches = []

    async def log_gift_code_redemptions_many(self, rows):
        self.log_batches.append(rows)

    async def sync_player_metadata_many(self, rows):
        return len(rows)


def player(player_id):
    return SimpleNamespace(player_id=str(player_id), player_name=f"P{player_id}", kingdom="830")


def row(player_id, *, success=False, error_code=None, user_id=99):
    return SimpleNamespace(player_id=str(player_id), success=success, error_code=error_code,
                           response_message="earlier", user_id=user_id)


def test_one_label_map_covers_every_category():
    assert STATUS_LABELS == {
        STATUS_SUCCESS: "✅ Success",
        STATUS_ALREADY_REDEEMED: "🔄 Already redeemed",
        STATUS_API_REJECTED: "🚫 API rejected",
        STATUS_INVALID_ID: "🆔 Invalid ID",
        STATUS_SKIPPED: "⏭️ Skipped",
    }
    # Kingdom mismatches and lookup failures are the same "Invalid ID" category everywhere.
    assert categorize_result({"error_code": "KINGDOM_MISMATCH"}) == STATUS_INVALID_ID
    assert categorize_result({"error_code": "PLAYER_LOOKUP_FAILED"}) == STATUS_INVALID_ID
    counts = format_status_counts([{"status_category": STATUS_INVALID_ID}, {"status_category": "unknown"}])
    assert "**🆔 Invalid ID:** 1" in counts and "**🚫 API rejected:** 1" in counts
    assert format_status_counts([], hide_zero=True) == ""


@pytest.mark.asyncio
async def test_auto_run_resumes_by_skipping_recorded_outcomes():
    gift = FakeGiftCodeService(recorded=[
        row(1, success=True, user_id=99),          # claimed by this auto run before a restart
        row(2, success=True, user_id=7),           # claimed by someone else
        row(3, error_code="INVALID_PLAYER_ID"),    # final failure: not retried
        row(4, error_code="ALREADY_REDEEMED_BY_API"),
    ])
    tracking = FakeTracking()
    service = GiftRedemptionService(gift, tracking)

    results = await service.run_bulk(
        gift_code="CODE", players=[player(i) for i in (1, 2, 3, 4, 5, 5)],
        actor_user_id=99, guild_id=None, channel_id=None, use_recorded_outcomes=True,
    )

    assert gift.calls == [5]  # only the unhandled player, once
    assert [r["status_category"] for r in results] == [
        STATUS_SUCCESS, STATUS_ALREADY_REDEEMED, STATUS_INVALID_ID, STATUS_ALREADY_REDEEMED, STATUS_SUCCESS,
    ]
    assert [len(batch) for batch in tracking.log_batches] == [1]
    assert {"INVALID_PLAYER_ID", "CASTLE_LEVEL_TOO_LOW", "ALREADY_REDEEMED_BY_API"} <= gift.final_error_codes
    assert "RATE_LIMITED" not in gift.final_error_codes


@pytest.mark.asyncio
async def test_each_result_is_saved_before_the_next_player_starts():
    tracking = FakeTracking()
    saved_before_call = []
    gift = FakeGiftCodeService()

    async def redeem(player_id, gift_code, kingdom_id=None):
        saved_before_call.append(len(tracking.log_batches))
        return {"success": True, "message": "OK"}

    gift.redeem_gift_code_remote = redeem
    service = GiftRedemptionService(gift, tracking)
    service.REDEEM_CONCURRENCY = 1
    await service.run_bulk(gift_code="CODE", players=[player(i) for i in (1, 2, 3)],
                           actor_user_id=1, guild_id=5, channel_id=6)
    assert saved_before_call == [0, 1, 2]
    assert tracking.log_batches[0][0]["guild_id"] == 5


@pytest.mark.asyncio
async def test_stop_keeps_finished_results_and_skips_unstarted_players():
    stop = asyncio.Event()
    tracking = FakeTracking()
    gift = FakeGiftCodeService()

    async def redeem(player_id, gift_code, kingdom_id=None):
        gift.calls.append(player_id)
        if player_id == 1:
            stop.set()  # e.g. Cancel pressed while the probe runs
        return {"success": True, "message": "OK"}

    gift.redeem_gift_code_remote = redeem
    service = GiftRedemptionService(gift, tracking)
    results = await service.run_bulk(gift_code="CODE", players=[player(i) for i in range(1, 6)],
                                     actor_user_id=1, guild_id=None, channel_id=None, stop_event=stop)
    assert gift.calls == [1]
    assert [r["player_id"] for r in results] == ["1"]
    assert len(tracking.log_batches) == 1


@pytest.mark.asyncio
async def test_rate_limit_wait_ends_early_when_stopping(monkeypatch):
    stop = asyncio.Event()
    gift = FakeGiftCodeService(responses={1: {"success": False, "error_code": "RATE_LIMITED", "message": "slow"}})
    service = GiftRedemptionService(gift, FakeTracking())
    waits = []

    async def rate_limit_wait(player_id, delay):
        waits.append(delay)
        if delay:
            stop.set()

    result = await asyncio.wait_for(
        service.redeem_with_retries(1, "830", "CODE", "1", rate_limit_wait=rate_limit_wait, stop_event=stop),
        timeout=2,
    )
    assert result["error_code"] == "RATE_LIMITED" and result["attempts"] == 1
    assert gift.calls == [1]
    assert waits[-1] == 0


@pytest.mark.asyncio
async def test_code_guard_is_shared_case_insensitive_and_released_with_the_task():
    guard = CodeJobGuard()
    auto = RedemptionJob(gift_code="Code1", kind="auto")
    assert guard.claim(auto) is None
    manual = RedemptionJob(gift_code=" code1 ", kind="manual", guild_id=5)
    assert guard.claim(manual) is auto

    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    guard.attach(auto, task)
    assert guard.get("CODE1") is auto
    release.set()
    await task
    await asyncio.sleep(0)
    assert guard.get("code1") is None
    assert guard.claim(manual) is None and guard.get("code1") is manual


@pytest.mark.asyncio
async def test_guard_shutdown_lets_jobs_finish_then_cancels_stragglers():
    guard = CodeJobGuard()
    polite = RedemptionJob(gift_code="A", kind="auto")
    stubborn = RedemptionJob(gift_code="B", kind="manual")
    for job in (polite, stubborn):
        guard.claim(job)

    async def obeys(job):
        await job.stop_event.wait()
        return "stopped"

    stubborn_started = asyncio.Event()

    async def ignores():
        stubborn_started.set()
        await asyncio.sleep(60)

    guard.attach(polite, asyncio.create_task(obeys(polite)))
    guard.attach(stubborn, asyncio.create_task(ignores()))
    await stubborn_started.wait()
    await guard.shutdown(grace_seconds=0.05, cancel_wait_seconds=0.5)

    assert polite.stop_reason == STOP_SHUTDOWN and polite.task.result() == "stopped"
    assert stubborn.task.cancelled()


def test_roster_health_counts_reach_and_flags_repeated_invalid_accounts():
    roster = [
        SimpleNamespace(player_id="1", player_name="Good", enabled=True),
        SimpleNamespace(player_id="2", player_name="Gone", enabled=True),
        SimpleNamespace(player_id="3", player_name="Once", enabled=True),
        SimpleNamespace(player_id="4", player_name="Off", enabled=False),
    ]
    rows = [
        SimpleNamespace(gift_code="NEW", player_id="1", success=True, error_code=None),
        SimpleNamespace(gift_code="OLD", player_id="1", success=False, error_code="ALREADY_REDEEMED_BY_API"),
        SimpleNamespace(gift_code="NEW", player_id="2", success=False, error_code="INVALID_PLAYER_ID"),
        SimpleNamespace(gift_code="OLD", player_id="2", success=False, error_code="INVALID_PLAYER_ID"),
        SimpleNamespace(gift_code="NEW", player_id="3", success=False, error_code="INVALID_PLAYER_ID"),
        SimpleNamespace(gift_code="NEW", player_id="4", success=False, error_code="INVALID_PLAYER_ID"),
        SimpleNamespace(gift_code="OLD", player_id="4", success=False, error_code="INVALID_PLAYER_ID"),
        SimpleNamespace(gift_code="NEW", player_id="other-server", success=True, error_code=None),
    ]
    report = summarize_roster_health([("NEW", True), ("OLD", False)], roster, rows)
    assert report.enabled_players == 3
    assert [(c.gift_code, c.reached, c.failed, c.pending) for c in report.codes] == [
        ("NEW", 1, 2, True), ("OLD", 1, 1, False),
    ]
    # Only enabled players with ≥2 invalid-account failures and no success are flagged.
    assert [(p.player_id, p.failed_codes, p.error_code) for p in report.failing_players] == [
        ("2", 2, "INVALID_PLAYER_ID"),
    ]


@pytest.mark.asyncio
async def test_transient_probe_failure_ends_an_auto_pass_without_touching_the_roster():
    outage = {"success": False, "error_code": "API_ERROR", "message": "Service unavailable"}
    gift = FakeGiftCodeService(responses={1: outage})
    service = GiftRedemptionService(gift, FakeTracking())
    service.REDEEM_RETRY_DELAY_SECONDS = 0
    service.REDEEM_RETRY_MAX_DELAY_SECONDS = 0

    results = await service.run_bulk(
        gift_code="CODE", players=[player(i) for i in (1, 2, 3)],
        actor_user_id=99, guild_id=None, channel_id=None,
        use_recorded_outcomes=True, stop_on_transient_probe=True,
    )

    assert {str(call) for call in gift.calls} == {"1"}  # only the probe (with its retries), nobody else
    assert len(results) == 1


@pytest.mark.asyncio
async def test_transient_probe_failure_does_not_stop_manual_runs():
    outage = {"success": False, "error_code": "API_ERROR", "message": "Service unavailable"}
    gift = FakeGiftCodeService(responses={1: outage})
    service = GiftRedemptionService(gift, FakeTracking())
    service.REDEEM_RETRY_DELAY_SECONDS = 0
    service.REDEEM_RETRY_MAX_DELAY_SECONDS = 0

    await service.run_bulk(
        gift_code="CODE", players=[player(i) for i in (1, 2)],
        actor_user_id=99, guild_id=None, channel_id=None,
    )

    assert "2" in {str(call) for call in gift.calls}
