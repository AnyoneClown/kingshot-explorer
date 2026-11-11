"""Bulk gift-code redemption shared by the automatic and manual runs.

Retry policy (the only retry layer)
    ``KingshotAPIClient`` makes exactly one paced HTTP attempt per call (global
    pacing: one request start every 2 s, the advertised 30/minute limit).
    ``GiftRedemptionService.redeem_with_retries`` decides retries from the mapped
    result: transient errors get ``REDEEM_MAX_RETRIES`` retries with exponential
    backoff, explicit rate limits get ``REDEEM_RATE_LIMIT_MAX_RETRIES`` retries with
    ~60 s waits. Final answers (success, already redeemed, invalid ID, requirement
    not met, code expired/unknown/exhausted) are never retried.

Persistence
    Each player's result is written as soon as it is known, so a restart or a
    cancelled job keeps every finished result. Automatic runs skip players whose
    outcome for the code is already recorded (success, already redeemed or a final
    failure), which is what lets a pending code resume where it stopped.

One job per code
    ``CodeJobGuard`` holds at most one running job per gift code, shared by the
    automatic run and ``/redeem``, so the same code is never redeemed twice in
    parallel.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence

logger = logging.getLogger(__name__)

STATUS_SUCCESS = "success"
STATUS_ALREADY_REDEEMED = "already_redeemed"
STATUS_API_REJECTED = "api_rejected"
STATUS_INVALID_ID = "invalid_id"
STATUS_SKIPPED = "skipped"

STATUS_ORDER = (
    STATUS_SUCCESS,
    STATUS_ALREADY_REDEEMED,
    STATUS_API_REJECTED,
    STATUS_INVALID_ID,
    STATUS_SKIPPED,
)

# The single label map used by progress cards, summaries and announcements.
STATUS_LABELS: dict[str, str] = {
    STATUS_SUCCESS: "✅ Success",
    STATUS_ALREADY_REDEEMED: "🔄 Already redeemed",
    STATUS_API_REJECTED: "🚫 API rejected",
    STATUS_INVALID_ID: "🆔 Invalid ID",
    STATUS_SKIPPED: "⏭️ Skipped",
}

ALREADY_REDEEMED_ERROR_CODES = frozenset({"ALREADY_REDEEMED", "ALREADY_REDEEMED_BY_API"})
INVALID_ID_ERROR_CODES = frozenset(
    {
        "INVALID_ID",
        "INVALID_USER_ID",
        "INVALID_PLAYER_ID",
        "KINGDOM_MISMATCH",
        "MISSING_KINGDOM",
        "PLAYER_LOOKUP_FAILED",
    }
)
GLOBAL_CODE_ERROR_CODES = frozenset(
    {"GIFT_CODE_EXPIRED", "GIFT_CODE_NOT_FOUND", "GIFT_CODE_CLAIM_LIMIT_REACHED"}
)
# Logged failures that will not change if the same player retries the same code.
FINAL_PLAYER_ERROR_CODES = frozenset(
    {
        "INVALID_PLAYER_ID",
        "KINGDOM_MISMATCH",
        "CASTLE_LEVEL_TOO_LOW",
        "SPEND_REQUIREMENT_NOT_MET",
        "VIP_REQUIREMENT_NOT_MET",
    }
)
# Failures that mean the in-game account itself is unusable (roster health report).
BROKEN_ACCOUNT_ERROR_CODES = frozenset({"INVALID_PLAYER_ID", "INVALID_USER_ID", "KINGDOM_MISMATCH"})

_RETRYABLE_ERROR_CODES = frozenset(
    {
        "API_ERROR",
        "UNEXPECTED_ERROR",
        "UNKNOWN_ERROR",
        "TIMEOUT_RETRY",
        "RATE_LIMITED",
        # The player lookup service was unreachable: says nothing about the account.
        "PLAYER_LOOKUP_UNAVAILABLE",
        "KINGDOM_REFRESH_UNAVAILABLE",
        "40019",
        "429",
        "500",
        "502",
        "503",
        "504",
    }
)
_RETRYABLE_PHRASES = (
    "rate limit",
    "too frequent",
    "too many requests",
    "timeout",
    "timed out",
    "temporar",
    "network",
    "not login",
    "http error 429",
    "http error 5",
    "max retries",
)

# KingShot Data API failures that mean the service itself is unreachable.
_LOOKUP_OUTAGE_ERROR_CODES = frozenset({"NETWORK_ERROR", "GATEWAY_ERROR", "MAX_RETRIES_EXCEEDED"})

JOB_AUTO = "auto"
JOB_MANUAL = "manual"
STOP_CANCELLED = "cancelled"
STOP_SHUTDOWN = "shutdown"

ProgressCallback = Callable[[list[dict], str], Awaitable[None]]


def status_label(status: str | None) -> str:
    return STATUS_LABELS.get(status or "", STATUS_LABELS[STATUS_API_REJECTED])


def count_statuses(results: Iterable[dict]) -> dict[str, int]:
    counts = dict.fromkeys(STATUS_ORDER, 0)
    for result in results:
        status = result.get("status_category")
        counts[status if status in counts else STATUS_API_REJECTED] += 1
    return counts


def format_status_counts(results: Iterable[dict], *, hide_zero: bool = False) -> str:
    counts = count_statuses(results)
    return "\n".join(
        f"**{STATUS_LABELS[status]}:** {counts[status]}"
        for status in STATUS_ORDER
        if counts[status] or not hide_zero
    )


def has_success(results: Iterable[dict]) -> bool:
    return any(result.get("status_category") == STATUS_SUCCESS for result in results)


def _error_code(result: dict) -> str:
    raw_code = result.get("error_code")
    if raw_code is None and isinstance(result.get("error_details"), dict):
        raw_code = result["error_details"].get("err_code")
    return str(raw_code or "").upper()


def categorize_result(result: dict) -> str:
    """Map an API/database redemption result into one status category."""
    if result.get("skipped", False):
        return STATUS_SKIPPED
    if result.get("success", False):
        return STATUS_SUCCESS
    if (
        result.get("already_redeemed", False)
        or result.get("already_redeemed_by_api", False)
        or result.get("error_code") in ALREADY_REDEEMED_ERROR_CODES
    ):
        return STATUS_ALREADY_REDEEMED
    if result.get("error_code") in INVALID_ID_ERROR_CODES:
        return STATUS_INVALID_ID
    return STATUS_API_REJECTED


def is_global_code_rejection(result: dict) -> bool:
    """Return whether one response proves the code is unusable for every player."""
    return result.get("global_code_error") is True or result.get("error_code") in GLOBAL_CODE_ERROR_CODES


def is_rate_limited_result(result: dict) -> bool:
    """Return whether the upstream explicitly rate-limited this redemption."""
    if _error_code(result) in {"RATE_LIMITED", "40019", "429"}:
        return True
    message = str(result.get("message") or "").lower()
    return (
        "429" in message
        or "rate limit" in message
        or "too frequent" in message
        or "too many requests" in message
    )


def is_retryable_result(result: dict) -> bool:
    """Return whether a failed redemption looks transient enough to retry."""
    if categorize_result(result) != STATUS_API_REJECTED:
        return False
    code = _error_code(result)
    if code in _RETRYABLE_ERROR_CODES:
        return True
    try:
        numeric_code = int(code)
        # Only HTTP-style statuses: the API's own business codes (40021, ...) are five
        # digits and say something about the player or the code, not about an outage.
        if numeric_code == 429 or 500 <= numeric_code < 600:
            return True
    except ValueError:
        pass
    message = str(result.get("message") or "").lower()
    return any(phrase in message for phrase in _RETRYABLE_PHRASES)


def is_kingdom_mismatch_result(result: dict) -> bool:
    """Return whether CenturyGame rejected the supplied player/kingdom pair."""
    upstream_code = (
        result["error_details"].get("err_code") if isinstance(result.get("error_details"), dict) else None
    )
    api_status = " ".join(str(result.get("api_status") or "").strip().rstrip(".").upper().split())
    return (
        str(result.get("error_code") or "").upper() == "KINGDOM_MISMATCH"
        or str(upstream_code or "") == "40020"
        or api_status == "USER INFO ERROR"
    )


async def _pause(delay: float, stop_event: asyncio.Event | None) -> bool:
    """Sleep ``delay`` seconds; return True if ``stop_event`` fired first."""
    if stop_event is None:
        await asyncio.sleep(delay)
        return False
    if stop_event.is_set():
        return True
    sleeper = asyncio.ensure_future(asyncio.sleep(delay))
    waiter = asyncio.ensure_future(stop_event.wait())
    try:
        await asyncio.wait({sleeper, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (sleeper, waiter):
            if not task.done():
                task.cancel()
    return stop_event.is_set()


# ---------------------------------------------------------------------- jobs


@dataclass(eq=False)
class RedemptionJob:
    """One running redemption of one code (automatic or manual)."""

    gift_code: str
    kind: str
    guild_id: int | None = None
    requester_id: int | None = None
    all_servers: bool = False
    total: int = 0
    stop_event: asyncio.Event = field(default_factory=asyncio.Event)
    stop_reason: str | None = None
    stopped_by: int | None = None
    task: asyncio.Task | None = None
    started_at: float = field(default_factory=time.time)

    @property
    def key(self) -> str:
        return CodeJobGuard.key(self.gift_code)

    @property
    def stop_requested(self) -> bool:
        return self.stop_event.is_set()

    def request_stop(self, *, reason: str = STOP_CANCELLED, user_id: int | None = None) -> bool:
        """Ask the job to stop after the players in flight; False if already stopping."""
        if self.stop_event.is_set():
            return False
        self.stop_reason = reason
        self.stopped_by = user_id
        self.stop_event.set()
        return True


class CodeJobGuard:
    """At most one running redemption job per gift code (auto and manual share it)."""

    def __init__(self) -> None:
        self._jobs: dict[str, RedemptionJob] = {}

    @staticmethod
    def key(gift_code: str) -> str:
        return str(gift_code).strip().casefold()

    def get(self, gift_code: str) -> RedemptionJob | None:
        return self._jobs.get(self.key(gift_code))

    def claim(self, job: RedemptionJob) -> RedemptionJob | None:
        """Register ``job``; return the job already holding the code instead, if any."""
        existing = self._jobs.get(job.key)
        if existing is not None:
            return existing
        self._jobs[job.key] = job
        return None

    def release(self, job: RedemptionJob) -> None:
        if self._jobs.get(job.key) is job:
            del self._jobs[job.key]

    def attach(self, job: RedemptionJob, task: asyncio.Task) -> None:
        """Bind the job's task; the code is released when the task finishes."""
        job.task = task
        task.add_done_callback(lambda _task: self.release(job))

    def active_jobs(self) -> list[RedemptionJob]:
        return list(self._jobs.values())

    def manual_job_for_guild(self, guild_id: int | None) -> RedemptionJob | None:
        return next(
            (job for job in self._jobs.values() if job.kind == JOB_MANUAL and job.guild_id == guild_id),
            None,
        )

    async def shutdown(self, *, grace_seconds: float = 8.0, cancel_wait_seconds: float = 2.0) -> None:
        """Stop every job: let in-flight players finish (results are saved), then cancel."""
        jobs = self.active_jobs()
        for job in jobs:
            job.request_stop(reason=STOP_SHUTDOWN)
        tasks = [job.task for job in jobs if job.task is not None and not job.task.done()]
        if not tasks:
            return
        logger.info("Stopping %s gift-code redemption job(s) for shutdown", len(tasks))
        _, pending = await asyncio.wait(tasks, timeout=grace_seconds)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=cancel_wait_seconds)


