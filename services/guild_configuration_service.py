"""Guild-level bot configuration service."""

import logging

from db.session import DatabaseManager, get_db
from repositories.discord_repositories import GuildConfigurationRepository

logger = logging.getLogger(__name__)


class GuildConfigurationService:
    """Read and update persisted bot configuration for a guild."""

    def __init__(
        self,
        default_use_voice_replies: bool = True,
        db_manager: DatabaseManager | None = None,
    ):
        self._default_use_voice_replies = default_use_voice_replies
        self._db_manager = db_manager

    async def _get_db_manager(self):
        return self._db_manager or get_db()

    @staticmethod
    async def get_or_create_guild_configuration(session, guild_id: int, default_use_voice_replies: bool):
        repo = GuildConfigurationRepository(session)
        return await repo.get_or_create(guild_id, default_use_voice_replies)

    @staticmethod
    async def set_use_voice_replies(session, guild_id: int, use_voice_replies: bool, default_use_voice_replies: bool):
        repo = GuildConfigurationRepository(session)
        return await repo.set_voice_replies(guild_id, use_voice_replies, default_use_voice_replies)

    async def get_or_create_for_guild(self, guild_id: int):
        db = await self._get_db_manager()
        async with db.session() as session:
            return await self.get_or_create_guild_configuration(
                session,
                guild_id,
                self._default_use_voice_replies,
            )

    async def set_use_voice_replies_for_guild(self, guild_id: int, use_voice_replies: bool):
        db = await self._get_db_manager()
        async with db.session() as session:
            return await self.set_use_voice_replies(
                session,
                guild_id,
                use_voice_replies,
                self._default_use_voice_replies,
            )
