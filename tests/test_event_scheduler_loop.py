import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from handlers.event_handler import EventHandler
from services.event_scheduler_service import EventSchedulerService


class Clock:
    def __init__(self, value: datetime):
        self.value = value

    def now(self) -> datetime:
        return self.value


def _http_error(cls, status, text):
    return cls(SimpleNamespace(status=status, reason=text), text)


class FakeChannel:
    def __init__(self, channel_id, guild=None, failures=()):
        self.id = channel_id
        self.guild = guild
        self.failures = list(failures)
        self.sent = []

    async def send(self, content, **kwargs):
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append((content, kwargs))


def _role(role_id, name):
    return SimpleNamespace(id=role_id, name=name, mention=f"<@&{role_id}>", is_default=lambda: False)


def _setup(channels, start=datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)):
    clock = Clock(start)
    service = EventSchedulerService(now_provider=clock.now)
    by_id = {channel.id: channel for channel in channels}
    bot = SimpleNamespace(
        get_channel=lambda channel_id: by_id.get(channel_id),
        fetch_channel=AsyncMock(side_effect=_http_error(discord.NotFound, 404, "Unknown Channel")),
    )
    return EventHandler(service, bot, {7}), service, clock


@pytest.mark.asyncio
async def test_send_failure_in_one_channel_does_not_stop_others_or_lose_state():
    forbidden = FakeChannel(1, failures=[_http_error(discord.Forbidden, 403, "Missing Permissions")])
    healthy = FakeChannel(2)
    handler, service, clock = _setup([forbidden, healthy])
    due = clock.value + timedelta(minutes=1)
    await service.schedule_event(1, due, ["everyone"], "Forbidden channel", repeat_every_days=1)
    await service.schedule_event(2, due, ["everyone"], "Healthy channel")
    await service.schedule_event(3, due, ["everyone"], "Deleted channel", repeat_every_days=1)
    clock.value = due

    await handler.run_scheduler_iteration()

    assert healthy.sent[0][0] == "@everyone\nHealthy channel"
    assert healthy.sent[0][1]["allowed_mentions"].everyone is True
    # Permanent failures are recorded and advanced instead of retrying forever.
    [recurring] = await service.get_events_for_channel(1)
    assert recurring.event_time == due + timedelta(days=1)
    assert recurring.last_error == "Missing permission to send messages in this channel."
    assert await service.get_events_for_channel(3) == []  # Deleted channel: stopped even though it repeats.
    health = handler.scheduler_health()
    assert health.sent_count == 1 and health.failed_count == 2 and health.last_run_at is not None
    assert "Reminder #" in health.last_error
    assert "Sent 1 · Failed 2 · Missed 0" in health.summary()

    clock.value += timedelta(minutes=1)
    await handler.run_scheduler_iteration()
    assert len(healthy.sent) == 1 and forbidden.sent == []


@pytest.mark.asyncio
async def test_transient_send_failure_is_retried_on_the_next_pass_without_duplicates():
    channel = FakeChannel(1, failures=[_http_error(discord.DiscordServerError, 503, "Service Unavailable")])
    handler, service, clock = _setup([channel])
    due = clock.value + timedelta(minutes=1)
    await service.schedule_event(1, due, ["everyone"], "Rally")
    clock.value = due

    await handler.run_scheduler_iteration()
    [pending] = await service.get_events_for_channel(1)
    assert "503" in pending.last_error and channel.sent == []

    clock.value += timedelta(minutes=1)
    await handler.run_scheduler_iteration()
    await handler.run_scheduler_iteration()
    assert len(channel.sent) == 1
    assert await service.get_events_for_channel(1) == []


@pytest.mark.asyncio
async def test_database_failures_are_survived_and_recording_retries_without_reposting():
    channel = FakeChannel(1)
    handler, service, clock = _setup([channel])
    due = clock.value + timedelta(minutes=1)
    await service.schedule_event(1, due, ["everyone"], "Shield up")
    clock.value = due

    real_claim, real_record = service.claim_due_events, service.record_delivery
    service.claim_due_events = AsyncMock(side_effect=RuntimeError("restart transaction (40001)"))
    await handler.run_scheduler_iteration()  # Must not raise.
    assert "40001" in handler.scheduler_health().last_error and channel.sent == []

    service.claim_due_events = real_claim
    service.record_delivery = AsyncMock(side_effect=RuntimeError("connection lost"))
    await handler.run_scheduler_iteration()
    assert len(channel.sent) == 1
    assert len(await service.get_events_for_channel(1)) == 1  # Not lost: still recorded as due.

    service.record_delivery = real_record
    clock.value += timedelta(minutes=1)
    await handler.run_scheduler_iteration()
    assert len(channel.sent) == 1  # Already posted; only the bookkeeping is retried.
    assert await service.get_events_for_channel(1) == []
    assert handler.scheduler_health().sent_count == 1


