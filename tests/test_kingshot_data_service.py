from __future__ import annotations

import asyncio

import httpx
import pytest

from services.kingshot_data_service import KingshotDataService


@pytest.mark.asyncio
async def test_health_check_does_not_require_api_key():
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "ok", "connected": True})

    service = KingshotDataService(api_key=None, base_url="https://ks.example")
    service._client = httpx.AsyncClient(
        base_url="https://ks.example",
        headers=service._build_headers(),
        transport=httpx.MockTransport(handle),
    )

    try:
        result = await service.get_health()
    finally:
        await service.close()

    assert result == {
        "success": True,
        "status_code": 200,
        "data": {"status": "ok", "connected": True},
    }
    assert requests[0].url.path == "/healthz"
    assert "X-API-Key" not in requests[0].headers
    assert requests[0].content == b""


@pytest.mark.asyncio
async def test_get_ranked_alliances_orders_by_power_and_resolves_tags():
    requests: list[httpx.Request] = []
    board = {
        "type": 1,
        "kid": 830,
        "entries": [
            {"rank": 2, "uid": 2002, "score": 400},
            {"rank": 1, "aid": "1001", "uid": 9999, "score": 500},
            {"rank": 3, "uid": 3003, "score": 300},
            {"rank": 4, "uid": 4004, "score": 200},
            {"rank": 5, "uid": 5005, "score": 100},
            {"rank": 6, "uid": 6006, "score": 50},
            {"rank": 9, "uid": 1001, "score": 10},
            {"rank": 10, "uid": 0, "score": 1},
        ],
    }

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/v1/leaderboards/kingdom/1":
            return httpx.Response(200, json=board)
        if request.url.path == "/v1/alliances/1001":
            return httpx.Response(
                200,
                json={
                    "aid": 1001,
                    "abbr": "TOP",
                    "name": " Top Alliance ",
                    "members": [{"uid": 10, "rank": 2}, {"uid": 11, "rank": 3}],
                },
            )
        if request.url.path == "/v1/alliances/2002":
            return httpx.Response(
                200,
                json={
                    "aid": None,
                    "abbr": None,
                    "members": [{"uid": 20, "rank": 2}, {"uid": 25, "rank": 5}],
                },
            )
        if request.url.path == "/v1/alliances/3003":
            return httpx.Response(
                200,
                json={"aid": 3003, "abbr": "A-3", "members": [{"uid": 30, "rank": 2}]},
            )
        if request.url.path == "/v1/alliances/4004":
            return httpx.Response(503, json={"error": "gateway unavailable"})
        if request.url.path == "/v1/alliances/5005":
            return httpx.Response(
                200,
                json={
                    "aid": 9999,
                    "abbr": "BAD",
                    "members": [{"uid": 56, "rank": 2}, {"uid": 55, "rank": 5}],
                },
            )
        if request.url.path == "/v1/alliances/6006":
            return httpx.Response(
                200,
                json={
                    "aid": 6006,
                    "abbr": "LONG",
                    "members": [
                        {"uid": 61, "rank": 2},
                        {"uid": 62, "rank": 2},
                        {"uid": 63, "rank": 2},
                        {"uid": 65, "rank": 5},
                    ],
                },
            )
        if request.url.path == "/v1/players/25":
            return httpx.Response(
                200,
                json={"alliance": {"aid": "2002", "abbr": "SEC", "name": "Second"}},
            )
        if request.url.path == "/v1/players/30":
            return httpx.Response(
                200,
                json={"alliance": {"aid": 3003, "abbr": "3RD", "name": "Third"}},
            )
        if request.url.path == "/v1/players/55":
            return httpx.Response(
                200,
                json={"alliance": {"aid": 9999, "abbr": "BAD", "name": "Wrong"}},
            )
        if request.url.path == "/v1/players/56":
            return httpx.Response(
                200,
                json={"alliance": {"aid": 5005, "abbr": "FIF", "name": "Fifth"}},
            )
        if request.url.path in {"/v1/players/61", "/v1/players/62", "/v1/players/65"}:
            return httpx.Response(200, json={"alliance": None})
        raise AssertionError(f"Unexpected request: {request.url}")

    service = KingshotDataService(
        api_key="secret",
        base_url="https://ks.example",
        max_retries=1,
    )
    service._client = httpx.AsyncClient(
        base_url="https://ks.example",
        headers=service._build_headers(),
        transport=httpx.MockTransport(handle),
    )

    try:
        result = await service.get_ranked_alliances("830", limit=10)
        exhaustive_result = await service.get_ranked_alliances(
            "830",
            limit=10,
            exhaustive=True,
        )
    finally:
        await service.close()

    assert result == {
        "success": True,
        "status_code": 200,
        "data": [
            {
                "aid": 1001,
                "power": 500,
                "rank": 1,
                "abbr": "TOP",
                "name": "Top Alliance",
                "member_count": 2,
            },
            {
                "aid": 2002,
                "power": 400,
                "rank": 2,
                "abbr": "SEC",
                "name": "Second",
                "member_count": 2,
            },
            {
                "aid": 3003,
                "power": 300,
                "rank": 3,
                "abbr": "3RD",
                "name": "Third",
                "member_count": 1,
            },
            {
                "aid": 5005,
                "power": 100,
                "rank": 5,
                "abbr": "FIF",
                "name": "Fifth",
                "member_count": 2,
            },
        ],
        "partial": True,
        "resolution_complete": True,
        "candidate_count": 6,
        "resolved_count": 4,
    }
    assert exhaustive_result == result
    board_request = next(
        request for request in requests if request.url.path == "/v1/leaderboards/kingdom/1"
    )
    assert dict(board_request.url.params) == {
        "kid": "830",
        "limit": "10",
        "resolve": "false",
    }
    assert sum(request.url.path == "/v1/alliances/1001" for request in requests) == 1
    assert {request.url.path for request in requests if request.url.path.startswith("/v1/players/")} == {
        "/v1/players/25",
        "/v1/players/30",
        "/v1/players/55",
        "/v1/players/56",
        "/v1/players/61",
        "/v1/players/62",
        "/v1/players/65",
    }


