"""DatabaseManager.run_in_transaction isolation handling."""

from contextlib import asynccontextmanager

import pytest

from db.session import DatabaseManager


class RecordingSession:
    def __init__(self, events):
        self.events = events

    async def execute(self, statement):
        self.events.append(str(statement))


def manager_with(events):
    manager = DatabaseManager("postgresql+asyncpg://user:pass@localhost/db")

    @asynccontextmanager
    async def session():
        yield RecordingSession(events)

    manager.session = session
    return manager


@pytest.mark.asyncio
async def test_isolation_level_is_set_before_the_first_statement():
    events = []
    manager = manager_with(events)

    async def operation(session):
        await session.execute("select 1")
        return "done"

    assert await manager.run_in_transaction(operation, isolation_level="SERIALIZABLE") == "done"
    assert events == ["SET TRANSACTION ISOLATION LEVEL SERIALIZABLE", "select 1"]


@pytest.mark.asyncio
async def test_default_transactions_keep_the_driver_isolation_level():
    events = []
    manager = manager_with(events)

    async def operation(session):
        await session.execute("select 1")

    await manager.run_in_transaction(operation)
    assert events == ["select 1"]


@pytest.mark.asyncio
async def test_unknown_isolation_level_is_rejected_before_connecting():
    events = []
    manager = manager_with(events)

    async def operation(session):
        raise AssertionError("must not run")

    with pytest.raises(ValueError):
        await manager.run_in_transaction(operation, isolation_level="SERIALIZABLE; DROP TABLE users")
    assert events == []


class _ConnectCaptured(Exception):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    ["cockroachdb+asyncpg://user:pass@localhost:26257/db", "postgresql+asyncpg://user:pass@localhost/db"],
)
async def test_asyncpg_connections_get_connect_and_statement_timeouts(monkeypatch, url):
    import asyncpg

    from db.session import DB_COMMAND_TIMEOUT_SECONDS, DB_CONNECT_TIMEOUT_SECONDS

    captured = {}

    async def fake_connect(*args, **kwargs):
        captured.update(kwargs)
        raise _ConnectCaptured()

    monkeypatch.setattr(asyncpg, "connect", fake_connect)
    manager = DatabaseManager(url)
    try:
        with pytest.raises(_ConnectCaptured):
            async with manager.engine.connect():
                pass
    finally:
        await manager.engine.dispose()

    # A half-open connection must fail the statement, not stall its caller for ~15 minutes.
    assert captured["timeout"] == DB_CONNECT_TIMEOUT_SECONDS
    assert captured["command_timeout"] == DB_COMMAND_TIMEOUT_SECONDS
