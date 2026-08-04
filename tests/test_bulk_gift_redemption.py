from types import SimpleNamespace

import pytest

from handlers.gift_code_handler import GiftCodeHandler


class FakeGiftCodeService:
    def __init__(self):
        self.remote_calls = []

    async def get_redeemed_players(self, session, gift_code):
        del session, gift_code
        return {"111"}

    async def redeem_gift_code_remote(self, player_id, gift_code):
        self.remote_calls.append((player_id, gift_code))
        return {
            "success": True,
            "message": "OK",
            "player_profile": {
                "playerId": str(player_id),
                "playerUid": f"uid-{player_id}",
                "name": f"Player {player_id}",
                "kingdom": 830,
                "level": 30,
            },
        }


class FakeTrackingService:
    def __init__(self):
        self.metadata_rows = []
        self.log_rows = []

    async def sync_player_metadata_many(self, rows):
        self.metadata_rows.extend(rows)
        return len(rows)

    async def log_gift_code_redemptions_many(self, rows):
        self.log_rows.extend(rows)
        return rows


def make_handler(gift_code_service=None, tracking_service=None):
    return GiftCodeHandler(
        gift_code_service=gift_code_service or FakeGiftCodeService(),
        player_info_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        config=SimpleNamespace(admin_user_ids=[]),
        interaction_tracking_service=tracking_service or FakeTrackingService(),
    )


@pytest.mark.asyncio
async def test_auto_redemption_does_not_announce_when_every_result_failed(monkeypatch):
    class FakeChannel:
        def __init__(self):
            self.sent_embeds = []

        async def send(self, *, embed):
            self.sent_embeds.append(embed)

    channel = FakeChannel()
    handler = make_handler()
    handler._config.auto_redeem_channels = {123}
    handler._bot.get_channel = lambda channel_id: channel if channel_id == 123 else None
    monkeypatch.setattr("handlers.gift_code_handler.discord.TextChannel", FakeChannel)

    await handler._send_auto_redemption_announcement(
        gift_code="CODE",
        total_players=3,
        results=[
            {"status_category": handler.STATUS_API_REJECTED},
            {"status_category": handler.STATUS_INVALID_ID},
            {"status_category": handler.STATUS_ALREADY_REDEEMED},
        ],
    )

    assert channel.sent_embeds == []


@pytest.mark.asyncio
async def test_auto_redemption_announces_mixed_results_with_a_success(monkeypatch):
    class FakeChannel:
        def __init__(self):
            self.sent_embeds = []

        async def send(self, *, embed):
            self.sent_embeds.append(embed)

    channel = FakeChannel()
    handler = make_handler()
    handler._config.auto_redeem_channels = {123}
    handler._bot.get_channel = lambda channel_id: channel if channel_id == 123 else None
    monkeypatch.setattr("handlers.gift_code_handler.discord.TextChannel", FakeChannel)

    await handler._send_auto_redemption_announcement(
        gift_code="CODE",
        total_players=2,
        results=[
            {"status_category": handler.STATUS_SUCCESS},
            {"status_category": handler.STATUS_API_REJECTED},
        ],
    )

    assert len(channel.sent_embeds) == 1
    assert "✅ **Success**: 1" in channel.sent_embeds[0].fields[1].value


@pytest.mark.asyncio
async def test_bulk_redemption_prefetches_and_persists_in_batches():
    gift_service = FakeGiftCodeService()
    tracking = FakeTrackingService()
    handler = make_handler(gift_service, tracking)

    players = [
        SimpleNamespace(player_id="111", player_name="Already", enabled=True),
        SimpleNamespace(player_id="bad-id", player_name="Bad", enabled=True),
        SimpleNamespace(player_id="222", player_name="Pending", enabled=True),
    ]

    results = await handler._run_bulk_redemption(
        gift_code="CODE",
        registered_players=players,
        actor_user_id=123,
        guild_id=456,
        channel_id=789,
    )

    assert gift_service.remote_calls == [(222, "CODE")]
    assert [result["status_category"] for result in results] == [
        handler.STATUS_ALREADY_REDEEMED,
        handler.STATUS_INVALID_ID,
        handler.STATUS_SUCCESS,
    ]
    assert len(tracking.log_rows) == 1
    assert tracking.log_rows[0]["player_id"] == "222"
    assert tracking.log_rows[0]["gift_code"] == "CODE"
    assert tracking.log_rows[0]["guild_id"] == 456
    assert len(tracking.metadata_rows) == 1
    assert tracking.metadata_rows[0]["player_id"] == "222"
    assert tracking.metadata_rows[0]["player_uid"] == "uid-222"


