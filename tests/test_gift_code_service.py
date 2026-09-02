import pytest

from services.gift_code_service import GiftCodeService


class FakeKingshotAPIClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def redeem_code(self, player_id, kingdom_id, gift_code):
        self.calls.append((player_id, kingdom_id, gift_code))
        return self.response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            {"code": 1, "data": [], "msg": "SAME TYPE EXCHANGE.", "err_code": 40011},
            {"success": True, "error_code": None, "global_code_error": None},
        ),
        (
            {"code": 1, "data": [], "msg": "RECEIVED.", "err_code": 40008},
            {
                "success": False,
                "error_code": "ALREADY_REDEEMED_BY_API",
                "global_code_error": None,
            },
        ),
        (
            {"code": 1, "data": [], "msg": "TIME ERROR.", "err_code": 40007},
            {
                "success": False,
                "error_code": "GIFT_CODE_EXPIRED",
                "global_code_error": True,
            },
        ),
        (
            {"code": 1, "data": [], "msg": "CDK NOT FOUND.", "err_code": 40014},
            {
                "success": False,
                "error_code": "GIFT_CODE_NOT_FOUND",
                "global_code_error": True,
            },
        ),
        (
            {"code": 1, "data": [], "msg": "USER INFO ERROR.", "err_code": 40020},
            {
                "success": False,
                "error_code": "KINGDOM_MISMATCH",
                "global_code_error": False,
            },
        ),
        (
            {"code": 1, "data": [], "msg": "TOO FREQUENT.", "err_code": 40019},
            {
                "success": False,
                "error_code": "RATE_LIMITED",
                "global_code_error": False,
            },
        ),
    ],
)
async def test_new_api_statuses_are_classified(response, expected):
    api_client = FakeKingshotAPIClient(response)
    service = GiftCodeService()
    service._client = api_client

    result = await service.redeem_gift_code_remote(
        player_id=122212334,
        gift_code="CHILLWEEKEND",
        kingdom_id=830,
    )

    assert result["success"] is expected["success"]
    assert result.get("error_code") == expected["error_code"]
    assert result.get("global_code_error") == expected["global_code_error"]
    assert api_client.calls == [("122212334", "830", "CHILLWEEKEND")]


@pytest.mark.asyncio
async def test_redemption_requires_kingdom_without_calling_api():
    api_client = FakeKingshotAPIClient({"code": 0, "msg": "SUCCESS"})
    service = GiftCodeService()
    service._client = api_client

    result = await service.redeem_gift_code_remote(
        player_id=122212334,
        gift_code="CHILLWEEKEND",
    )

    assert result["error_code"] == "MISSING_KINGDOM"
    assert api_client.calls == []
