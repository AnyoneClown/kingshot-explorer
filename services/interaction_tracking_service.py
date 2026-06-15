"""Cross-cutting interaction tracking service."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Optional

from db.session import DatabaseManager, get_db
from repositories.discord_repositories import (
    GiftCodeRedemptionRepository,
    GuildConfigurationRepository,
    RegisteredPlayerRepository,
    TranslationLogRepository,
    UserRepository,
)


class InteractionTrackingService:
    """Tracks user activity and related persistence side effects."""

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

    async def track_user(self, *, session=None, user_id, username, discriminator, display_name):
        async with self._session(session) as db_session:
            user_repo = UserRepository(db_session)
            return await user_repo.upsert(user_id, username, discriminator, display_name)

    async def track_translation(
        self,
        user_id: int,
        original_text: str,
        translated_text: str,
        target_language: str,
        source_language: Optional[str] = None,
        translation_type: str = "manual",
        guild_id: Optional[int] = None,
        channel_id: Optional[int] = None,
        username: Optional[str] = None,
        discriminator: Optional[str] = None,
        display_name: Optional[str] = None,
        session: Optional[Any] = None,
    ):
        async with self._session(session) as db_session:
            if username is not None:
                await self.track_user(
                    session=db_session,
                    user_id=user_id,
                    username=username,
                    discriminator=discriminator,
                    display_name=display_name,
                )

            translation_repo = TranslationLogRepository(db_session)
            await translation_repo.create(
                user_id=user_id,
                original_text=original_text,
                translated_text=translated_text,
                target_language=target_language,
                source_language=source_language,
                translation_type=translation_type,
                guild_id=guild_id,
                channel_id=channel_id,
            )

    async def track_player_lookup(
        self,
        user_id: int,
        player_id: str,
        player_name: Optional[str] = None,
        kingdom: Optional[str] = None,
        castle_level: Optional[str] = None,
        success: bool = True,
        guild_id: Optional[int] = None,
        channel_id: Optional[int] = None,
        username: Optional[str] = None,
        discriminator: Optional[str] = None,
        display_name: Optional[str] = None,
        session: Optional[Any] = None,
    ):
        del guild_id, channel_id
        async with self._session(session) as db_session:
            if username is not None:
                await self.track_user(
                    session=db_session,
                    user_id=user_id,
                    username=username,
                    discriminator=discriminator,
                    display_name=display_name,
                )

            if not success:
                return None

            return await RegisteredPlayerRepository(db_session).upsert_profile(
                player_id=player_id,
                player_name=player_name,
                kingdom=kingdom,
                castle_level=castle_level,
                added_by_user_id=user_id,
            )

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

    async def log_gift_code_redemption(
        self,
        *,
        user_id: int,
        player_id: str,
        gift_code: str,
        success: bool,
        response_message: Optional[str] = None,
        error_code: Optional[str] = None,
        guild_id: Optional[int] = None,
        channel_id: Optional[int] = None,
        session: Optional[Any] = None,
    ):
        async with self._session(session) as db_session:
            redemption_repo = GiftCodeRedemptionRepository(db_session)
            await redemption_repo.create(
                user_id=user_id,
                player_id=player_id,
                gift_code=gift_code,
                success=success,
                response_message=response_message,
                error_code=error_code,
                guild_id=guild_id,
                channel_id=channel_id,
            )

    async def is_voice_replies_enabled(self, guild_id: int, default_enabled: bool = True) -> bool:
        async with self._session() as db_session:
            repo = GuildConfigurationRepository(db_session)
            guild_config = await repo.get(guild_id)

        if guild_config is None:
            return default_enabled

        return guild_config.use_voice_replies