# ------------------------------------------------------------- roster health


@dataclass
class CodeReach:
    gift_code: str
    reached: int
    failed: int
    pending: bool = False


@dataclass
class FailingPlayer:
    player_id: str
    player_name: Optional[str]
    failed_codes: int
    error_code: str


@dataclass
class RosterHealthReport:
    enabled_players: int
    codes: list[CodeReach]
    failing_players: list[FailingPlayer]


def summarize_roster_health(
    codes: Sequence[tuple[str, bool]],
    roster: Sequence[Any],
    rows: Iterable[Any],
    *,
    min_failures: int = 2,
) -> RosterHealthReport:
    """Summarize recent redemptions for one server roster.

    ``codes`` is ``(gift_code, still_pending)`` newest first, ``roster`` the server's
    entries (``player_id``, ``player_name``, ``enabled``) and ``rows`` the attempt
    rows (``gift_code``, ``player_id``, ``success``, ``error_code``). A player is
    flagged when its account failed as invalid on at least ``min_failures`` of
    those codes and none of them reached it.
    """
    enabled_ids = {str(entry.player_id) for entry in roster if entry.enabled}
    names = {str(entry.player_id): entry.player_name for entry in roster}
    reached: set[tuple[str, str]] = set()
    failed: set[tuple[str, str]] = set()
    broken: dict[tuple[str, str], str] = {}
    code_names = {code for code, _ in codes}
    for row in rows:
        key = (str(row.gift_code), str(row.player_id))
        if key[0] not in code_names or key[1] not in names:
            continue
        if row.success or row.error_code in ALREADY_REDEEMED_ERROR_CODES:
            reached.add(key)
        else:
            failed.add(key)
            if row.error_code in BROKEN_ACCOUNT_ERROR_CODES:
                broken[key] = str(row.error_code)

    code_reach = [
        CodeReach(
            gift_code=code,
            reached=sum((code, player_id) in reached for player_id in enabled_ids),
            failed=sum((code, player_id) in failed and (code, player_id) not in reached for player_id in enabled_ids),
            pending=pending,
        )
        for code, pending in codes
    ]
    failing: list[FailingPlayer] = []
    for player_id in sorted(enabled_ids):
        if any((code, player_id) in reached for code, _ in codes):
            continue
        errors = [broken[(code, player_id)] for code, _ in codes if (code, player_id) in broken]
        if len(errors) >= min_failures:
            failing.append(FailingPlayer(player_id, names.get(player_id), len(errors), errors[0]))
    return RosterHealthReport(enabled_players=len(enabled_ids), codes=code_reach, failing_players=failing)


