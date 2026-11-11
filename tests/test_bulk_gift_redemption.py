import asyncio
from types import SimpleNamespace

from unittest.mock import AsyncMock

import pytest

from handlers.gift_code_handler import GiftCodeHandler


class FakeGiftCodeService:
    def __init__(self):
        self.remote_calls = []

    async def get_redeemed_players(self, session, gift_code):
        del session, gift_code
        return {"111"}

    async def redeem_gift_code_remote(self, player_id, gift_code, kingdom_id=None):
        self.remote_calls.append((player_id, gift_code, kingdom_id))
        return {
            "success": True,
            "message": "OK",
        }


class FakeKingshotDataService:
    def __init__(self):
        self.player_calls = []

    async def get_player_by_fid(self, player_id):
        self.player_calls.append(str(player_id))
        return {
            "success": True,
            "data": {
                "fid": int(player_id),
                "uid": f"uid-{player_id}",
                "name": f"Player {player_id}",
                "kid": 830,
                "stove_lv": 30,
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

    async def track_user(self, **kwargs):
        del kwargs


def make_handler(
    gift_code_service=None,
    tracking_service=None,
    kingshot_data_service=None,
    player_registry_service=None,
    config=None,
):
    return GiftCodeHandler(
        gift_code_service=gift_code_service or FakeGiftCodeService(),
        player_info_service=SimpleNamespace(),
        bot=SimpleNamespace(),
        config=config or SimpleNamespace(admin_user_ids=[]),
        interaction_tracking_service=tracking_service or FakeTrackingService(),
        player_registry_service=player_registry_service,
        kingshot_data_service=kingshot_data_service or FakeKingshotDataService(),
    )


@pytest.mark.asyncio
async def test_auto_redemption_is_not_announced_without_a_success(monkeypatch):
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

    # Announcements are only posted when at least one player got the code.
    assert channel.sent_embeds == []


@pytest.mark.asyncio
async def test_auto_redemption_announces_mixed_results_with_a_success(monkeypatch, gift_cards):
    class FakeChannel:
        def __init__(self):
            self.sent_embeds = []
            self.sent_files = []

        async def send(self, *, embed, files=()):
            self.sent_embeds.append(embed)
            self.sent_files.append(list(files))

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
    # The counts are on the attached card, not in a text field.
    (card,) = gift_cards
    assert card.stats.counts[handler.STATUS_SUCCESS] == 1
    assert card.stats.counts[handler.STATUS_API_REJECTED] == 1
    assert card.title == "Gift code redeemed"
    embed = channel.sent_embeds[0]
    assert embed.image.url == "attachment://gift-code.png"
    assert [field.name for field in embed.fields] == ["Gift Code"]
    assert [file.filename for file in channel.sent_files[0]] == ["gift-code.png"]


@pytest.mark.asyncio
async def test_manual_result_details_take_the_card_off_and_the_summary_puts_it_back(gift_cards):
    channel = SimpleNamespace(id=789, sent=[])

    async def send(**kwargs):
        channel.sent.append(kwargs)
        return SimpleNamespace(id=1)

    channel.send = send
    handler = make_handler()
    results = [
        {"player_id": "1", "status_category": handler.STATUS_SUCCESS, "kingdom": "795"},
        {"player_id": "2", "status_category": handler.STATUS_INVALID_ID, "message": "No such player"},
    ]

    await handler._send_redemption_results_to_channel(
        channel=channel, requester_user_id=123, gift_code="CODE", results=results
    )

    (card,) = gift_cards
    assert card.title == "Redemption finished" and card.stats.counts[handler.STATUS_INVALID_ID] == 1
    view = channel.sent[0]["view"]
    click = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
    await view.details_button.callback(click)
    opened = click.response.edit_message.call_args.kwargs
    assert opened["attachments"] == [] and opened["embed"].title.startswith("Redemption · Failure")
    await view.details_button.callback(click)
    closed = click.response.edit_message.call_args.kwargs
    assert [file.filename for file in closed["attachments"]] == ["gift-code.png"]
    assert closed["embed"].image.url == "attachment://gift-code.png"


@pytest.mark.asyncio
async def test_auto_redemption_summary_stays_text_when_the_card_cannot_be_used(monkeypatch):
    class FakeChannel:
        def __init__(self, can_attach=True):
            self.sent = []
            self.guild = SimpleNamespace(me=object())
            self._can_attach = can_attach

        def permissions_for(self, member):
            return SimpleNamespace(attach_files=self._can_attach)

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    results = [{"status_category": "success"}, {"status_category": "api_rejected"}]

    async def announce(channel):
        handler = make_handler()
        handler._config.auto_redeem_channels = {123}
        handler._bot.get_channel = lambda channel_id: channel if channel_id == 123 else None
        await handler._send_auto_redemption_announcement(gift_code="CODE", total_players=2, results=results)
        return channel.sent[0]

    monkeypatch.setattr("handlers.gift_code_handler.discord.TextChannel", FakeChannel)
    # The bot may not attach files in this channel.
    sent = await announce(FakeChannel(can_attach=False))
    assert "files" not in sent and sent["embed"].image.url is None
    assert "**✅ Success:** 1" in sent["embed"].fields[1].value

    # The card cannot be drawn.
    def broken(stats, **options):
        raise RuntimeError("no font")

    monkeypatch.setattr("services.gift_card_renderer.render_redemption_card", broken)
    sent = await announce(FakeChannel())
    assert "files" not in sent
    assert "**👥 Total players:** 2" in sent["embed"].fields[1].value


@pytest.mark.asyncio
async def test_manual_result_is_posted_as_normal_channel_message():
    class FakeChannel:
        id = 789

        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    channel = FakeChannel()
    handler = make_handler()

    await handler._send_redemption_results_to_channel(
        channel=channel,
        requester_user_id=123,
        gift_code="CODE",
        results=[
            {
                "player_id": "222",
                "player_name": "Player",
                "success": True,
                "message": "OK",
                "status_category": handler.STATUS_SUCCESS,
            }
        ],
    )

    assert len(channel.sent) == 2
    assert channel.sent[0]["embed"].title == "✅ All Gift Codes Redeemed Successfully!"
    assert "content" not in channel.sent[0]
    # The counts are on the attached result card.
    assert channel.sent[0]["embed"].image.url == "attachment://gift-code.png"
    assert [file.filename for file in channel.sent[0]["files"]] == ["gift-code.png"]
    assert "Success" not in channel.sent[0]["embed"].description
    # The ping is a separate new message so Discord notifies the requester.
    assert channel.sent[1]["content"].startswith("<@123>")
    assert [user.id for user in channel.sent[1]["allowed_mentions"].users] == [123]


@pytest.mark.asyncio
async def test_manual_redeem_starts_background_job_and_rejects_duplicate(monkeypatch):
    class FakeResponse:
        def __init__(self):
            self.deferred = False

        async def defer(self, **kwargs):
            self.deferred = True
            self.defer_kwargs = kwargs

        def is_done(self):
            return self.deferred

    class FakeFollowup:
        def __init__(self):
            self.sent = []

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    class FakeChannel:
        id = 789

        async def send(self, **kwargs):
            del kwargs

    class FakeRegistry:
        async def get_guild_redeem_targets(self, guild_id):
            assert guild_id == 456
            return [SimpleNamespace(player_id="222", kingdom="830", enabled=True)]

    def make_interaction():
        return SimpleNamespace(
            id=456,
            created_at=None,
            response=FakeResponse(),
            followup=FakeFollowup(),
            user=SimpleNamespace(
                id=123,
                name="Admin",
                discriminator="0",
                display_name="Admin",
            ),
            guild=SimpleNamespace(id=456, name="Guild"),
            channel=FakeChannel(),
        )

    handler = make_handler(
        player_registry_service=FakeRegistry(),
        config=SimpleNamespace(admin_user_ids=[123]),
    )
    job_started = asyncio.Event()
    release_job = asyncio.Event()
    captured = {}

    async def fake_job(**kwargs):
        captured.update(kwargs)
        job_started.set()
        await release_job.wait()

    monkeypatch.setattr(handler, "_run_manual_redemption_job", fake_job)

    first_interaction = make_interaction()
    await handler._handle_redeem_gift_code_slash(first_interaction, " CODE ")
    await job_started.wait()

    assert first_interaction.response.deferred is True
    assert first_interaction.followup.sent[0]["embed"].title == "🎁 Redemption Job Started"
    assert first_interaction.followup.sent[0]["ephemeral"] is True
    assert captured["gift_code"] == "CODE"
    assert captured["actor_user_id"] == 123

    # One manual job per server at a time, and one job per code.
    second_interaction = make_interaction()
    await handler._handle_redeem_gift_code_slash(second_interaction, "OTHER")
    assert second_interaction.followup.sent[0]["embed"].title == "⏳ Redemption Job Already Running"

    job = handler._jobs.get("code")
    assert job is not None and job.kind == "manual" and captured["job"] is job
    release_job.set()
    await job.task
    await asyncio.sleep(0)
    assert handler._jobs.get("CODE") is None


@pytest.mark.asyncio
async def test_bulk_redemption_prefetches_and_persists_in_batches():
    gift_service = FakeGiftCodeService()
    tracking = FakeTrackingService()
    kingshot = FakeKingshotDataService()
    handler = make_handler(gift_service, tracking, kingshot)

    players = [
        SimpleNamespace(player_id="111", player_name="Already", enabled=True),
        SimpleNamespace(player_id="bad-id", player_name="Bad", enabled=True),
        SimpleNamespace(
            player_id="222",
            player_uid="db-uid-222",
            player_name="Pending",
            kingdom="830",
            castle_level="30",
            enabled=True,
        ),
    ]

    results = await handler._run_bulk_redemption(
        gift_code="CODE",
        registered_players=players,
        actor_user_id=123,
        guild_id=456,
        channel_id=789,
    )

    assert gift_service.remote_calls == [(222, "CODE", "830")]
    assert [result["status_category"] for result in results] == [
        handler.STATUS_ALREADY_REDEEMED,
        handler.STATUS_INVALID_ID,
        handler.STATUS_SUCCESS,
    ]
    assert kingshot.player_calls == []
    assert len(tracking.log_rows) == 1
    assert tracking.log_rows[0]["player_id"] == "222"
    assert tracking.log_rows[0]["gift_code"] == "CODE"
    assert tracking.log_rows[0]["guild_id"] == 456
    assert len(tracking.metadata_rows) == 1
    assert tracking.metadata_rows[0]["player_id"] == "222"
    assert tracking.metadata_rows[0]["player_uid"] == "db-uid-222"


@pytest.mark.asyncio
async def test_global_code_error_stops_after_probe_and_skips_remaining_players():
    class InvalidCodeGiftService(FakeGiftCodeService):
        async def get_redeemed_players(self, session, gift_code):
            del session, gift_code
            return set()

        async def redeem_gift_code_remote(self, player_id, gift_code, kingdom_id=None):
            self.remote_calls.append((player_id, gift_code, kingdom_id))
            return {
                "success": False,
                "message": "Gift code was not found or is incorrect.",
                "error_code": "GIFT_CODE_NOT_FOUND",
                "api_status": "CDK NOT FOUND",
                "global_code_error": True,
            }

    gift_service = InvalidCodeGiftService()
    tracking = FakeTrackingService()
    kingshot = FakeKingshotDataService()
    handler = make_handler(gift_service, tracking, kingshot)
    players = [
        SimpleNamespace(
            player_id=str(player_id),
            player_name=f"Player {player_id}",
            kingdom="830",
            enabled=True,
        )
        for player_id in range(222, 232)
    ]

    results = await handler._run_bulk_redemption(
        gift_code="WRONG-CODE",
        registered_players=players,
        actor_user_id=123,
        guild_id=456,
        channel_id=789,
    )

    assert gift_service.remote_calls == [(222, "WRONG-CODE", "830")]
    assert kingshot.player_calls == []
    assert results[0]["status_category"] == handler.STATUS_API_REJECTED
    assert [result["status_category"] for result in results[1:]] == [handler.STATUS_SKIPPED] * 9
    assert len(tracking.log_rows) == 1
    assert tracking.log_rows[0]["error_code"] == "GIFT_CODE_NOT_FOUND"


@pytest.mark.asyncio
async def test_cached_kingdom_does_not_call_jeab():
    class UnexpectedKingshotDataService:
        async def get_player_by_fid(self, player_id):
            raise AssertionError(f"Jeab should not be called for cached player {player_id}")

    gift_service = FakeGiftCodeService()
    handler = make_handler(
        gift_code_service=gift_service,
        kingshot_data_service=UnexpectedKingshotDataService(),
    )
    player = SimpleNamespace(
        player_id="222",
        player_uid="uid-222",
        player_name="Cached Player",
        kingdom="831",
        castle_level="30",
        enabled=True,
    )

    results = await handler._run_bulk_redemption(
        gift_code="CODE",
        registered_players=[player],
        actor_user_id=123,
        guild_id=456,
        channel_id=789,
    )

    assert gift_service.remote_calls == [(222, "CODE", "831")]
    assert results[0]["status_category"] == handler.STATUS_SUCCESS


@pytest.mark.asyncio
async def test_missing_cached_kingdom_is_resolved_through_jeab():
    gift_service = FakeGiftCodeService()
    kingshot = FakeKingshotDataService()
    handler = make_handler(gift_code_service=gift_service, kingshot_data_service=kingshot)
    player = SimpleNamespace(player_id="222", player_name="Player", kingdom=None, enabled=True)

    results = await handler._run_bulk_redemption(
        gift_code="CODE",
        registered_players=[player],
        actor_user_id=123,
        guild_id=456,
        channel_id=789,
    )

    assert kingshot.player_calls == ["222"]
    assert gift_service.remote_calls == [(222, "CODE", "830")]
    assert results[0]["status_category"] == handler.STATUS_SUCCESS


@pytest.mark.asyncio
async def test_cached_kingdom_mismatch_refreshes_jeab_and_retries_once():
    class TransferredPlayerGiftCodeService(FakeGiftCodeService):
        async def get_redeemed_players(self, session, gift_code):
            del session, gift_code
            return set()

        async def redeem_gift_code_remote(self, player_id, gift_code, kingdom_id=None):
            self.remote_calls.append((player_id, gift_code, kingdom_id))
            if kingdom_id == "829":
                return {
                    "success": False,
                    "message": "The kingdom does not match this player.",
                    "error_code": "KINGDOM_MISMATCH",
                    "api_status": "USER INFO ERROR",
                    "error_details": {"err_code": "40020"},
                }
            return {"success": True, "message": "OK"}

    gift_service = TransferredPlayerGiftCodeService()
    tracking = FakeTrackingService()
    kingshot = FakeKingshotDataService()
    handler = make_handler(gift_service, tracking, kingshot)
    player = SimpleNamespace(
        player_id="222",
        player_uid="old-uid",
        player_name="Cached Player",
        kingdom="829",
        castle_level="29",
        enabled=True,
    )

    results = await handler._run_bulk_redemption(
        gift_code="CODE",
        registered_players=[player],
        actor_user_id=123,
        guild_id=456,
        channel_id=789,
    )

    assert gift_service.remote_calls == [(222, "CODE", "829"), (222, "CODE", "830")]
    assert kingshot.player_calls == ["222"]
    assert results[0]["status_category"] == handler.STATUS_SUCCESS
    assert results[0]["attempts"] == 2
    assert tracking.metadata_rows[0]["kingdom"] == "830"
    assert tracking.metadata_rows[0]["player_uid"] == "uid-222"


@pytest.mark.asyncio
async def test_cached_kingdom_mismatch_does_not_retry_when_jeab_returns_same_kingdom():
    class MismatchGiftCodeService(FakeGiftCodeService):
        async def get_redeemed_players(self, session, gift_code):
            del session, gift_code
            return set()

        async def redeem_gift_code_remote(self, player_id, gift_code, kingdom_id=None):
            self.remote_calls.append((player_id, gift_code, kingdom_id))
            return {
                "success": False,
                "message": "The kingdom does not match this player.",
                "error_code": "KINGDOM_MISMATCH",
                "api_status": "USER INFO ERROR",
            }

    gift_service = MismatchGiftCodeService()
    kingshot = FakeKingshotDataService()
    handler = make_handler(gift_code_service=gift_service, kingshot_data_service=kingshot)
    player = SimpleNamespace(player_id="222", player_name="Player", kingdom="830", enabled=True)

    results = await handler._run_bulk_redemption(
        gift_code="CODE",
        registered_players=[player],
        actor_user_id=123,
        guild_id=456,
        channel_id=789,
    )

    assert gift_service.remote_calls == [(222, "CODE", "830")]
    assert kingshot.player_calls == ["222"]
    assert results[0]["status_category"] == handler.STATUS_INVALID_ID


@pytest.mark.asyncio
async def test_transient_redemption_failures_retry(monkeypatch):
    class RetryGiftCodeService(FakeGiftCodeService):
        async def redeem_gift_code_remote(self, player_id, gift_code, kingdom_id=None):
            self.remote_calls.append((player_id, gift_code, kingdom_id))
            if len(self.remote_calls) == 1:
                return {
                    "success": False,
                    "message": "HTTP Error 503",
                    "error_code": "503",
                }
            return {"success": True, "message": "OK"}

    async def no_sleep(delay):
        del delay

    monkeypatch.setattr("services.gift_redemption_service.asyncio.sleep", no_sleep)
    monkeypatch.setattr("services.gift_redemption_service.random.uniform", lambda _start, _end: 0)

    gift_service = RetryGiftCodeService()
    handler = make_handler(gift_service)

    result = await handler._redeem_with_retries(
        player_id_int=222,
        kingdom_id="830",
        gift_code="CODE",
        player_id_for_logs="222",
    )

    assert result["success"] is True
    assert result["attempts"] == 2
    assert result["retries"] == 1
    assert gift_service.remote_calls == [(222, "CODE", "830"), (222, "CODE", "830")]


@pytest.mark.asyncio
async def test_rate_limit_retries_use_bounded_budget(monkeypatch):
    class RateLimitedGiftCodeService(FakeGiftCodeService):
        async def redeem_gift_code_remote(self, player_id, gift_code, kingdom_id=None):
            self.remote_calls.append((player_id, gift_code, kingdom_id))
            return {
                "success": False,
                "message": "The player is temporarily rate limited.",
                "error_code": "RATE_LIMITED",
            }

    async def no_sleep(delay):
        del delay

    monkeypatch.setattr("services.gift_redemption_service.asyncio.sleep", no_sleep)
    monkeypatch.setattr("services.gift_redemption_service.random.uniform", lambda _start, _end: 0)

    gift_service = RateLimitedGiftCodeService()
    handler = make_handler(gift_service)

    result = await handler._redeem_with_retries(
        player_id_int=222,
        kingdom_id="830",
        gift_code="CODE",
        player_id_for_logs="222",
    )

    assert result["success"] is False
    assert result["attempts"] == 4
    assert result["retries"] == 3
    assert gift_service.remote_calls == [(222, "CODE", "830")] * 4


@pytest.mark.asyncio
async def test_non_rate_limit_transient_failures_keep_standard_budget(monkeypatch):
    class FailingGiftCodeService(FakeGiftCodeService):
        async def redeem_gift_code_remote(self, player_id, gift_code, kingdom_id=None):
            self.remote_calls.append((player_id, gift_code, kingdom_id))
            return {
                "success": False,
                "message": "HTTP Error 503",
                "error_code": "503",
            }

    async def no_sleep(delay):
        del delay

    monkeypatch.setattr("services.gift_redemption_service.asyncio.sleep", no_sleep)
    monkeypatch.setattr("services.gift_redemption_service.random.uniform", lambda _start, _end: 0)

    gift_service = FailingGiftCodeService()
    handler = make_handler(gift_service)

    result = await handler._redeem_with_retries(
        player_id_int=222,
        kingdom_id="830",
        gift_code="CODE",
        player_id_for_logs="222",
    )

    assert result["success"] is False
    assert result["attempts"] == 3
    assert result["retries"] == 2
    assert gift_service.remote_calls == [(222, "CODE", "830")] * 3
