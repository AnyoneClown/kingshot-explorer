"""Run the async repositories against an in-memory SQLite database in tests."""

from contextlib import asynccontextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from db.models import (
    Base,
    GiftCode,
    GiftCodeRedemption,
    GuildPlayerRegistration,
    RegisteredPlayer,
    TranslationLog,
    User,
)


class AsyncSessionAdapter:
    """The subset of AsyncSession the repositories use, over a sync Session."""

    def __init__(self, session: Session):
        self._session = session

    async def execute(self, statement):
        return self._session.execute(statement)

    def add(self, instance):
        self._session.add(instance)

    def add_all(self, instances):
        self._session.add_all(instances)

    async def flush(self):
        self._session.flush()

    async def delete(self, instance):
        self._session.delete(instance)


class SqliteDb:
    """DatabaseManager stand-in: ``session()`` and ``run_in_transaction()``."""

    def __init__(self, engine=None):
        self.engine = engine or create_engine("sqlite://")
        self.transactions = []
        self.isolation_levels = []
        Base.metadata.create_all(
            self.engine,
            tables=[
                User.__table__,
                TranslationLog.__table__,
                RegisteredPlayer.__table__,
                GuildPlayerRegistration.__table__,
                GiftCode.__table__,
                GiftCodeRedemption.__table__,
            ],
        )

    @asynccontextmanager
    async def session(self):
        # autoflush=False like the production session factory.
        with Session(self.engine, expire_on_commit=False, autoflush=False) as sync_session, sync_session.begin():
            yield AsyncSessionAdapter(sync_session)

    async def run_in_transaction(self, operation, *, attempts=4, retry_on_integrity_error=False, isolation_level=None):
        self.transactions.append(retry_on_integrity_error)
        self.isolation_levels.append(isolation_level)
        async with self.session() as session:
            return await operation(session)
