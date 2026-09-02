from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

from config.bot_config import BotConfig
from handlers.status_handler import StatusHandler
from main import TranslatorBot
from services.database_health_service import DatabaseHealthService


class FakeResponse:
    def __init__(self):
        self.deferred = False

    async def defer(self, **kwargs):
        self.deferred = True
        self.defer_kwargs = kwargs


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, **kwargs):
        self.messages.append(kwargs)


class FakeBot:
    latency = 0.042
    guilds = [object(), object()]

    def is_closed(self):
        return False


class FakeDatabaseHealth:
    def __init__(self, result=(True, "Reachable")):
        self.result = result

    async def check_database(self):
        return self.result


class FakeDataHealth:
    def __init__(self, result):
        self.result = result

    async def get_health(self):
        return self.result


class FakeWorker:
    def __init__(self, running: bool):
        self.running = running

    def is_scheduler_running(self):
        return self.running

    def is_polling_running(self):
        return self.running


def make_config(*, profile="local"):
    return BotConfig(
        discord_token="token",
        database_url="postgresql+asyncpg://user:pass@localhost/test",
        nvidia_api_key="key" if profile == "local" else None,
        bot_profile=profile,
    )


def field_values(embed: discord.Embed) -> dict[str, str]:
    return {field.name: field.value for field in embed.fields}


async def render_status(handler: StatusHandler):
    interaction = SimpleNamespace(response=FakeResponse(), followup=FakeFollowup())
    await handler._handle_status(interaction)
    assert interaction.response.deferred is True
    assert interaction.response.defer_kwargs == {"thinking": True, "ephemeral": True}
    assert interaction.followup.messages[0]["ephemeral"] is True
    return interaction.followup.messages[0]["embed"]


@pytest.mark.asyncio
async def test_global_status_reports_local_workers_as_disabled():
    handler = StatusHandler(
        FakeBot(),
        make_config(profile="global"),
        FakeDatabaseHealth(),
        kingshot_data_service=FakeDataHealth(
            {"success": True, "data": {"status": "ok", "connected": True}}
        ),
        started_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    embed = await render_status(handler)
    fields = field_values(embed)

    assert fields["KingShot Data API"] == "OK - Reachable"
    assert fields["Scheduler"] == "INFO - Disabled"
    assert fields["Gift Polling"] == "INFO - Disabled"
    assert "AI Chat Model" not in fields
    assert embed.color == discord.Color.green()


@pytest.mark.asyncio
async def test_local_status_distinguishes_disabled_and_stopped_workers():
    handler = StatusHandler(
        FakeBot(),
        make_config(),
        FakeDatabaseHealth(),
        event_handler=FakeWorker(running=False),
        gift_code_handler=FakeWorker(running=False),
        kingshot_data_service=FakeDataHealth(
            {"success": True, "data": {"status": "ok", "connected": True}}
        ),
        started_at=datetime.now(timezone.utc),
        gift_polling_enabled=False,
    )

    embed = await render_status(handler)
    fields = field_values(embed)

    assert fields["Scheduler"] == "ISSUE - Stopped"
    assert fields["Gift Polling"] == "INFO - Disabled"
    assert fields["AI Chat Model"] == "`nvidia/nemotron-3-ultra-550b-a55b`"
    assert embed.color == discord.Color.orange()


@pytest.mark.asyncio
async def test_enabled_gift_polling_and_data_gateway_failures_affect_health():
    handler = StatusHandler(
        FakeBot(),
        make_config(),
        FakeDatabaseHealth(result=(False, "password=do-not-leak")),
        event_handler=FakeWorker(running=True),
        gift_code_handler=FakeWorker(running=False),
        kingshot_data_service=FakeDataHealth(
            {"success": True, "data": {"status": "ok", "connected": False}}
        ),
        started_at=datetime.now(timezone.utc),
    )

    embed = await render_status(handler)
    fields = field_values(embed)

    assert fields["Database"] == "ISSUE - Unavailable"
    assert "do-not-leak" not in str(embed.to_dict())
    assert fields["KingShot Data API"] == "ISSUE - Degraded"
    assert fields["Scheduler"] == "OK - Running"
    assert fields["Gift Polling"] == "ISSUE - Stopped"
    assert embed.color == discord.Color.orange()


@pytest.mark.asyncio
async def test_database_and_data_api_checks_run_concurrently():
    arrivals: set[str] = set()
    ready = asyncio.Event()

    class CoordinatedDatabaseHealth:
        async def check_database(self):
            arrivals.add("database")
            if len(arrivals) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=0.5)
            return True, "Reachable"

    class CoordinatedDataHealth:
        async def get_health(self):
            arrivals.add("data")
            if len(arrivals) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=0.5)
            return {"success": True, "data": {"status": "ok", "connected": True}}

    handler = StatusHandler(
        FakeBot(),
        make_config(profile="global"),
        CoordinatedDatabaseHealth(),
        kingshot_data_service=CoordinatedDataHealth(),
        started_at=datetime.now(timezone.utc),
    )

    await render_status(handler)

    assert arrivals == {"database", "data"}


@pytest.mark.asyncio
async def test_database_health_service_sanitizes_connection_errors():
    class BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("password=do-not-leak")

        async def __aexit__(self, exc_type, exc_value, traceback):
            return False

    class BrokenDatabase:
        def session(self):
            return BrokenSession()

    service = DatabaseHealthService(BrokenDatabase())

    assert await service.check_database() == (False, "Unavailable")


def test_global_profile_registers_status_command():
    app = TranslatorBot(make_config(profile="global"))

    assert app.status_handler is not None
    assert app.bot.tree.get_command("status") is not None