@pytest.mark.asyncio
async def test_get_ranked_alliances_propagates_leaderboard_failure():
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(401, json={"error": "bad API key"})

    service = KingshotDataService(api_key="secret", base_url="https://ks.example")
    service._client = httpx.AsyncClient(
        base_url="https://ks.example",
        headers=service._build_headers(),
        transport=httpx.MockTransport(handle),
    )

    try:
        result = await service.get_ranked_alliances(830)
    finally:
        await service.close()

    assert result == {
        "success": False,
        "status_code": 401,
        "error_code": "AUTH_ERROR",
        "error_message": "bad API key",
    }
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_get_ranked_alliances_returns_completed_rows_when_one_resolution_blocks():
    class BlockingResolutionService(KingshotDataService):
        _ALLIANCE_RESOLUTION_DEADLINE_SECONDS = 0.01
        _ALLIANCE_BATCH_RETENTION_SECONDS = 0.01

        def __init__(self):
            super().__init__(api_key="secret", base_url="https://ks.example")
            self.board_calls = 0
            self.blocked = asyncio.Event()
            self.blocked_cancelled = asyncio.Event()
            self.release = asyncio.Event()

        async def get_kingdom_board(self, board_type, kid, limit=100, resolve=False):
            assert (board_type, kid, limit, resolve) == (1, 830, 3, False)
            self.board_calls += 1
            return {
                "success": True,
                "status_code": 200,
                "data": {
                    "entries": [
                        {"rank": 3, "uid": 3003, "score": 300},
                        {"rank": 1, "uid": 1001, "score": 500},
                        {"rank": 2, "uid": 2002, "score": 400},
                    ]
                },
            }

        async def _resolve_ranked_alliance(self, candidate, kid):
            if candidate["aid"] == 1001:
                self.blocked.set()
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    self.blocked_cancelled.set()
                    raise
            return self._ranked_alliance(
                candidate,
                f"A{candidate['rank']:02d}",
                "Resolved",
                candidate["rank"],
            )

    service = BlockingResolutionService()

    try:
        result = await asyncio.wait_for(service.get_ranked_alliances(830, limit=3), timeout=0.2)

        assert service.blocked.is_set()
        assert not service.blocked_cancelled.is_set()
        assert result == {
            "success": True,
            "status_code": 200,
            "data": [
                {
                    "aid": 2002,
                    "power": 400,
                    "rank": 2,
                    "abbr": "A02",
                    "name": "Resolved",
                    "member_count": 2,
                },
                {
                    "aid": 3003,
                    "power": 300,
                    "rank": 3,
                    "abbr": "A03",
                    "name": "Resolved",
                    "member_count": 3,
                },
            ],
            "partial": True,
            "resolution_complete": False,
            "candidate_count": 3,
            "resolved_count": 2,
        }

        exhaustive = asyncio.create_task(
            service.get_ranked_alliances(830, limit=3, exhaustive=True)
        )
        await asyncio.sleep(0)
        service.release.set()
        full_result = await asyncio.wait_for(exhaustive, timeout=0.2)
        assert full_result == {
            "success": True,
            "status_code": 200,
            "data": [
                {
                    "aid": 1001,
                    "power": 500,
                    "rank": 1,
                    "abbr": "A01",
                    "name": "Resolved",
                    "member_count": 1,
                },
                *result["data"],
            ],
            "partial": False,
            "resolution_complete": True,
            "candidate_count": 3,
            "resolved_count": 3,
        }
        assert service.board_calls == 1
        assert not service.blocked_cancelled.is_set()
        assert service._ranked_alliance_batches
        await asyncio.sleep(0.03)
        assert service._ranked_alliance_batches == {}
    finally:
        service.release.set()
        await service.close()


