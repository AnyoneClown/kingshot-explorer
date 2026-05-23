"""Guild-level bot configuration service."""

import logging

from db import GuildConfiguration, get_db
from services.database_service import DatabaseService

logger = logging.getLogger(__name__)


class GuildConfigurationService:
    """Read and update persisted bot configuration for a guild."""

    def __init__(self, default_use_voice_replies: bool = True):
        self._default_use_voice_replies = default_use_voice_replies

    @staticmethod
    async def get_or_create_guild_configuration(session, guild_id: int, default_use_voice_replies: bool):
        guild_config = await DatabaseService.get_guild_configuration(session, guild_id)
        if guild_config is not None:
            return guild_config

        guild_config = GuildConfiguration(
            guild_id=guild_id,
            use_voice_replies=default_use_voice_replies,
        )
        session.add(guild_config)
        await session.flush()
        return guild_config

    @staticmethod
    async def set_use_voice_replies(session, guild_id: int, use_voice_replies: bool, default_use_voice_replies: bool):
        guild_config = await GuildConfigurationService.get_or_create_guild_configuration(
            session,
            guild_id,
            default_use_voice_replies,
        )
        guild_config.use_voice_replies = use_voice_replies
        await session.flush()
        return guild_config

    async def get_or_create_for_guild(self, guild_id: int):
        db = get_db()
        async with db.session() as session:
            return await self.get_or_create_guild_configuration(
                session,
                guild_id,
                self._default_use_voice_replies,
            )

    async def set_use_voice_replies_for_guild(self, guild_id: int, use_voice_replies: bool):
        db = get_db()
        async with db.session() as session:
            return await self.set_use_voice_replies(
                session,
                guild_id,
                use_voice_replies,
                self._default_use_voice_replies,
            )