def find_broken_players(
    codes: Sequence[str],
    players: Sequence[Any],
    rows: Iterable[Any],
    *,
    current_code: str | None = None,
    min_failures: int = 3,
) -> list[FailingPlayer]:
    """Players whose account keeps failing as invalid across recent codes.

    A player is returned when ``current_code`` (default: the first of ``codes``)
    failed as an invalid account (wrong Governor ID, kingdom mismatch), at least
    ``min_failures`` of the codes failed that way, and none of them reached the
    player. One bad code or a temporary outage therefore never disables anyone.
    """
    if not codes:
        return []
    current = current_code or codes[0]
    names = {str(player.player_id): getattr(player, "player_name", None) for player in players}
    code_names = set(codes)
    reached: set[str] = set()
    broken: dict[tuple[str, str], str] = {}
    for row in rows:
        code, player_id = str(row.gift_code), str(row.player_id)
        if code not in code_names or player_id not in names:
            continue
        if row.success or row.error_code in ALREADY_REDEEMED_ERROR_CODES:
            reached.add(player_id)
        elif row.error_code in BROKEN_ACCOUNT_ERROR_CODES:
            broken[(code, player_id)] = str(row.error_code)
    failing: list[FailingPlayer] = []
    for player_id in sorted(names):
        if player_id in reached or (current, player_id) not in broken:
            continue
        errors = [broken[(code, player_id)] for code in codes if (code, player_id) in broken]
        if len(errors) >= min_failures:
            failing.append(FailingPlayer(player_id, names[player_id], len(errors), errors[0]))
    return failing


