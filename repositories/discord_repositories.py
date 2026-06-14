"""Repository layer for Discord bot persistence operations."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Set

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    GiftCode,
    GiftCodeRedemption,
    GuildConfiguration,
    RegisteredPlayer,
    TranslationLog,
    User,
)

logger = logging.getLogger(__name__)


class UserRepository:
    """Repository for user records."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_by_id(self, user_id: int) -> Optional[User]:
        result = await self._session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def upsert(self, user_id: int, username: str, discriminator: Optional[str], display_name: Optional[str]) -> User:
        user = await self.get_by_id(user_id)

        if user:
            user.last_seen = datetime.utcnow()
            user.username = username
            user.discriminator = discriminator
            if display_name:
                user.display_name = display_name
            logger.debug("Updated existing user: %s", user_id)
            return user

        user = User(
            id=user_id,
            username=username,
            discriminator=discriminator,
            display_name=display_name,
        )
        self._session.add(user)
        logger.info("Created new user: %s (%s)", user_id, username)
        await self._session.flush()
        return user


class TranslationLogRepository:
    """Repository for translation log records."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(
        self,
        user_id: int,
        original_text: str,
        translated_text: str,
        target_language: str,
        source_language: Optional[str] = None,
        translation_type: str = "manual",
        guild_id: Optional[int] = None,
        channel_id: Optional[int] = None,
    ) -> TranslationLog:
        log = TranslationLog(
            user_id=user_id,
            original_text=original_text,
            translated_text=translated_text,
            target_language=target_language,
            source_language=source_language,
            translation_type=translation_type,
            guild_id=guild_id,
            channel_id=channel_id,
        )
        self._session.add(log)
        await self._session.flush()
        logger.info("Logged translation for user %s: %s (%s)", user_id, target_language, translation_type)
        return log


class GuildConfigurationRepository:
    """Repository for guild-level configuration."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, guild_id: int) -> Optional[GuildConfiguration]:
        result = await self._session.execute(select(GuildConfiguration).where(GuildConfiguration.guild_id == guild_id))
        return result.scalar_one_or_none()

    async def get_or_create(
        self,
        guild_id: int,
        default_use_voice_replies: bool,
        default_use_random_replies: bool = False,
    ) -> GuildConfiguration:
        guild_config = await self.get(guild_id)
        if guild_config is not None:
            return guild_config

        guild_config = GuildConfiguration(
            guild_id=guild_id,
            use_voice_replies=default_use_voice_replies,
            use_random_replies=default_use_random_replies,
        )
        self._session.add(guild_config)
        await self._session.flush()
        return guild_config

    async def set_voice_replies(
        self,
        guild_id: int,
        use_voice_replies: bool,
        default_use_voice_replies: bool,
        default_use_random_replies: bool = False,
    ):
        guild_config = await self.get_or_create(guild_id, default_use_voice_replies, default_use_random_replies)
        guild_config.use_voice_replies = use_voice_replies
        await self._session.flush()
        return guild_config

    async def set_random_replies(
        self,
        guild_id: int,
        use_random_replies: bool,
        default_use_voice_replies: bool,
        default_use_random_replies: bool = False,
    ):
        guild_config = await self.get_or_create(guild_id, default_use_voice_replies, default_use_random_replies)
        guild_config.use_random_replies = use_random_replies
        await self._session.flush()
        return guild_config


