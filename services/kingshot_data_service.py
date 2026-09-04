"""Service client for read-only KingShot data API."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx


logger = logging.getLogger(__name__)


@dataclass
class _RankedAllianceBatch:
    """One reusable leaderboard snapshot and its in-flight tag resolutions."""

    status_code: Optional[int]
    candidates: list[Dict[str, int]]
    tasks: list[asyncio.Task[Optional[Dict[str, Any]]]]
    cleanup_handle: Optional[asyncio.TimerHandle] = None


class KingshotDataService:
    """Client for KingShot read-only data endpoints."""

    _ALLIANCE_POWER_BOARD_TYPE = 1
    _ALLIANCE_RESOLUTION_CONCURRENCY = 25
    _ALLIANCE_RESOLUTION_DEADLINE_SECONDS = 1.5
    _ALLIANCE_RESOLVER_MAX_LIFETIME_SECONDS = 10.0
    _ALLIANCE_BATCH_RETENTION_SECONDS = 30.0

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str = "https://ks.jeab.dev",
        timeout_seconds: int = 30,
        max_retries: int = 3,
    ):
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_retries = max_retries
        self._client: Optional[httpx.AsyncClient] = None
        self._ranked_alliance_batches: Dict[tuple[str, int], _RankedAllianceBatch] = {}

    async def __aenter__(self):
        """Initialize shared async client for context-manager usage."""
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(self._timeout_seconds),
            headers=self._build_headers(),
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Close shared async client."""
        await self.close()

    async def close(self) -> None:
        """Close the shared async client."""
        batches = list(self._ranked_alliance_batches.values())
        self._ranked_alliance_batches.clear()
        retained_tasks = []
        for batch in batches:
            if batch.cleanup_handle is not None:
                batch.cleanup_handle.cancel()
                batch.cleanup_handle = None
            retained_tasks.extend(batch.tasks)

        for task in retained_tasks:
            if not task.done():
                task.cancel()
        if retained_tasks:
            await asyncio.gather(*retained_tasks, return_exceptions=True)

        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def ensure_client(self) -> httpx.AsyncClient:
        """Initialize client lazily if not already created."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout_seconds),
                headers=self._build_headers(),
            )
        return self._client

    async def get_health(self) -> Dict[str, Any]:
        """Fetch unauthenticated KingShot Data API and gateway health."""
        return await self._request("GET", "/healthz", require_api_key=False)

    async def get_arena(self, uid: str) -> Dict[str, Any]:
        """Fetch arena team for internal player uid."""
        return await self._request("GET", f"/v1/arena/{uid}")

    async def get_player(self, uid: str | int) -> Dict[str, Any]:
        """Fetch player profile by internal player uid."""
        return await self._request("GET", f"/v1/players/{uid}")

    async def get_alliance(self, aid: str | int, kid: int | str) -> Dict[str, Any]:
        """Fetch alliance summary and roster."""
        return await self._request("GET", f"/v1/alliances/{aid}", params={"kid": str(kid)})

    async def get_alliance_full(self, aid: str | int, kid: int | str) -> Dict[str, Any]:
        """Fetch alliance summary and full member profiles."""
        return await self._request("GET", f"/v1/alliances/{aid}/full", params={"kid": str(kid)})

    async def get_ranked_alliances(
        self,
        kid: int | str,
        limit: int = 25,
        *,
        exhaustive: bool = False,
    ) -> Dict[str, Any]:
        """Fetch ranked alliances, optionally waiting for every tag resolution.

        Bounded calls retain unfinished work. A prompt exhaustive call with the same
        kingdom and limit reuses that batch instead of repeating its API requests.
        """
        requested_limit = max(1, int(limit))
        batch_key = (str(kid), requested_limit)
        batch = self._ranked_alliance_batches.get(batch_key)
        if batch is not None:
            self._retain_ranked_alliance_batch(batch_key, batch)

        if batch is None:
            batch_result = await self._start_ranked_alliance_batch(
                kid,
                requested_limit,
                batch_key,
            )
            if isinstance(batch_result, dict):
                return batch_result
            batch = batch_result

        if batch.tasks:
            if exhaustive:
                completion = asyncio.gather(*batch.tasks, return_exceptions=True)
                await asyncio.shield(completion)
            else:
                await asyncio.wait(
                    batch.tasks,
                    timeout=self._ALLIANCE_RESOLUTION_DEADLINE_SECONDS,
                )

        self._retain_ranked_alliance_batch(batch_key, batch)
        return self._ranked_alliance_batch_result(batch)

    async def _start_ranked_alliance_batch(
        self,
        kid: int | str,
        requested_limit: int,
        batch_key: tuple[str, int],
    ) -> _RankedAllianceBatch | Dict[str, Any]:
        """Fetch a leaderboard and start reusable alliance-resolution tasks."""
        board_result = await self.get_kingdom_board(
            self._ALLIANCE_POWER_BOARD_TYPE,
            kid,
            limit=requested_limit,
            resolve=False,
        )
        if not board_result.get("success"):
            return board_result

        board_payload = board_result.get("data")
        entries = board_payload.get("entries") if isinstance(board_payload, dict) else None
        if not isinstance(entries, list):
            entries = []

        candidates_by_aid: Dict[int, Dict[str, int]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue

            # Alliance boards use the generic leaderboard entry's `uid` field for aid.
            aid = self._positive_int(entry.get("aid")) or self._positive_int(entry.get("uid"))
            power = self._nonnegative_int(entry.get("score"))
            rank = self._positive_int(entry.get("rank"))
            if aid is None or power is None or rank is None:
                continue

            candidate = {"aid": aid, "power": power, "rank": rank}
            previous = candidates_by_aid.get(aid)
            if previous is None or (power, -rank) > (previous["power"], -previous["rank"]):
                candidates_by_aid[aid] = candidate

        candidates = sorted(
            candidates_by_aid.values(),
            key=lambda alliance: (-alliance["power"], alliance["rank"], alliance["aid"]),
        )[:requested_limit]
        semaphore = asyncio.Semaphore(self._ALLIANCE_RESOLUTION_CONCURRENCY)

        async def resolve(candidate: Dict[str, int]) -> Optional[Dict[str, Any]]:
            try:
                async with asyncio.timeout(self._ALLIANCE_RESOLVER_MAX_LIFETIME_SECONDS):
                    async with semaphore:
                        return await self._resolve_ranked_alliance(candidate, kid)
            except TimeoutError:
                logger.warning(
                    "Alliance tag resolution timed out for aid %s in kingdom %s",
                    candidate["aid"],
                    kid,
                )
                return None
            except Exception:
                logger.warning(
                    "Could not resolve alliance tag for aid %s in kingdom %s",
                    candidate["aid"],
                    kid,
                    exc_info=True,
                )
                return None

        resolution_tasks = [asyncio.create_task(resolve(candidate)) for candidate in candidates]
        status_code = board_result.get("status_code", 200)
        if not isinstance(status_code, int):
            status_code = None
        batch = _RankedAllianceBatch(
            status_code=status_code,
            candidates=candidates,
            tasks=resolution_tasks,
        )
        self._ranked_alliance_batches[batch_key] = batch
        for task in resolution_tasks:
            task.add_done_callback(
                lambda completed, key=batch_key, current=batch: self._ranked_alliance_task_done(
                    key,
                    current,
                    completed,
                )
            )
        self._retain_ranked_alliance_batch(batch_key, batch)
        return batch

    def _ranked_alliance_task_done(
        self,
        batch_key: tuple[str, int],
        batch: _RankedAllianceBatch,
        task: asyncio.Task[Optional[Dict[str, Any]]],
    ) -> None:
        """Consume task failures and retain a completed batch briefly for a waiter."""
        if not task.cancelled():
            try:
                error = task.exception()
            except asyncio.CancelledError:
                error = None
            if error is not None:
                logger.warning(
                    "Unexpected ranked-alliance resolution failure",
                    exc_info=(type(error), error, error.__traceback__),
                )

        if all(resolution.done() for resolution in batch.tasks):
            self._retain_ranked_alliance_batch(batch_key, batch)

    def _retain_ranked_alliance_batch(
        self,
        batch_key: tuple[str, int],
        batch: _RankedAllianceBatch,
    ) -> None:
        """Give a completed batch a short reuse window before removing it."""
        if self._ranked_alliance_batches.get(batch_key) is not batch:
            return
        if batch.cleanup_handle is not None:
            batch.cleanup_handle.cancel()
            batch.cleanup_handle = None
        if all(task.done() for task in batch.tasks):
            batch.cleanup_handle = asyncio.get_running_loop().call_later(
                self._ALLIANCE_BATCH_RETENTION_SECONDS,
                self._expire_ranked_alliance_batch,
                batch_key,
                batch,
            )

    def _expire_ranked_alliance_batch(
        self,
        batch_key: tuple[str, int],
        batch: _RankedAllianceBatch,
    ) -> None:
        """Remove a completed batch only when it is still the cached instance."""
        if self._ranked_alliance_batches.get(batch_key) is batch:
            self._ranked_alliance_batches.pop(batch_key, None)
        batch.cleanup_handle = None

    @staticmethod
    def _ranked_alliance_batch_result(batch: _RankedAllianceBatch) -> Dict[str, Any]:
        """Build a backward-compatible envelope with resolution progress metadata."""
        resolved = []
        for task in batch.tasks:
            if not task.done() or task.cancelled():
                continue
            try:
                alliance = task.result()
            except Exception:
                continue
            if alliance is not None:
                resolved.append(alliance)

        alliances = resolved
        alliances.sort(key=lambda alliance: (-alliance["power"], alliance["rank"], alliance["aid"]))
        resolution_complete = all(task.done() for task in batch.tasks)
        return {
            "success": True,
            "status_code": batch.status_code,
            "data": alliances,
            "partial": len(alliances) < len(batch.candidates),
            "resolution_complete": resolution_complete,
            "candidate_count": len(batch.candidates),
            "resolved_count": len(alliances),
        }

    async def _resolve_ranked_alliance(
        self,
        candidate: Dict[str, int],
        kid: int | str,
    ) -> Optional[Dict[str, Any]]:
        aid = candidate["aid"]
        alliance_result = await self.get_alliance(aid, kid)
        if not alliance_result.get("success"):
            return None

        payload = alliance_result.get("data")
        if not isinstance(payload, dict):
            return None

        members = payload.get("members")
        readable_members = (
            [member for member in members if isinstance(member, dict)]
            if isinstance(members, list)
            else []
        )
        member_count = len(readable_members)
        tag = self._alliance_tag(payload.get("abbr"))
        payload_aid = self._positive_int(payload.get("aid"))
        if tag is not None and (payload_aid is None or payload_aid == aid):
            return self._ranked_alliance(candidate, tag, payload.get("name"), member_count)

        if not isinstance(members, list):
            return None

        ordered_members = [
            *(member for member in readable_members if self._positive_int(member.get("rank")) == 5),
            *(member for member in readable_members if self._positive_int(member.get("rank")) != 5),
        ]
        representative_uids: list[int] = []
        for member in ordered_members:
            member_uid = self._positive_int(member.get("uid"))
            if member_uid is not None and member_uid not in representative_uids:
                representative_uids.append(member_uid)
            if len(representative_uids) == 3:
                break

        for member_uid in representative_uids:
            player_result = await self.get_player(member_uid)
            if not player_result.get("success"):
                continue

            player = player_result.get("data")
            alliance = player.get("alliance") if isinstance(player, dict) else None
            if not isinstance(alliance, dict) or self._positive_int(alliance.get("aid")) != aid:
                continue

            tag = self._alliance_tag(alliance.get("abbr"))
            if tag is not None:
                return self._ranked_alliance(candidate, tag, alliance.get("name"), member_count)
        return None

    @staticmethod
    def _ranked_alliance(
        candidate: Dict[str, int],
        tag: str,
        name: Any,
        member_count: int,
    ) -> Dict[str, Any]:
        readable_name = name.strip() if isinstance(name, str) and name.strip() else None
        return {
            **candidate,
            "abbr": tag,
            "name": readable_name,
            "member_count": member_count,
        }

    @staticmethod
    def _alliance_tag(value: Any) -> Optional[str]:
        if not isinstance(value, str) or len(value) != 3 or not value.isalnum():
            return None
        return value

    @staticmethod
    def _positive_int(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _nonnegative_int(value: Any) -> Optional[int]:
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed >= 0 else None

    async def get_player_by_fid(self, fid: str | int) -> Dict[str, Any]:
        """Fetch player profile by Governor ID (fid)."""
        return await self._request("GET", f"/v1/players/by-fid/{fid}")

    async def get_kingdom_board(
        self,
        board_type: int | str,
        kid: int | str,
        limit: int = 100,
        resolve: bool = False,
    ) -> Dict[str, Any]:
        """Fetch kingdom leaderboard for a board type."""
        return await self._request(
            "GET",
            f"/v1/leaderboards/kingdom/{board_type}",
            params={"kid": str(kid), "limit": str(limit), "resolve": str(resolve).lower()},
        )

    async def get_global_board(
        self,
        board_type: int | str,
        limit: int = 100,
        resolve: bool = False,
    ) -> Dict[str, Any]:
        """Fetch global leaderboard for a board type."""
        return await self._request(
            "GET",
            f"/v1/leaderboards/global/{board_type}",
            params={"limit": str(limit), "resolve": str(resolve).lower()},
        )

    async def search_leaderboard(
        self,
        board_type: int | str,
        uid: str,
        kid: int | str,
        *,
        aid: int | str = 0,
        rank_id: int | str = 0,
    ) -> Dict[str, Any]:
        """Search one player's leaderboard entry."""
        return await self._request(
            "GET",
            "/v1/leaderboards/search",
            params={
                "type": str(board_type),
                "uid": str(uid),
                "kid": str(kid),
                "aid": str(aid),
                "rank_id": str(rank_id),
            },
        )

    def _build_headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, str] | None = None,
        require_api_key: bool = True,
    ) -> Dict[str, Any]:
        if require_api_key and not self._api_key:
            logger.warning("KingShot Data API request blocked: missing API key")
            return {
                "success": False,
                "status_code": None,
                "error_code": "MISSING_API_KEY",
                "error_message": "KingShot Data API key is not configured.",
            }

        params = params or {}
        for attempt in range(1, self._max_retries + 1):
            try:
                client = await self.ensure_client()
                response = await client.request(method=method, url=path, params=params)
            except (httpx.NetworkError, httpx.TimeoutException) as exc:
                if attempt >= self._max_retries:
                    logger.error("Network error after retries while calling %s: %s", path, exc)
                    return {
                        "success": False,
                        "status_code": None,
                        "error_code": "NETWORK_ERROR",
                        "error_message": str(exc),
                    }

                await self._sleep_backoff(attempt)
                continue

            payload = self._safe_json(response)
            status_code = response.status_code
            if status_code == 200:
                return {
                    "success": True,
                    "status_code": status_code,
                    "data": payload,
                }

            if status_code in {401, 422}:
                error_code = "AUTH_ERROR" if status_code == 401 else "BAD_REQUEST"
                return {
                    "success": False,
                    "status_code": status_code,
                    "error_code": error_code,
                    "error_message": self._extract_error(payload, status_code),
                }

            if status_code in {502, 503, 504}:
                if attempt >= self._max_retries:
                    return {
                        "success": False,
                        "status_code": status_code,
                        "error_code": "GATEWAY_ERROR",
                        "error_message": self._extract_error(payload, status_code),
                    }

                await self._sleep_backoff(attempt)
                continue

            return {
                "success": False,
                "status_code": status_code,
                "error_code": "HTTP_ERROR",
                "error_message": self._extract_error(payload, status_code),
            }

        return {
            "success": False,
            "status_code": None,
            "error_code": "MAX_RETRIES_EXCEEDED",
            "error_message": "Request did not succeed after retrying.",
        }

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    @staticmethod
    def _extract_error(payload: Any, status_code: int) -> str:
        if isinstance(payload, dict):
            if isinstance(payload.get("error"), str):
                return payload["error"]
            if isinstance(payload.get("message"), str):
                return payload["message"]
        return f"HTTP {status_code}"

    @staticmethod
    async def _sleep_backoff(attempt: int) -> None:
        delay = 0.8 * (2 ** (attempt - 1))
        await asyncio.sleep(delay)