@pytest.mark.asyncio
async def test_exhaustive_resolution_finishes_after_per_alliance_lifetime():
    class TimedResolutionService(KingshotDataService):
        _ALLIANCE_RESOLUTION_DEADLINE_SECONDS = 0.005
        _ALLIANCE_RESOLVER_MAX_LIFETIME_SECONDS = 0.02

        def __init__(self):
            super().__init__(api_key="secret", base_url="https://ks.example")
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.release = asyncio.Event()

        async def get_kingdom_board(self, board_type, kid, limit=100, resolve=False):
            return {
                "success": True,
                "status_code": 200,
                "data": {"entries": [{"rank": 1, "uid": 1001, "score": 500}]},
            }

        async def _resolve_ranked_alliance(self, candidate, kid):
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    service = TimedResolutionService()

    try:
        partial = await service.get_ranked_alliances(830, limit=1)
        assert partial["resolution_complete"] is False

        exhaustive = await asyncio.wait_for(
            service.get_ranked_alliances(830, limit=1, exhaustive=True),
            timeout=0.2,
        )
        assert service.started.is_set()
        assert service.cancelled.is_set()
        assert exhaustive == {
            "success": True,
            "status_code": 200,
            "data": [],
            "partial": True,
            "resolution_complete": True,
            "candidate_count": 1,
            "resolved_count": 0,
        }
    finally:
        service.release.set()
        await service.close()


@pytest.mark.asyncio
async def test_close_cancels_and_drains_retained_alliance_resolutions():
    class NeverResolvingService(KingshotDataService):
        _ALLIANCE_RESOLUTION_DEADLINE_SECONDS = 0.01

        def __init__(self):
            super().__init__(api_key="secret", base_url="https://ks.example")
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.release = asyncio.Event()

        async def get_kingdom_board(self, board_type, kid, limit=100, resolve=False):
            return {
                "success": True,
                "status_code": 200,
                "data": {"entries": [{"rank": 1, "uid": 1001, "score": 500}]},
            }

        async def _resolve_ranked_alliance(self, candidate, kid):
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    service = NeverResolvingService()

    result = await service.get_ranked_alliances(830, limit=1)
    assert service.started.is_set()
    assert result["partial"] is True
    assert result["resolution_complete"] is False

    await asyncio.wait_for(service.close(), timeout=0.2)

    assert service.cancelled.is_set()
    assert service._ranked_alliance_batches == {}
