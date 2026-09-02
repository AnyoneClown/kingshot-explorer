import pytest

from services.kingshot_api import KingshotAPIClient


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
async def test_century_requests_are_started_at_least_one_second_apart(monkeypatch):
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

    assert sleeps == [1.0]
    assert client._last_request_started_at == 101.0
