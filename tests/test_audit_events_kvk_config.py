"""Regression tests for the events / KvK / guild configuration / shared UI audit."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy.dialects import postgresql

from handlers.help_handler import HelpHandler
from handlers.kvk_handler import KVKHandler
from services.event_scheduler_service import EventSchedulerService, ScheduledEvent
from services.guild_configuration_service import GuildConfigurationService, GuildSettings
from tests.test_help_ui import interaction as help_interaction
from tests.test_help_ui import make_bot


def test_overlapping_settings_updates_do_not_cache_a_row_missing_the_other_change():
    async def check():
        service = GuildConfigurationService(db_manager=object())
        row = {"use_random_replies": False, "gift_code_channel_id": None}
        reply_read, release_reply = asyncio.Event(), asyncio.Event()

        # Each update reads the row first and writes only its own column, like the ORM
        # does under READ COMMITTED: the slower writer returns its stale read of the rest.
        async def reply_update(operation, *, retry_on_integrity_error=False):
            snapshot = dict(row)
            reply_read.set()
            await release_reply.wait()
            row["use_random_replies"] = True
            snapshot["use_random_replies"] = True
            return GuildSettings(guild_id=5, **snapshot)

        async def channel_update(operation, *, retry_on_integrity_error=False):
            row["gift_code_channel_id"] = 77
            return GuildSettings(guild_id=5, **row)

        service._run = reply_update
        slow = asyncio.create_task(service.update_for_guild(5, use_random_replies=True))
        await reply_read.wait()
        service._run = channel_update
        fast = await service.update_for_guild(5, gift_code_channel_id=77)
        release_reply.set()
        stale = await slow

        assert fast.gift_code_channel_id == 77
        assert stale.gift_code_channel_id is None  # The slower writer's own (stale) view.
        cached = service._cached(5)
        # The cache must not keep "no gift-code channel" for minutes while the DB has 77.
        assert cached is None or cached.gift_code_channel_id == 77

        async def fresh_read(operation, *, retry_on_integrity_error=False):
            return GuildSettings(guild_id=5, **row)

        service._run = fresh_read
        settings = await service.get_or_create_for_guild(5)
        assert settings.use_random_replies is True and settings.gift_code_channel_id == 77

    asyncio.run(check())


def test_sequential_settings_updates_are_still_cached():
    async def check():
        service = GuildConfigurationService(db_manager=object())

        async def write(operation, *, retry_on_integrity_error=False):
            return GuildSettings(guild_id=5, use_random_replies=True)

        service._run = write
        await service.update_for_guild(5, use_random_replies=True)
        assert service._cached(5) == GuildSettings(guild_id=5, use_random_replies=True)

    asyncio.run(check())


def test_dossier_button_cooldowns_forget_users_whose_window_reset(monkeypatch):
    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr("handlers.kvk_handler.time.time", lambda: clock.now)
    handler = KVKHandler(SimpleNamespace(), object(), dossier_service=SimpleNamespace())

    assert handler._dossier_button_retry_after(1) is None
    assert handler._dossier_button_retry_after(1) is None
    assert handler._dossier_button_retry_after(1) > 0  # Two per minute, as before.
    for user_id in range(2, 500):
        handler._dossier_button_retry_after(user_id)
    assert len(handler._dossier_button_cooldowns) == 499

    clock.now += 61
    assert handler._dossier_button_retry_after(1) is None
    assert list(handler._dossier_button_cooldowns) == [1]


def test_reminder_bookkeeping_locks_rows_it_checks_before_writing():
    """READ COMMITTED: the "still active at the claimed time" check must hold until the write.

    Without row locks an edit (or cancel) committed between the check and the write of
    ``record_delivery`` is overwritten with the next occurrence of the old schedule.
    """
    statements = []
    now = datetime(2026, 5, 11, 12, 0, tzinfo=timezone.utc)

    class Session:
        async def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(scalar_one_or_none=lambda: None, scalars=lambda: SimpleNamespace(all=list))

    class Db:
        async def run_in_transaction(self, operation):
            return await operation(Session())

    async def check():
        service = EventSchedulerService(db_manager=Db(), now_provider=lambda: now)
        await service.claim_due_events()
        event = ScheduledEvent(1, now - timedelta(minutes=1), ["everyone"], "Rally", repeat_every_days=1, channel_id=2)
        await service.record_delivery(event, delivered=True)

    asyncio.run(check())
    compiled = [str(statement.compile(dialect=postgresql.dialect())) for statement in statements]
    assert len(compiled) == 2 and all("FOR UPDATE" in sql for sql in compiled), compiled


def test_help_checks_manager_access_once_per_request():
    """Each manager lookup may take up to 2 s on a slow database; two would miss the 3 s deadline."""
    access = SimpleNamespace(can_manage=AsyncMock(return_value=False), is_super_admin=lambda user_id: False)
    handler = HelpHandler(make_bot(local=True), access_policy=access)

    async def check():
        request = help_interaction(2)
        await handler._handle_help(request)
        assert access.can_manage.await_count == 1
        sent = request.response.send_message.call_args.kwargs
        assert sent["ephemeral"] is True
        # Manager commands are left out for a member.
        assert all("Server managers" not in field.name for field in sent["embed"].fields)

    asyncio.run(check())
