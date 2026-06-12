"""Player registry service for gift code redemption targets."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, List, Optional

from db.session import DatabaseManager, get_db
from repositories.discord_repositories import RegisteredPlayerRepository


class PlayerRegistryService:
    """Service managing gift-code registered players."""

    def __init__(self, db_manager: DatabaseManager | None = None):
        self._db_manager = db_manager

    async def _get_db_manager(self):
        return self._db_manager or get_db()

    @asynccontextmanager
    async def _session(self, existing_session=None):
        if existing_session is not None:
            yield existing_session
            return

        db = await self._get_db_manager()
        async with db.session() as session:
            yield session

    async def get_registered_player(self, player_id: str, session: Optional[Any] = None):
        async with self._session(session) as db_session:
            return await RegisteredPlayerRepository(db_session).get(player_id)

    async def get_registered_players(self, enabled_only: bool = True, session: Optional[Any] = None) -> List:
        async with self._session(session) as db_session:
            return await RegisteredPlayerRepository(db_session).list(enabled_only=enabled_only)

    async def add_registered_player(
        self,
        *,
        player_id: str,
        added_by_user_id: int,
        player_name: Optional[str] = None,
        kingdom: Optional[str] = None,
        castle_level: Optional[str] = None,
        enabled: bool = True,
        session: Optional[Any] = None,
    ):
        async with self._session(session) as db_session:
            return await RegisteredPlayerRepository(db_session).add_or_update(
                player_id=player_id,
                added_by_user_id=added_by_user_id,
                player_name=player_name,
                kingdom=kingdom,
                castle_level=castle_level,
                enabled=enabled,
            )

    async def remove_registered_player(self, player_id: str, session: Optional[Any] = None) -> bool:
        async with self._session(session) as db_session:
            return await RegisteredPlayerRepository(db_session).remove(player_id)

    async def toggle_registered_player(self, player_id: str, session: Optional[Any] = None) -> Optional[bool]:
        async with self._session(session) as db_session:
            return await RegisteredPlayerRepository(db_session).toggle(player_id)

    async def sync_player_metadata(
        self,
        player_id: str,
        player_name: Optional[str] = None,
        kingdom: Optional[str] = None,
        castle_level: Optional[str] = None,
        added_by_user_id: Optional[int] = None,
        session: Optional[Any] = None,
    ) -> bool:
        async with self._session(session) as db_session:
            return await RegisteredPlayerRepository(db_session).sync_metadata(
                player_id=player_id,
                player_name=player_name,
                kingdom=kingdom,
                castle_level=castle_level,
                added_by_user_id=added_by_user_id,
            )
