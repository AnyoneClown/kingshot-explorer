"""Simple database health-check service."""

from __future__ import annotations

from sqlalchemy import text

from db.session import DatabaseManager, get_db


class DatabaseHealthService:
    """Run basic database checks through a service boundary."""

    def __init__(self, db_manager: DatabaseManager | None = None):
        self._db_manager = db_manager

    async def _get_db_manager(self):
        return self._db_manager or get_db()

    async def check_database(self) -> tuple[bool, str]:
        try:
            db = await self._get_db_manager()
            async with db.session() as session:
                await session.execute(text("SELECT 1"))
            return True, "Reachable"
        except Exception as exc:
            return False, str(exc)
