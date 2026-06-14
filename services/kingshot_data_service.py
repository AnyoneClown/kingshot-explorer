"""Service client for read-only KingShot data API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

import httpx


logger = logging.getLogger(__name__)


class KingshotDataService:
    """Client for KingShot read-only data endpoints."""

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
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def close(self) -> None:
        """Close the shared async client."""
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

    async def get_arena(self, uid: str) -> Dict[str, Any]:
        """Fetch arena team for internal player uid."""
        return await self._request("GET", f"/v1/arena/{uid}")

    async def get_alliance(self, aid: str | int, kid: int | str) -> Dict[str, Any]:
        """Fetch alliance summary and roster."""
        return await self._request("GET", f"/v1/alliances/{aid}", params={"kid": str(kid)})

    async def get_alliance_full(self, aid: str | int, kid: int | str) -> Dict[str, Any]:
        """Fetch alliance summary and full member profiles."""
        return await self._request("GET", f"/v1/alliances/{aid}/full", params={"kid": str(kid)})

    async def get_player_by_fid(self, fid: str | int) -> Dict[str, Any]:
        """Fetch player profile by Governor ID (fid)."""
        return await self._request("GET", f"/v1/players/by-fid/{fid}")

    async def get_kingdom_board(self, board_type: int | str, kid: int | str, limit: int = 100) -> Dict[str, Any]:
        """Fetch kingdom leaderboard for a board type."""
        return await self._request(
            "GET",
            f"/v1/leaderboards/kingdom/{board_type}",
            params={"kid": str(kid), "limit": str(limit)},
        )

    async def get_global_board(self, board_type: int | str, limit: int = 100) -> Dict[str, Any]:
        """Fetch global leaderboard for a board type."""
        return await self._request(
            "GET",
            f"/v1/leaderboards/global/{board_type}",
            params={"limit": str(limit)},
        )

    async def search_leaderboard(self, board_type: int | str, uid: str, kid: int | str) -> Dict[str, Any]:
        """Search one player's leaderboard entry."""
        return await self._request(
            "GET",
            "/v1/leaderboards/search",
            params={"type": str(board_type), "uid": str(uid), "kid": str(kid)},
        )

    def _build_headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    async def _request(self, method: str, path: str, *, params: Dict[str, str] | None = None) -> Dict[str, Any]:
        if not self._api_key:
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