@pytest.mark.asyncio
async def test_transient_redemption_failures_retry(monkeypatch):
    class RetryGiftCodeService(FakeGiftCodeService):
        async def redeem_gift_code_remote(self, player_id, gift_code):
            self.remote_calls.append((player_id, gift_code))
            if len(self.remote_calls) == 1:
                return {
                    "success": False,
                    "message": "HTTP Error 503",
                    "error_code": "503",
                }
            return {"success": True, "message": "OK"}

    async def no_sleep(delay):
        del delay

    monkeypatch.setattr("handlers.gift_code_handler.asyncio.sleep", no_sleep)
    monkeypatch.setattr("handlers.gift_code_handler.random.uniform", lambda _start, _end: 0)

    gift_service = RetryGiftCodeService()
    handler = make_handler(gift_service)

    result = await handler._redeem_with_retries(
        player_id_int=222,
        gift_code="CODE",
        player_id_for_logs="222",
    )

    assert result["success"] is True
    assert result["attempts"] == 2
    assert result["retries"] == 1
    assert gift_service.remote_calls == [(222, "CODE"), (222, "CODE")]


@pytest.mark.asyncio
async def test_rate_limit_retries_until_cleared(monkeypatch):
    class RateLimitedGiftCodeService(FakeGiftCodeService):
        async def redeem_gift_code_remote(self, player_id, gift_code):
            self.remote_calls.append((player_id, gift_code))
            if len(self.remote_calls) <= 20:
                return {
                    "success": False,
                    "message": "API rate limit exceeded (429 Too Many Requests).",
                    "error_code": "429",
                }
            return {"success": True, "message": "OK"}

    async def no_sleep(delay):
        del delay

    monkeypatch.setattr("handlers.gift_code_handler.asyncio.sleep", no_sleep)
    monkeypatch.setattr("handlers.gift_code_handler.random.uniform", lambda _start, _end: 0)

    gift_service = RateLimitedGiftCodeService()
    handler = make_handler(gift_service)

    result = await handler._redeem_with_retries(
        player_id_int=222,
        gift_code="CODE",
        player_id_for_logs="222",
    )

    assert result["success"] is True
    assert result["attempts"] == 21
    assert result["retries"] == 20
    assert gift_service.remote_calls == [(222, "CODE")] * 21


@pytest.mark.asyncio
async def test_non_rate_limit_transient_failures_keep_standard_budget(monkeypatch):
    class FailingGiftCodeService(FakeGiftCodeService):
        async def redeem_gift_code_remote(self, player_id, gift_code):
            self.remote_calls.append((player_id, gift_code))
            return {
                "success": False,
                "message": "HTTP Error 503",
                "error_code": "503",
            }

    async def no_sleep(delay):
        del delay

    monkeypatch.setattr("handlers.gift_code_handler.asyncio.sleep", no_sleep)
    monkeypatch.setattr("handlers.gift_code_handler.random.uniform", lambda _start, _end: 0)

    gift_service = FailingGiftCodeService()
    handler = make_handler(gift_service)

    result = await handler._redeem_with_retries(
        player_id_int=222,
        gift_code="CODE",
        player_id_for_logs="222",
    )

    assert result["success"] is False
    assert result["attempts"] == 3
    assert result["retries"] == 2
    assert gift_service.remote_calls == [(222, "CODE")] * 3