class RegisteredPlayerRepository:
    """Repository for registered players used in gift code workflow."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def _get(self, player_id: str) -> Optional[RegisteredPlayer]:
        result = await self._session.execute(select(RegisteredPlayer).where(RegisteredPlayer.player_id == player_id))
        return result.scalar_one_or_none()

    async def upsert_profile(
        self,
        player_id: str,
        player_name: Optional[str] = None,
        kingdom: Optional[str] = None,
        castle_level: Optional[str] = None,
        enabled: Optional[bool] = None,
        added_by_user_id: Optional[int] = None,
        overwrite_owner: bool = False,
    ) -> Optional[RegisteredPlayer]:
        player = await self._get(player_id)

        if player:
            if player_name:
                player.player_name = player_name
            if kingdom is not None:
                player.kingdom = kingdom
            if castle_level is not None:
                player.castle_level = castle_level
            if enabled is not None:
                player.enabled = enabled
            if overwrite_owner and added_by_user_id is not None:
                player.added_by_user_id = added_by_user_id

            await self._session.flush()
            return player

        if added_by_user_id is None:
            return None

        player = RegisteredPlayer(
            player_id=player_id,
            player_name=player_name,
            kingdom=kingdom,
            castle_level=castle_level,
            enabled=(enabled if enabled is not None else False),
            added_by_user_id=added_by_user_id,
        )
        self._session.add(player)
        await self._session.flush()
        return player

    async def list(self, enabled_only: bool = True):
        query = select(RegisteredPlayer)
        if enabled_only:
            query = query.where(RegisteredPlayer.enabled.is_(True))
        query = query.order_by(RegisteredPlayer.player_id)

        result = await self._session.execute(query)
        players = result.scalars().all()
        logger.info("Retrieved %s registered players (enabled_only=%s)", len(players), enabled_only)
        return list(players)

    async def get(self, player_id: str) -> Optional[RegisteredPlayer]:
        return await self._get(player_id)

    async def add_or_update(
        self,
        player_id: str,
        added_by_user_id: int,
        player_name: Optional[str] = None,
        kingdom: Optional[str] = None,
        castle_level: Optional[str] = None,
        enabled: bool = True,
    ) -> RegisteredPlayer:
        player = await self.upsert_profile(
            player_id=player_id,
            player_name=player_name,
            kingdom=kingdom,
            castle_level=castle_level,
            enabled=enabled,
            added_by_user_id=added_by_user_id,
            overwrite_owner=True,
        )
        if player is None:
            raise ValueError("Unable to upsert registered player")

        logger.info("Upserted registered player %s (enabled=%s)", player_id, enabled)
        return player

    async def remove(self, player_id: str) -> bool:
        player = await self._get(player_id)
        if player:
            await self._session.delete(player)
            await self._session.flush()
            logger.info("Removed registered player %s", player_id)
            return True

        logger.warning("Attempted to remove non-existent player %s", player_id)
        return False

    async def toggle(self, player_id: str) -> Optional[bool]:
        player = await self._get(player_id)
        if not player:
            logger.warning("Attempted to toggle non-existent player %s", player_id)
            return None

        player.enabled = not player.enabled
        await self._session.flush()
        logger.info("Toggled registered player %s to enabled=%s", player_id, player.enabled)
        return player.enabled

    async def sync_metadata(
        self,
        player_id: str,
        player_name: Optional[str] = None,
        kingdom: Optional[str] = None,
        castle_level: Optional[str] = None,
        added_by_user_id: Optional[int] = None,
    ) -> bool:
        player = await self.upsert_profile(
            player_id=player_id,
            player_name=player_name,
            kingdom=kingdom,
            castle_level=castle_level,
            added_by_user_id=added_by_user_id,
        )
        if player is None:
            return False

        logger.debug("Refreshed metadata for player %s", player_id)
        return True


class GiftCodeRepository:
    """Repository for gift code catalog records."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, code_id: int) -> Optional[GiftCode]:
        result = await self._session.execute(select(GiftCode).where(GiftCode.id == code_id))
        return result.scalar_one_or_none()

    async def add_or_update(
        self,
        code_id: int,
        code: str,
        created_at_api: datetime,
        expires_at: Optional[datetime] = None,
    ) -> tuple[bool, GiftCode]:
        existing_code = await self.get(code_id)
        if existing_code:
            existing_code.expires_at = expires_at
            logger.debug("Updated existing gift code %s (ID: %s)", code, code_id)
            return False, existing_code

        new_code = GiftCode(
            id=code_id,
            code=code,
            expires_at=expires_at,
            created_at_api=created_at_api,
        )
        self._session.add(new_code)
        logger.info("Added new tracked gift code: %s (ID: %s)", code, code_id)
        await self._session.flush()
        return True, new_code

    async def list_all(self):
        result = await self._session.execute(select(GiftCode).order_by(GiftCode.created_at_api.desc()))
        return list(result.scalars().all())


class GiftCodeRedemptionRepository:
    """Repository for gift code redemption attempts."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def create(
        self,
        user_id: int,
        player_id: str,
        gift_code: str,
        success: bool,
        response_message: Optional[str] = None,
        error_code: Optional[str] = None,
        guild_id: Optional[int] = None,
        channel_id: Optional[int] = None,
    ) -> GiftCodeRedemption:
        log = GiftCodeRedemption(
            user_id=user_id,
            player_id=player_id,
            gift_code=gift_code,
            success=success,
            response_message=response_message,
            error_code=error_code,
            guild_id=guild_id,
            channel_id=channel_id,
        )
        self._session.add(log)
        await self._session.flush()
        logger.info(
            "Logged gift code redemption by user %s: player %s, code '%s' (success=%s)",
            user_id,
            player_id,
            gift_code,
            success,
        )
        return log

    async def find_successful_redemption(self, player_id: int, gift_code: str):
        result = await self._session.execute(
            select(GiftCodeRedemption)
            .where(GiftCodeRedemption.player_id == str(player_id))
            .where(GiftCodeRedemption.gift_code == gift_code)
            .where(GiftCodeRedemption.success.is_(True))
            .order_by(GiftCodeRedemption.created_at.desc())
        )
        return result.scalar_one_or_none()

    async def redeemed_player_ids(self, gift_code: str) -> Set[str]:
        result = await self._session.execute(
            select(GiftCodeRedemption.player_id)
            .where(GiftCodeRedemption.gift_code == gift_code)
            .where(
                or_(
                    GiftCodeRedemption.success.is_(True),
                    GiftCodeRedemption.error_code == "ALREADY_REDEEMED_BY_API",
                )
            )
        )
        return set(result.scalars().all())