@pytest.mark.asyncio
async def test_reminders_overdue_after_downtime_are_not_posted_late():
    channel = FakeChannel(1)
    handler, service, clock = _setup([channel])
    await service.schedule_event(1, clock.value + timedelta(minutes=1), ["everyone"], "Missed one-time")
    await service.schedule_event(1, clock.value + timedelta(minutes=1), ["everyone"], "Missed daily", 1)
    clock.value += timedelta(hours=5)

    await handler.run_scheduler_iteration()

    assert channel.sent == []
    [daily] = await service.get_events_for_channel(1)
    assert daily.message == "Missed daily" and daily.event_time > clock.value
    assert handler.scheduler_health().missed_count == 2


@pytest.mark.asyncio
async def test_reminders_ping_stored_roles_and_fall_back_to_everyone():
    guild = SimpleNamespace(roles=[_role(55, "Officers"), _role(66, "R4")])
    channel = FakeChannel(1, guild=guild)
    handler, service, clock = _setup([channel])
    due = clock.value + timedelta(minutes=1)
    await service.schedule_event(1, due, ["officers", "@R4", "Ghost"], "Officer meeting")
    await service.schedule_event(1, due + timedelta(seconds=1), ["everyone"], "Everyone event")
    await service.schedule_event(1, due + timedelta(seconds=2), ["Ghost"], "Deleted role")
    clock.value = due + timedelta(seconds=2)

    await handler.run_scheduler_iteration()

    (roles_content, roles_kwargs), (everyone_content, everyone_kwargs), (ghost_content, ghost_kwargs) = channel.sent
    assert roles_content == "<@&55> <@&66>\nOfficer meeting"
    assert roles_kwargs["allowed_mentions"].roles == guild.roles
    assert roles_kwargs["allowed_mentions"].everyone is False
    assert everyone_content == "@everyone\nEveryone event"
    assert everyone_kwargs["allowed_mentions"].everyone is True
    assert everyone_kwargs["allowed_mentions"].roles is False
    # A deleted role is never escalated to @everyone.
    assert ghost_content == "Deleted role" and ghost_kwargs["allowed_mentions"].everyone is False
    assert handler.scheduler_health().sent_count == 3


@pytest.mark.asyncio
async def test_long_legacy_messages_are_split_and_only_the_first_part_pings():
    channel = FakeChannel(1)
    handler = EventHandler(Mock(), Mock(), {7})
    warning = await handler._send_event_notification(channel, ["everyone"], "x" * 2500)
    assert warning is None
    assert [len(content) for content, _ in channel.sent] == [2000, 510]
    assert channel.sent[1][1]["allowed_mentions"].everyone is False


@pytest.mark.asyncio
async def test_loop_error_handler_restarts_the_scheduler():
    handler = EventHandler(Mock(), Mock(), {7})
    handler.SCHEDULER_RESTART_DELAY_SECONDS = 0.01
    calls = []

    async def iteration():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")

    handler.run_scheduler_iteration = iteration
    handler.start_scheduler_task()
    for _ in range(100):
        if len(calls) >= 2:
            break
        await asyncio.sleep(0.01)
    try:
        assert len(calls) >= 2 and handler.is_scheduler_running()
        health = handler.scheduler_health()
        assert "boom" in health.last_error and health.failed_count == 1
    finally:
        handler.stop_scheduler_task()
        await asyncio.sleep(0)
    handler._restart_scheduler()  # An explicit stop is not undone by a pending restart.
    assert not handler.is_scheduler_running()


def test_failed_continuation_after_ping_counts_as_delivered():
    """A retry after the pinging chunk was posted would ping everyone twice."""
    handler = EventHandler(Mock(), Mock(), {7})
    sent = []

    class Channel:
        guild = None

        async def send(self, content, **kwargs):
            if sent:
                raise discord.HTTPException(SimpleNamespace(status=503, reason="busy"), "busy")
            sent.append(content)

    warning = asyncio.run(handler._send_event_notification(Channel(), ["everyone"], "x" * 2500))

    assert len(sent) == 1 and sent[0].startswith("@everyone")
    assert warning and "Only part of the reminder was posted" in warning
