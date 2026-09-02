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