# ------------------------------------------------------------------- service


class GiftRedemptionService:
    """Redeems one code for many players with bounded concurrency and one retry layer."""

    REDEEM_MAX_RETRIES = 2
    REDEEM_RATE_LIMIT_MAX_RETRIES = 3
    REDEEM_RETRY_DELAY_SECONDS = 1.0
    REDEEM_RETRY_MAX_DELAY_SECONDS = 30.0
    REDEEM_RATE_LIMIT_DELAY_SECONDS = 60.0
    REDEEM_RATE_LIMIT_MAX_DELAY_SECONDS = 60.0
    REDEEM_CONCURRENCY = 3
    # Automatic passes end after this many temporary failures in a row.
    TRANSIENT_ABORT_AFTER = 6

    def __init__(self, gift_code_service: Any, tracking_service: Any, kingshot_data_service: Any = None):
        self._gift_code_service = gift_code_service
        self._tracking_service = tracking_service
        self._kingshot_data_service = kingshot_data_service

    # ---------------------------------------------------------- result builders

    @staticmethod
    def already_redeemed_result(player: Any, gift_code: str) -> dict[str, Any]:
        return {
            "player_id": str(player.player_id),
            "player_name": getattr(player, "player_name", None),
            "success": False,
            "message": f"Gift code `{gift_code}` was already redeemed for this player.",
            "error_code": "ALREADY_REDEEMED",
            "already_redeemed": True,
            "status_category": STATUS_ALREADY_REDEEMED,
            "should_log": False,
        }

    @staticmethod
    def invalid_player_id_result(player: Any) -> dict[str, Any]:
        return {
            "player_id": str(player.player_id),
            "player_name": getattr(player, "player_name", None),
            "success": False,
            "message": "Invalid player ID format",
            "error_code": "INVALID_ID",
            "status_category": STATUS_INVALID_ID,
            "should_log": False,
        }

    @staticmethod
    def player_lookup_failure_result(player: Any, message: str, *, unavailable: bool = False) -> dict[str, Any]:
        """A failed profile lookup.

        ``unavailable`` means the lookup service itself failed (timeout, outage): that
        is temporary and retried, unlike a player the service does not know.
        """
        return {
            "player_id": str(player.player_id),
            "player_name": getattr(player, "player_name", None),
            "success": False,
            "message": message,
            "error_code": "PLAYER_LOOKUP_UNAVAILABLE" if unavailable else "PLAYER_LOOKUP_FAILED",
            "status_category": STATUS_API_REJECTED if unavailable else STATUS_INVALID_ID,
            "lookup_unavailable": unavailable,
            "should_log": False,
        }

    @staticmethod
    def skipped_after_global_error_result(player: Any, global_error: dict[str, Any]) -> dict[str, Any]:
        reason = str(global_error.get("message") or "The gift code was rejected.")
        return {
            "player_id": str(player.player_id),
            "player_name": getattr(player, "player_name", None),
            "success": False,
            "message": f"Skipped without an API request: {reason}",
            "error_code": "SKIPPED_GLOBAL_CODE_ERROR",
            "status_category": STATUS_SKIPPED,
            "skipped": True,
            "should_log": False,
        }

    @staticmethod
    def recorded_outcome_result(player: Any, row: Any, actor_user_id: int) -> dict[str, Any]:
        """Result for a player whose outcome for this code is already stored.

        A success recorded by the same actor (the automatic run before a restart)
        still counts as a success in the summary; anyone else's counts as already
        redeemed.
        """
        if row.success:
            status = STATUS_SUCCESS if row.user_id == actor_user_id else STATUS_ALREADY_REDEEMED
        elif row.error_code in ALREADY_REDEEMED_ERROR_CODES:
            status = STATUS_ALREADY_REDEEMED
        else:
            status = categorize_result({"error_code": row.error_code})
        return {
            "player_id": str(player.player_id),
            "player_name": getattr(player, "player_name", None),
            "success": status == STATUS_SUCCESS,
            "message": row.response_message or "Result recorded by an earlier run.",
            "error_code": row.error_code,
            "status_category": status,
            "recorded": True,
            "should_log": False,
        }

    # -------------------------------------------------------- player resolution

    @staticmethod
    def cached_redemption_player(player: Any) -> dict[str, Any] | None:
        """Build a redemption profile from the registered player's database values."""
        cached_kingdom = getattr(player, "kingdom", None)
        if cached_kingdom in (None, "", 0, "0"):
            return None
        return {
            "playerId": str(player.player_id),
            "playerUid": getattr(player, "player_uid", None),
            "name": getattr(player, "player_name", None),
            "kingdom": str(cached_kingdom),
            "level": getattr(player, "castle_level", None),
        }

    async def fetch_redemption_player_from_jeab(
        self, player: Any
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Fetch the current player profile from the KingShot Data API."""
        player_id = str(player.player_id)
        if self._kingshot_data_service is None:
            return None, self.player_lookup_failure_result(player, "Jeab player lookup is unavailable.")

        unavailable = False
        try:
            response = await self._kingshot_data_service.get_player_by_fid(player_id)
            data = response.get("data") if isinstance(response, dict) else None
            if isinstance(response, dict) and response.get("success") is True and isinstance(data, dict):
                if data.get("error"):
                    return None, self.player_lookup_failure_result(
                        player, f"Jeab could not resolve this player: {data['error']}"
                    )
                kingdom = data.get("kid")
                if kingdom not in (None, "", 0, "0"):
                    return {
                        "playerId": str(data.get("fid") or player_id),
                        "playerUid": str(data["uid"]) if data.get("uid") is not None else None,
                        "name": data.get("name") or getattr(player, "player_name", None),
                        "kingdom": str(kingdom),
                        "level": data.get("stove_lv") or data.get("lv"),
                    }, None
                lookup_error = "Jeab returned no kingdom for this player."
            else:
                # Only a failing service is temporary. "Not found", a bad request or a
                # missing API key will not get better by retrying, and treating them as
                # temporary would let one bad account hold up the code for everyone.
                unavailable = self._lookup_service_failed(response)
                lookup_error = str(
                    (response.get("error_message") if isinstance(response, dict) else None)
                    or (response.get("message") if isinstance(response, dict) else None)
                    or "Jeab player lookup failed."
                )
        except Exception as exc:
            unavailable = True
            lookup_error = f"Jeab player lookup failed: {exc}"
            logger.warning("Jeab player lookup crashed for player %s: %s", player_id, exc, exc_info=True)

        return None, self.player_lookup_failure_result(player, lookup_error, unavailable=unavailable)

    @staticmethod
    def _lookup_service_failed(response: Any) -> bool:
        """Whether a failed Data API answer means an outage (network, gateway, 429, 5xx)."""
        if not isinstance(response, dict):
            return True
        if str(response.get("error_code") or "").upper() in _LOOKUP_OUTAGE_ERROR_CODES:
            return True
        status = response.get("status_code")
        return isinstance(status, int) and (status == 429 or 500 <= status < 600)

    async def resolve_redemption_player(
        self, player: Any
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Use the database kingdom, fetching Jeab only when it is missing."""
        cached_profile = self.cached_redemption_player(player)
        if cached_profile is not None:
            return cached_profile, None
        return await self.fetch_redemption_player_from_jeab(player)

    # -------------------------------------------------------------- persistence

    @staticmethod
    def build_metadata_rows(player_id: str, result: dict[str, Any], added_by_user_id: int) -> list[dict[str, Any]]:
        player_profile = result.get("player_profile")
        if not isinstance(player_profile, dict):
            return []

        resolved_player_id = str(player_profile.get("playerId") or player_id)
        raw_uid = player_profile.get("playerUid") or player_profile.get("uid")
        resolved_name = player_profile.get("name")
        resolved_kingdom = str(player_profile["kingdom"]) if player_profile.get("kingdom") is not None else None
        resolved_level = str(player_profile["level"]) if player_profile.get("level") is not None else None
        rows = [
            {
                "player_id": resolved_player_id,
                "player_uid": str(raw_uid) if raw_uid is not None else None,
                "player_name": resolved_name,
                "kingdom": resolved_kingdom,
                "castle_level": resolved_level,
                # No owner: redemption only refreshes existing profiles. A profile
                # removed while the run was going must not be recreated.
            }
        ]
        if resolved_player_id != str(player_id):
            rows.append(
                {
                    "player_id": str(player_id),
                    "player_name": resolved_name,
                    "kingdom": resolved_kingdom,
                    "castle_level": resolved_level,
                }
            )
        return rows

    async def persist_results(
        self,
        *,
        gift_code: str,
        results: Sequence[dict[str, Any]],
        actor_user_id: int,
        guild_id: int | None,
        channel_id: int | None,
    ) -> None:
        """Write attempt logs first (they drive resume), then refresh player metadata."""
        log_rows: list[dict[str, Any]] = []
        metadata_rows: list[dict[str, Any]] = []
        for result in results:
            player_id = str(result["player_id"])
            metadata_rows.extend(self.build_metadata_rows(player_id, result, actor_user_id))
            if result.get("should_log", True):
                log_rows.append(
                    {
                        "user_id": actor_user_id,
                        "player_id": player_id,
                        "gift_code": gift_code,
                        "success": result.get("success", False),
                        "response_message": result.get("message"),
                        "error_code": result.get("error_code"),
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                    }
                )
        if log_rows:
            await self._tracking_service.log_gift_code_redemptions_many(log_rows)
        if metadata_rows:
            await self._tracking_service.sync_player_metadata_many(metadata_rows)

    async def load_recorded_outcomes(self, gift_code: str) -> dict[str, Any]:
        """Best stored outcome per player for ``gift_code`` (success > already > final failure)."""
        rows = await self._gift_code_service.get_recorded_outcomes(
            gift_code, ALREADY_REDEEMED_ERROR_CODES | FINAL_PLAYER_ERROR_CODES
        )

        def rank(row: Any) -> int:
            if row.success:
                return 2
            return 1 if row.error_code in ALREADY_REDEEMED_ERROR_CODES else 0

        outcomes: dict[str, Any] = {}
        for row in rows:
            player_id = str(row.player_id)
            current = outcomes.get(player_id)
            if current is None or rank(row) >= rank(current):
                outcomes[player_id] = row
        return outcomes

    # ---------------------------------------------------------------- redemption

    async def redeem_with_retries(
        self,
        player_id_int: int,
        kingdom_id: str,
        gift_code: str,
        player_id_for_logs: str,
        rate_limit_wait: Callable[[str, float], Awaitable[None]] | None = None,
        stop_event: asyncio.Event | None = None,
    ) -> dict[str, Any]:
        """Redeem a code, retrying transient/API failures only (the single retry layer)."""
        standard_max_attempts = self.REDEEM_MAX_RETRIES + 1
        attempt = 0
        while True:
            attempt += 1
            try:
                last_result = await self._gift_code_service.redeem_gift_code_remote(
                    player_id_int,
                    gift_code,
                    kingdom_id=kingdom_id,
                )
            except Exception as exc:
                logger.error(
                    "Redeem attempt %s crashed for player %s and code '%s': %s",
                    attempt,
                    player_id_for_logs,
                    gift_code,
                    exc,
                    exc_info=True,
                )
                last_result = {
                    "success": False,
                    "message": "Unexpected error occurred",
                    "error_code": "UNEXPECTED_ERROR",
                }

            rate_limited = is_rate_limited_result(last_result)
            max_attempts = self.REDEEM_RATE_LIMIT_MAX_RETRIES + 1 if rate_limited else standard_max_attempts
            if (
                not is_retryable_result(last_result)
                or attempt >= max_attempts
                or (stop_event is not None and stop_event.is_set())
            ):
                normalized_result = dict(last_result)
                normalized_result.setdefault("attempts", attempt)
                normalized_result.setdefault("retries", max(0, attempt - 1))
                return normalized_result

            if rate_limited:
                retry_delay = min(
                    self.REDEEM_RATE_LIMIT_MAX_DELAY_SECONDS,
                    self.REDEEM_RATE_LIMIT_DELAY_SECONDS * attempt,
                ) + random.uniform(1.0, 4.0)
            else:
                retry_delay = min(
                    self.REDEEM_RETRY_MAX_DELAY_SECONDS,
                    self.REDEEM_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)),
                ) + random.uniform(0, 0.5)

            logger.warning(
                "Redeem attempt %s/%s failed for player %s and code '%s' with retryable status. "
                "Retrying in %.1fs (error_code=%s, message=%s)",
                attempt,
                max_attempts,
                player_id_for_logs,
                gift_code,
                retry_delay,
                last_result.get("error_code"),
                last_result.get("message"),
            )
            if rate_limited and rate_limit_wait is not None:
                await rate_limit_wait(player_id_for_logs, retry_delay)
            try:
                stopped = await _pause(retry_delay, stop_event)
            finally:
                if rate_limited and rate_limit_wait is not None:
                    await rate_limit_wait(player_id_for_logs, 0)
            if stopped:
                normalized_result = dict(last_result)
                normalized_result.setdefault("attempts", attempt)
                normalized_result.setdefault("retries", max(0, attempt - 1))
                return normalized_result

    async def _redeem_player(
        self,
        player: Any,
        player_id_int: int,
        gift_code: str,
        *,
        rate_limit_wait: Callable[[str, float], Awaitable[None]],
        stop_event: asyncio.Event | None,
    ) -> dict[str, Any]:
        cached_profile = self.cached_redemption_player(player)
        player_profile, lookup_failure = await self.resolve_redemption_player(player)
        if lookup_failure is not None:
            return lookup_failure
        assert player_profile is not None

        result = await self.redeem_with_retries(
            player_id_int=player_id_int,
            kingdom_id=str(player_profile["kingdom"]),
            gift_code=gift_code,
            player_id_for_logs=str(player.player_id),
            rate_limit_wait=rate_limit_wait,
            stop_event=stop_event,
        )

        # A transfer makes the database kingdom stale. Only refresh Jeab after
        # CenturyGame rejects a cached player/kingdom pair, then retry once with
        # the newly reported kingdom.
        if cached_profile is not None and is_kingdom_mismatch_result(result):
            refreshed_profile, refresh_failure = await self.fetch_redemption_player_from_jeab(player)
            if refreshed_profile is not None:
                cached_kingdom = str(player_profile["kingdom"])
                refreshed_kingdom = str(refreshed_profile["kingdom"])
                player_profile = refreshed_profile
                if refreshed_kingdom != cached_kingdom:
                    logger.info(
                        "Player %s moved from kingdom %s to %s; retrying gift code '%s' once",
                        player.player_id,
                        cached_kingdom,
                        refreshed_kingdom,
                        gift_code,
                    )
                    first_attempts = int(result.get("attempts") or 1)
                    result = await self.redeem_with_retries(
                        player_id_int=player_id_int,
                        kingdom_id=refreshed_kingdom,
                        gift_code=gift_code,
                        player_id_for_logs=str(player.player_id),
                        rate_limit_wait=rate_limit_wait,
                        stop_event=stop_event,
                    )
                    total_attempts = first_attempts + int(result.get("attempts") or 1)
                    result = {
                        **result,
                        "attempts": total_attempts,
                        "retries": max(0, total_attempts - 1),
                        "kingdom_refreshed": True,
                        "previous_kingdom": cached_kingdom,
                        "refreshed_kingdom": refreshed_kingdom,
                    }
                else:
                    logger.warning(
                        "CenturyGame rejected kingdom %s for player %s, but Jeab returned the same kingdom",
                        cached_kingdom,
                        player.player_id,
                    )
            else:
                logger.warning(
                    "CenturyGame rejected cached kingdom %s for player %s and Jeab refresh failed: %s",
                    player_profile["kingdom"],
                    player.player_id,
                    refresh_failure.get("message") if refresh_failure else "unknown error",
                )
                if refresh_failure and refresh_failure.get("lookup_unavailable"):
                    # The new kingdom could not be looked up, so this is not final:
                    # a later pass refreshes the kingdom and tries again.
                    result = {
                        **result,
                        "error_code": "KINGDOM_REFRESH_UNAVAILABLE",
                        "message": "The kingdom did not match and could not be refreshed right now.",
                    }

        return {
            "player_id": str(player.player_id),
            "player_name": player_profile.get("name") or getattr(player, "player_name", None),
            "success": result.get("success", False),
            "message": result.get("message", "Unknown error"),
            "error_code": result.get("error_code"),
            "api_status": result.get("api_status"),
            "global_code_error": result.get("global_code_error", False),
            "already_redeemed": result.get("already_redeemed", False),
            "already_redeemed_by_api": result.get("already_redeemed_by_api", False),
            "status_category": categorize_result(result),
            "player_profile": player_profile,
            "attempts": result.get("attempts"),
            "retries": result.get("retries", 0),
            "redemption_attempted": True,
            "should_log": True,
        }

    async def run_bulk(
        self,
        *,
        gift_code: str,
        players: Sequence[Any],
        actor_user_id: int,
        guild_id: int | None,
        channel_id: int | None,
        progress: ProgressCallback | None = None,
        stop_event: asyncio.Event | None = None,
        use_recorded_outcomes: bool = False,
        stop_on_transient_probe: bool = False,
    ) -> list[dict[str, Any]]:
        """Redeem one code for ``players`` (each once) and return their results in order.

        With ``stop_on_transient_probe`` (automatic runs), a probe that fails with a
        temporary error (outage, rate limit, 5xx) ends the pass right away instead of
        sending the whole roster into a failing API; the caller retries later.

        One usable player is probed first, so an expired/unknown/exhausted code costs
        one request; then players run in batches of ``REDEEM_CONCURRENCY``. Every
        result is saved as soon as it is known. When ``stop_event`` is set, no new
        players start; in-flight ones finish (retry waits are cut short) and the
        players never started are left out of the results.

        ``use_recorded_outcomes`` (automatic runs) skips players whose outcome for this
        code is already stored, including final failures; manual runs only skip
        players who already have the code.
        """
        seen_ids: set[str] = set()
        unique_players: list[Any] = []
        for player in players:
            if str(player.player_id) not in seen_ids:
                seen_ids.add(str(player.player_id))
                unique_players.append(player)
        if use_recorded_outcomes:
            recorded = await self.load_recorded_outcomes(gift_code)
            already_redeemed: set[str] = set()
        else:
            recorded = {}
            already_redeemed = await self._gift_code_service.get_redeemed_players(None, gift_code)

        indexed_results: list[tuple[int, dict[str, Any]]] = []
        pending: list[tuple[int, Any, int]] = []
        for index, player in enumerate(unique_players):
            player_id = str(player.player_id)
            if player_id in recorded:
                indexed_results.append((index, self.recorded_outcome_result(player, recorded[player_id], actor_user_id)))
                continue
            if player_id in already_redeemed:
                indexed_results.append((index, self.already_redeemed_result(player, gift_code)))
                continue
            try:
                player_id_int = int(player_id)
            except ValueError:
                logger.error("Invalid player ID format during bulk redeem: %s", player_id)
                indexed_results.append((index, self.invalid_player_id_result(player)))
                continue
            pending.append((index, player, player_id_int))

        waiting: dict[str, float] = {}

        async def report(phase: str = "Redeeming players") -> None:
            if progress is None:
                return
            if waiting:
                phase = f"Rate limited · retry <t:{int(max(waiting.values()))}:R> · {len(waiting)} waiting"
            await progress([result for _, result in indexed_results], phase)

        async def rate_limit_wait(player_id: str, delay: float) -> None:
            if delay:
                waiting[player_id] = time.time() + delay
            else:
                waiting.pop(player_id, None)
            await report()

        async def redeem_and_report(item: tuple[int, Any, int]) -> tuple[int, dict[str, Any]]:
            index, player, player_id_int = item
            result = await self._redeem_player(
                player,
                player_id_int,
                gift_code,
                rate_limit_wait=rate_limit_wait,
                stop_event=stop_event,
            )
            try:
                await self.persist_results(
                    gift_code=gift_code,
                    results=[result],
                    actor_user_id=actor_user_id,
                    guild_id=guild_id,
                    channel_id=channel_id,
                )
            except Exception:
                logger.exception("Could not save the redemption result of player %s for '%s'", player.player_id, gift_code)
            indexed_results.append((index, result))
            await report()
            return index, result

        def stopped() -> bool:
            return stop_event is not None and stop_event.is_set()

        await report()
        next_pending_index = 0
        global_error: dict[str, Any] | None = None

        # Probe one usable player before scheduling bulk work. A bad, expired, or
        # exhausted code therefore costs one redemption request, not 500+.
        while next_pending_index < len(pending) and not stopped():
            _, probe_result = await redeem_and_report(pending[next_pending_index])
            next_pending_index += 1
            if is_global_code_rejection(probe_result):
                global_error = probe_result
                break
            if probe_result.get("redemption_attempted"):
                break

        transient_probe = (
            stop_on_transient_probe
            and global_error is None
            and bool(indexed_results)
            and is_retryable_result(indexed_results[-1][1])
        )
        if transient_probe:
            logger.warning(
                "Probe for code '%s' failed temporarily; ending this pass, %s players stay unattempted",
                gift_code,
                len(pending) - next_pending_index,
            )

        if global_error is None and not transient_probe:
            consecutive_transient = 0
            while next_pending_index < len(pending) and not stopped():
                batch = pending[next_pending_index : next_pending_index + self.REDEEM_CONCURRENCY]
                batch_results = await asyncio.gather(*(redeem_and_report(item) for item in batch))
                next_pending_index += len(batch)
                global_error = next(
                    (result for _, result in batch_results if is_global_code_rejection(result)),
                    None,
                )
                if global_error is not None:
                    break
                if all(is_retryable_result(result) for _, result in batch_results):
                    consecutive_transient += len(batch_results)
                else:
                    consecutive_transient = 0
                if stop_on_transient_probe and consecutive_transient >= self.TRANSIENT_ABORT_AFTER:
                    # An outage or rate-limit storm began mid-run. Each further player
                    # would wait through every retry (about a minute each), so end the
                    # pass; the caller retries the unreached players later.
                    logger.warning(
                        "Ending the pass for code '%s' after %s temporary failures in a row; %s players stay "
                        "unattempted",
                        gift_code,
                        consecutive_transient,
                        len(pending) - next_pending_index,
                    )
                    break

        if global_error is not None and next_pending_index < len(pending):
            logger.warning(
                "Stopping bulk redemption for code '%s' after global rejection %s; skipping %s queued players",
                gift_code,
                global_error.get("error_code"),
                len(pending) - next_pending_index,
            )
            indexed_results.extend(
                (index, self.skipped_after_global_error_result(player, global_error))
                for index, player, _ in pending[next_pending_index:]
            )
        elif stopped() and next_pending_index < len(pending):
            logger.info(
                "Redemption of '%s' stopped on request; %s players were not started",
                gift_code,
                len(pending) - next_pending_index,
            )

        await report("Finishing")
        return [result for _, result in sorted(indexed_results, key=lambda item: item[0])]

    async def probe_code(
        self,
        *,
        gift_code: str,
        player: Any,
        actor_user_id: int,
        guild_id: int | None,
        channel_id: int | None,
    ) -> dict[str, Any]:
        """Redeem ``gift_code`` for one player and save the result (code validation)."""
        try:
            player_id_int = int(str(player.player_id))
        except ValueError:
            return self.invalid_player_id_result(player)

        async def no_wait(_player_id: str, _delay: float) -> None:
            return None

        result = await self._redeem_player(
            player, player_id_int, gift_code, rate_limit_wait=no_wait, stop_event=None
        )
        try:
            await self.persist_results(
                gift_code=gift_code,
                results=[result],
                actor_user_id=actor_user_id,
                guild_id=guild_id,
                channel_id=channel_id,
            )
        except Exception:
            logger.exception("Could not save the probe result of player %s for '%s'", player.player_id, gift_code)
        return result

    async def find_broken_players(
        self,
        players: Sequence[Any],
        *,
        current_code: str | None = None,
        code_limit: int = 5,
        min_failures: int = 3,
    ) -> list[FailingPlayer]:
        """Accounts among ``players`` that failed as invalid on the latest codes."""
        if not players:
            return []
        codes = [str(code.code) for code in await self._gift_code_service.get_recent_gift_codes(code_limit)]
        if current_code is not None and current_code not in codes:
            # "The last ``code_limit`` codes, including the current one": the current
            # code takes the place of the oldest, it does not widen the window.
            codes = [current_code, *codes][: max(1, code_limit)]
        rows = await self._gift_code_service.get_redemption_rows(
            codes, [str(player.player_id) for player in players]
        )
        return find_broken_players(codes, players, rows, current_code=current_code, min_failures=min_failures)

    # ----------------------------------------------------------- roster health

    async def build_roster_health(self, roster: Sequence[Any], *, code_limit: int = 5) -> RosterHealthReport:
        codes = await self._gift_code_service.get_recent_gift_codes(code_limit)
        code_rows = [(str(code.code), getattr(code, "auto_redeem_status", None) == "pending") for code in codes]
        rows = await self._gift_code_service.get_redemption_rows(
            [code for code, _ in code_rows],
            [str(entry.player_id) for entry in roster],
        )
        return summarize_roster_health(code_rows, roster, rows)
