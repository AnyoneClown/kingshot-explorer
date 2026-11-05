import pytest

from services.kingshot_api import (
    MAX_REQUESTS_PER_MINUTE,
    MIN_REQUEST_INTERVAL_SECONDS,
    KingshotAPIClient,
)


@pytest.mark.asyncio
async def test_redeem_code_uses_kingdom_and_unix_seconds(monkeypatch):
    client = KingshotAPIClient()
    captured = {}

    async def fake_request(path, params):
        captured["path"] = path
        captured["params"] = params
        return {"code": 0, "msg": "SUCCESS"}

    monkeypatch.setattr("services.kingshot_api.time.time", lambda: 1788364201.987)
    monkeypatch.setattr(client, "_request", fake_request)

    result = await client.redeem_code("122212334", "830", "CHILLWEEKEND")

    assert result == {"code": 0, "msg": "SUCCESS"}
    assert captured == {
        "path": "/gift_code",
        "params": {
            "fid": "122212334",
            "kid": "830",
            "cdk": "CHILLWEEKEND",
            "time": "1788364201",
        },
    }


@pytest.mark.asyncio
async def test_century_requests_are_paced_to_advertised_minute_limit(monkeypatch):
    client = KingshotAPIClient()
    clock = [100.0]
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)
        clock[0] += delay

    monkeypatch.setattr("services.kingshot_api.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("services.kingshot_api.asyncio.sleep", fake_sleep)

    await client._wait_for_request_slot()
    await client._wait_for_request_slot()

    assert MAX_REQUESTS_PER_MINUTE == 30
    assert MIN_REQUEST_INTERVAL_SECONDS == 2.0
    assert sleeps == [2.0]
    assert client._last_request_started_at == 102.0


class _FakeResponse:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    async def text(self):
        return "<html>"


class _FakeSession:
    def __init__(self, outcome):
        self.outcome = outcome
        self.posts = 0

    def post(self, url, **kwargs):
        self.posts += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (_FakeResponse(429), {"err_code": 429}),
        (_FakeResponse(503), {"err_code": 503, "msg": "HTTP Error 503"}),
        (_FakeResponse(200, {"code": 0, "msg": "SUCCESS"}), {"code": 0}),
    ],
)
async def test_request_makes_exactly_one_attempt(monkeypatch, outcome, expected):
    """Retries live in GiftRedemptionService only; the client never retries."""
    client = KingshotAPIClient()
    session = _FakeSession(outcome)
    client._session = session

    async def no_wait():
        return None

    monkeypatch.setattr(client, "_wait_for_request_slot", no_wait)
    result = await client._request("/gift_code", {"fid": "1"})

    assert session.posts == 1
    assert expected.items() <= result.items()


@pytest.mark.asyncio
async def test_transport_errors_surface_after_one_attempt(monkeypatch):
    import aiohttp

    client = KingshotAPIClient()
    session = _FakeSession(aiohttp.ClientConnectionError("reset"))
    client._session = session

    async def no_wait():
        return None

    monkeypatch.setattr(client, "_wait_for_request_slot", no_wait)
    with pytest.raises(ValueError, match="HTTP request failed"):
        await client._request("/gift_code", {"fid": "1"})
    assert session.posts == 1
