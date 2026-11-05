"""KvK prep dossier: everything worth knowing about two kingdoms before a KvK.

Per kingdom (cached together for ``CACHE_TTL_SECONDS``):

- castle / throne: current owner (and whether a foreign kingdom holds it), king,
  recent reigns, most-crowned kings, ruling alliance's KvK wins
  (``GET /v1/kingdoms/{kid}/castle?kvk=true&resolve=true``);
- top alliances by power (board type 1, tags resolved from rosters);
- top players by power (board type 3) and kills (board type 4), ``resolve=true``;
- kingshot.net KvK match history summary (``KVKService``).

Per ordered kingdom pair: kingshot.net direct (head-to-head) match history.

Every source is fetched independently with its own timeout; a failed source becomes
an ``{"ok": False, "error": ...}`` section instead of failing the dossier. Results
with a failed section are cached briefly so a recovering source is retried soon.
Concurrent requests for the same kingdom share one in-flight load. Only targeted
reads are made (no map sweeps), with a small cap on concurrent Data API calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

ALLIANCE_POWER_BOARD = 1
PERSONAL_POWER_BOARD = 3
KILL_COUNT_BOARD = 4

KINGDOM_SECTIONS = ("castle", "alliances", "top_power", "top_kills", "kvk_history")

_ERROR_LABELS = {
    "MISSING_API_KEY": "API not configured",
    "AUTH_ERROR": "API key rejected",
    "BAD_REQUEST": "request rejected",
    "GATEWAY_ERROR": "game gateway error",
    "NETWORK_ERROR": "network error",
    "MAX_RETRIES_EXCEEDED": "no response",
}


def section_ok(data: Any) -> Dict[str, Any]:
    return {"ok": True, "data": data, "error": None}


def section_error(message: str) -> Dict[str, Any]:
    return {"ok": False, "data": None, "error": message}


class KvKDossierService:
    """Build and cache KvK prep dossiers from the Data API and kingshot.net."""

    CACHE_TTL_SECONDS = 600.0
    PARTIAL_CACHE_TTL_SECONDS = 60.0
    SOURCE_TIMEOUT_SECONDS = 25.0
    DATA_API_CONCURRENCY = 3
    MAX_CACHE_ENTRIES = 128
    TOP_LIMIT = 5
    RECENT_REIGNS = 3
    TOP_KINGS = 2
    RECENT_DIRECT_MATCHES = 3

    def __init__(
        self,
        kingshot_data_service: Any,
        kvk_service: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._data = kingshot_data_service
        self._kvk = kvk_service
        self._clock = clock
        self._cache: Dict[tuple, tuple[float, Any]] = {}
        self._inflight: Dict[tuple, asyncio.Task] = {}
        self._data_api_slots = asyncio.Semaphore(self.DATA_API_CONCURRENCY)

    async def build_dossier(self, kingdom_a: int, kingdom_b: int) -> Dict[str, Any]:
        """Assemble both kingdoms' intel and their direct match history."""
        intel_a, intel_b, head_to_head = await asyncio.gather(
            self.get_kingdom_intel(kingdom_a),
            self.get_kingdom_intel(kingdom_b),
            self.get_head_to_head(kingdom_a, kingdom_b),
        )
        return {"kingdom_a": intel_a, "kingdom_b": intel_b, "head_to_head": head_to_head}

    async def get_kingdom_intel(self, kid: int) -> Dict[str, Any]:
        return await self._cached(
            ("kingdom", int(kid)),
            lambda: self._load_kingdom(int(kid)),
            # Partial sections (e.g. some alliance tags timed out) only get the short TTL.
            lambda intel: all(intel[name]["ok"] and not intel[name].get("partial") for name in KINGDOM_SECTIONS),
        )

    async def get_head_to_head(self, kingdom_a: int, kingdom_b: int) -> Dict[str, Any]:
        return await self._cached(
            ("head_to_head", int(kingdom_a), int(kingdom_b)),
            lambda: self._guard("KvK head-to-head", self._load_head_to_head(int(kingdom_a), int(kingdom_b))),
            lambda section: section["ok"],
        )

    def clear_cache(self) -> None:
        self._cache.clear()

    # ------------------------------------------------------------------ cache

    async def _cached(
        self,
        key: tuple,
        loader: Callable[[], Awaitable[Any]],
        is_complete: Callable[[Any], bool],
    ) -> Any:
        entry = self._cache.get(key)
        if entry is not None and entry[0] > self._clock():
            return entry[1]

        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(self._load_and_store(key, loader, is_complete))
            self._inflight[key] = task
        # A cancelled caller (e.g. an expired interaction) must not cancel the shared load.
        return await asyncio.shield(task)

    async def _load_and_store(
        self,
        key: tuple,
        loader: Callable[[], Awaitable[Any]],
        is_complete: Callable[[Any], bool],
    ) -> Any:
        try:
            value = await loader()
            ttl = self.CACHE_TTL_SECONDS if is_complete(value) else self.PARTIAL_CACHE_TTL_SECONDS
            self._store(key, value, ttl)
            return value
        finally:
            self._inflight.pop(key, None)

    def _store(self, key: tuple, value: Any, ttl: float) -> None:
        now = self._clock()
        for stale in [cached for cached, (expires, _) in self._cache.items() if expires <= now]:
            del self._cache[stale]
        while len(self._cache) >= self.MAX_CACHE_ENTRIES:
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = (now + ttl, value)

    async def _guard(self, label: str, work: Awaitable[Dict[str, Any]]) -> Dict[str, Any]:
        """Bound one source by time and turn any failure into an error section."""
        try:
            async with asyncio.timeout(self.SOURCE_TIMEOUT_SECONDS):
                return await work
        except TimeoutError:
            logger.warning("KvK dossier source timed out: %s", label)
            return section_error("timed out")
        except Exception:
            logger.warning("KvK dossier source failed: %s", label, exc_info=True)
            return section_error("unexpected error")

    # ---------------------------------------------------------------- loaders

    async def _load_kingdom(self, kid: int) -> Dict[str, Any]:
        castle, alliances, top_power, top_kills, kvk_history = await asyncio.gather(
            self._guard(f"castle K{kid}", self._load_castle(kid)),
            self._guard(f"alliances K{kid}", self._load_alliances(kid)),
            self._guard(f"top power K{kid}", self._load_board(kid, PERSONAL_POWER_BOARD)),
            self._guard(f"top kills K{kid}", self._load_board(kid, KILL_COUNT_BOARD)),
            self._guard(f"KvK history K{kid}", self._load_kvk_history(kid)),
        )
        return {
            "kid": kid,
            "fetched_at": int(time.time()),
            "castle": castle,
            "alliances": alliances,
            "top_power": top_power,
            "top_kills": top_kills,
            "kvk_history": kvk_history,
        }

    async def _data_call(self, method: str, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        if self._data is None:
            return {"success": False, "error_code": "MISSING_API_KEY"}
        async with self._data_api_slots:
            return await getattr(self._data, method)(*args, **kwargs)

    async def _load_castle(self, kid: int) -> Dict[str, Any]:
        result = await self._data_call("get_kingdom_castle", kid, kvk=True, resolve=True)
        kvk_included = True
        if not result.get("success") and result.get("status_code") in {500, 502}:
            # kvk=true resolves the king's alliance first; that extra lookup can fail
            # on its own, so retry once for the plain snapshot.
            result = await self._data_call("get_kingdom_castle", kid)
            kvk_included = False
        if not result.get("success"):
            return section_error(self._error_label(result))
        payload = result.get("data")
        if not isinstance(payload, dict):
            return section_error("unexpected response")
        return section_ok(self._normalize_castle(kid, payload, kvk_included))

    async def _load_alliances(self, kid: int) -> Dict[str, Any]:
        result = await self._data_call("get_ranked_alliances", kid, self.TOP_LIMIT, exhaustive=True)
        if not result.get("success"):
            return section_error(self._error_label(result))
        rows = []
        for alliance in result.get("data") or []:
            if not isinstance(alliance, dict):
                continue
            rows.append(
                {
                    "aid": _int(alliance.get("aid")),
                    "abbr": _text(alliance.get("abbr")),
                    "name": _text(alliance.get("name")),
                    "power": _int(alliance.get("power")),
                    "member_count": _int(alliance.get("member_count")),
                }
            )
        section = section_ok(rows[: self.TOP_LIMIT])
        if result.get("partial"):
            section["partial"] = True
        return section

    async def _load_board(self, kid: int, board_type: int) -> Dict[str, Any]:
        result = await self._data_call("get_kingdom_board", board_type, kid, limit=self.TOP_LIMIT, resolve=True)
        if not result.get("success"):
            return section_error(self._error_label(result))
        payload = result.get("data")
        entries = payload.get("entries") if isinstance(payload, dict) else payload
        if not isinstance(entries, list):
            return section_error("unexpected response")
        rows = []
        for index, entry in enumerate(entry for entry in entries if isinstance(entry, dict)):
            alliance = entry.get("alliance") if isinstance(entry.get("alliance"), dict) else {}
            rows.append(
                {
                    "rank": _int(entry.get("rank")) or index + 1,
                    "name": _text(entry.get("name")),
                    "abbr": _text(alliance.get("abbr")),
                    "score": _int(entry.get("score")),
                }
            )
        rows.sort(key=lambda row: row["rank"])
        return section_ok(rows[: self.TOP_LIMIT])

    async def _load_kvk_history(self, kid: int) -> Dict[str, Any]:
        result = await self._kvk.get_kingdom_stats(kid)
        if not result.get("success"):
            return section_error("match history unavailable")
        summary = result.get("data") if isinstance(result.get("data"), dict) else {}
        return section_ok(_match_summary(summary, recent=1))

    async def _load_head_to_head(self, kingdom_a: int, kingdom_b: int) -> Dict[str, Any]:
        result = await self._kvk.get_head_to_head(kingdom_a, kingdom_b)
        if not result.get("success"):
            return section_error("match history unavailable")
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        summary_a = data.get("kingdom_a") if isinstance(data.get("kingdom_a"), dict) else {}
        summary_b = data.get("kingdom_b") if isinstance(data.get("kingdom_b"), dict) else {}
        matches = [match for match in data.get("matches") or [] if isinstance(match, dict)]
        return section_ok(
            {
                "match_count": len(matches),
                "kingdom_a": _match_summary(summary_a, recent=0),
                "kingdom_b": _match_summary(summary_b, recent=0),
                "recent": matches[: self.RECENT_DIRECT_MATCHES],
            }
        )

    # ------------------------------------------------------------- normalizers

    @classmethod
    def _normalize_castle(cls, kid: int, payload: Dict[str, Any], kvk_included: bool) -> Dict[str, Any]:
        owner_kid = _int(payload.get("owner_kid"))
        foreign_owner = payload.get("foreign_owner")
        if not isinstance(foreign_owner, bool):
            foreign_owner = owner_kid is not None and owner_kid != kid

        king_payload = payload.get("king") if isinstance(payload.get("king"), dict) else None
        king = None
        if king_payload and (king_payload.get("uid") or king_payload.get("name")):
            home_kid = _int(king_payload.get("home_kid"))
            king = {
                "name": _text(king_payload.get("name")),
                "abbr": _text(king_payload.get("abbr")),
                "alliance_name": _text(king_payload.get("aname")),
                "home_kid": home_kid,
                "is_foreign": home_kid is not None and home_kid != kid,
            }

        history = [record for record in payload.get("history") or [] if isinstance(record, dict)]
        history.sort(key=lambda record: _int(record.get("ts")) or 0)
        recent_reigns = []
        for record in reversed(history[-cls.RECENT_REIGNS:]):
            home_kid = _int(record.get("home_kid"))
            invader = record.get("is_invader")
            recent_reigns.append(
                {
                    "name": _text(record.get("name")),
                    "abbr": _text(record.get("abbr")),
                    "home_kid": home_kid,
                    "ts": _int(record.get("ts")),
                    "is_cross_mode": bool(record.get("is_cross_mode")),
                    "is_invader": bool(invader) if isinstance(invader, bool) else (home_kid is not None and home_kid != kid),
                }
            )

        tallies = [tally for tally in payload.get("kings") or [] if isinstance(tally, dict)]
        tallies.sort(key=lambda tally: -(_int(tally.get("king_terms")) or 0))
        top_kings = [
            {
                "name": _text(tally.get("name")),
                "abbr": _text(tally.get("abbr")),
                "home_kid": _int(tally.get("home_kid")),
                "king_terms": _int(tally.get("king_terms")) or 0,
                "high_king_terms": _int(tally.get("high_king_terms")) or 0,
                "invader_terms": _int(tally.get("invader_terms")) or 0,
                "power": _int(tally.get("power")),
            }
            for tally in tallies[: cls.TOP_KINGS]
        ]

        kvk = None
        kvk_payload = payload.get("kvk") if isinstance(payload.get("kvk"), dict) else None
        if kvk_payload is not None:
            opponents = [item for item in kvk_payload.get("opponents") or [] if isinstance(item, dict)]
            opponents.sort(key=lambda item: _int(item.get("ts")) or 0)
            victories = _int(kvk_payload.get("victories"))
            kvk = {
                "victories": victories if victories is not None else len(opponents),
                "last_opponent_kid": _int(opponents[-1].get("opponent_kid")) if opponents else None,
                "last_ts": _int(opponents[-1].get("ts")) if opponents else None,
            }

        return {
            "kid": kid,
            "owner_kid": owner_kid,
            "foreign_owner": foreign_owner,
            "castle_name": _text(payload.get("castle_name")),
            "king": king,
            "term_count": _int(payload.get("count")),
            "recent_reigns": recent_reigns,
            "top_kings": top_kings,
            "kvk": kvk,
            "kvk_included": kvk_included,
        }

    @staticmethod
    def _error_label(result: Dict[str, Any]) -> str:
        label = _ERROR_LABELS.get(str(result.get("error_code")))
        if label:
            return label
        status_code = result.get("status_code")
        return f"HTTP {status_code}" if status_code else "request failed"


def _match_summary(summary: Dict[str, Any], *, recent: int) -> Dict[str, Any]:
    keys = ("matchCount", "wins", "losses", "prepWins", "prepLosses", "winRate", "prepWinRate", "currentStreak")
    result = {key: summary.get(key) for key in keys}
    history = summary.get("history") if isinstance(summary.get("history"), list) else []
    result["recent"] = [match for match in history if isinstance(match, dict)][:recent]
    return result


def _int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None
