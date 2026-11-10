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
