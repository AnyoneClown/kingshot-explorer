from __future__ import annotations

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
