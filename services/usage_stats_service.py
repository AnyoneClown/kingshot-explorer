"""Per-day command usage counters.

Counts are buffered in memory and written in batches, so running a command never
waits for the database. Only the command name, the server and the UTC day are
stored — no user data.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from db.session import DatabaseManager, get_db
from repositories.discord_repositories import CommandUsageRepository
from services.transactions import run_db_operation

logger = logging.getLogger(__name__)

# Guild ID stored for commands used in direct messages.
DM_GUILD_ID = 0
MAX_COMMAND_NAME_LENGTH = 100
# Entries kept while the database is unreachable, so a long outage cannot grow memory.
MAX_BUFFERED_KEYS = 5000


class UsageStatsService:
    def __init__(self, db_manager: DatabaseManager | None = None, retention_days: int = 180):
        self._db_manager = db_manager
        self._retention_days = retention_days
        self._buffer: Counter[tuple[date, int, str]] = Counter()

    async def _get_db_manager(self):
        return self._db_manager or get_db()

    def record(self, command_name: str, guild_id: int | None, *, when: datetime | None = None) -> None:
        """Count one use; never raises and never touches the database."""
        name = str(command_name or "").strip()[:MAX_COMMAND_NAME_LENGTH]
        if not name:
            return
        day = (when or datetime.now(timezone.utc)).astimezone(timezone.utc).date()
        key = (day, int(guild_id) if guild_id else DM_GUILD_ID, name)
        if key not in self._buffer and len(self._buffer) >= MAX_BUFFERED_KEYS:
            return
        self._buffer[key] += 1

    @property
    def buffered(self) -> int:
        return sum(self._buffer.values())

    async def flush(self) -> int:
        """Write buffered counts; on failure they stay buffered for the next flush."""
        if not self._buffer:
            return 0
        pending, self._buffer = self._buffer, Counter()

        async def operation(session) -> None:
            repo = CommandUsageRepository(session)
            for (day, guild_id, name), uses in pending.items():
                await repo.add(day, guild_id, name, uses)

        try:
            await run_db_operation(await self._get_db_manager(), operation, retry_on_integrity_error=True)
        except Exception:
            self._buffer.update(pending)
            logger.warning("Could not save command usage counters; keeping them for the next flush", exc_info=True)
            return 0
        return sum(pending.values())

    async def top_commands(
        self, days: int = 30, *, guild_id: int | None = None, limit: int = 10
    ) -> list[tuple[str, int]]:
        since = datetime.now(timezone.utc).date() - timedelta(days=max(1, days) - 1)

        async def operation(session):
            return await CommandUsageRepository(session).totals_since(since, guild_id=guild_id)

        db = await self._get_db_manager()
        async with db.session() as session:
            totals = Counter(dict(await operation(session)))
        for (day, buffered_guild, name), uses in self._buffer.items():
            if day >= since and (guild_id is None or buffered_guild == guild_id):
                totals[name] += uses
        return sorted(totals.items(), key=lambda item: (-item[1], item[0]))[:limit]

    async def purge_old(self) -> int:
        if self._retention_days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=self._retention_days)

        async def operation(session):
            return await CommandUsageRepository(session).delete_older_than(cutoff)

        return await run_db_operation(await self._get_db_manager(), operation)
