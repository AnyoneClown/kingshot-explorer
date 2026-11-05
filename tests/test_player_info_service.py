import asyncio

import pytest

from services.player_info_service import PlayerInfoService, PlayerLookupUnavailable


class FakeDataService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def get_player_by_fid(self, fid):
        self.calls.append(fid)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_get_player_info_uses_data_api_by_fid_and_normalizes_profile():
    data = FakeDataService(
        {
            "success": True,
            "status_code": 200,
            "data": {
                "uid": 85826384,
                "fid": 277720981,
                "name": "Moonberry",
                "kid": 7,
                "stove_lv": 30,
                "power": 940162,
                "alliance": {"aid": 1, "abbr": "AQM", "name": "Aquarium"},
            },
        }
    )

    player = asyncio.run(PlayerInfoService(data).get_player_info(" 277720981 "))

    assert data.calls == ["277720981"]
    assert player == {
        "name": "Moonberry",
        "playerId": "277720981",
        "playerUid": "85826384",
        "level": 30,
        "kingdom": 7,
        "power": 940162,
        "allianceTag": "AQM",
        "profilePhoto": None,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"fid": 1, "error": "fid not found"},
        {"uid": None, "name": None, "kid": None},
    ],
)
def test_get_player_info_returns_none_for_unknown_players(payload):
    data = FakeDataService({"success": True, "status_code": 200, "data": payload})

    assert asyncio.run(PlayerInfoService(data).get_player_info("1")) is None


def test_get_player_info_rejects_non_numeric_ids_without_calling_api():
    data = FakeDataService({"success": True, "data": {}})

    assert asyncio.run(PlayerInfoService(data).get_player_info("abc")) is None
    assert data.calls == []


@pytest.mark.parametrize(
    "result",
    [
        {"success": False, "status_code": None, "error_code": "NETWORK_ERROR", "error_message": "down"},
        {"success": False, "status_code": 401, "error_code": "AUTH_ERROR", "error_message": "bad key"},
        RuntimeError("boom"),
    ],
)
def test_get_player_info_raises_when_lookup_is_unavailable(result):
    with pytest.raises(PlayerLookupUnavailable):
        asyncio.run(PlayerInfoService(FakeDataService(result)).get_player_info("123"))


def test_get_player_info_without_data_service_is_unavailable():
    with pytest.raises(PlayerLookupUnavailable):
        asyncio.run(PlayerInfoService().get_player_info("123"))
