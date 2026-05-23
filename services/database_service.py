"""Database service for managing users and statistics."""

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import GiftCode, GiftCodeRedemption, GuildConfiguration, RegisteredPlayer, TranslationLog, User

logger = logging.getLogger(__name__)


class DatabaseService:
    """Service for database operations related to users and stats."""

    @staticmethod
    async def get_or_create_user(
        session: AsyncSession,
        user_id: int,
        username: str,
        discriminator: Optional[str] = None,
        display_name: Optional[str] = None,
    ) -> User:
        """
        Get an existing user or create a new one.

        Args:
            session: Database session
            user_id: Discord user ID
            username: Username
            discriminator: User discriminator (for legacy Discord usernames)
            display_name: Display name/nickname

        Returns:
            User object
        """
        # Try to get existing user
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()

        if user:
            # Update last seen and other info if changed
            user.last_seen = datetime.utcnow()
            user.username = username
            user.discriminator = discriminator
            if display_name:
                user.display_name = display_name
            logger.debug(f"Updated existing user: {user_id}")
        else:
            # Create new user
            user = User(
                id=user_id,
                username=username,
                discriminator=discriminator,
                display_name=display_name,
            )
            session.add(user)
            logger.info(f"Created new user: {user_id} ({username})")

        await session.flush()
        return user

    @staticmethod
    async def log_translation(
        session: AsyncSession,
        user_id: int,
        original_text: str,
        translated_text: str,
        target_language: str,
        source_language: Optional[str] = None,
        translation_type: str = "manual",
        guild_id: Optional[int] = None,
        channel_id: Optional[int] = None,
    ) -> TranslationLog:
        """
        Log a translation to the database.

        Args:
            session: Database session
            user_id: Discord user ID
            original_text: Original text
            translated_text: Translated text
            target_language: Target language code
            source_language: Source language code (optional)
            translation_type: Type of translation (manual, reaction, command, etc.)
            guild_id: Discord guild ID (optional)
            channel_id: Discord channel ID (optional)

        Returns:
            TranslationLog object
        """
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
        session.add(log)
        await session.flush()
        logger.info(f"Logged translation for user {user_id}: {target_language} ({translation_type})")
        return log

    @staticmethod
    async def get_user(session: AsyncSession, user_id: int) -> Optional[User]:
        """
        Get user information.

        Args:
            session: Database session
            user_id: Discord user ID

        Returns:
            User object or None if not found
        """
        result = await session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_guild_configuration(session: AsyncSession, guild_id: int) -> Optional[GuildConfiguration]:
        """Get persisted bot configuration for a guild."""
        result = await session.execute(select(GuildConfiguration).where(GuildConfiguration.guild_id == guild_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def _upsert_player_profile(
        session: AsyncSession,
        player_id: str,
        player_name: Optional[str] = None,
        kingdom: Optional[str] = None,
        castle_level: Optional[str] = None,
        enabled: Optional[bool] = None,
        added_by_user_id: Optional[int] = None,
        overwrite_owner: bool = False,
    ) -> Optional[RegisteredPlayer]:
        """Create or update a player profile in the unified player table."""
        result = await session.execute(select(RegisteredPlayer).where(RegisteredPlayer.player_id == player_id))
        player = result.scalar_one_or_none()

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

            await session.flush()
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
        session.add(player)
        await session.flush()
        return player

    @staticmethod
    async def log_player_lookup(
        session: AsyncSession,
        user_id: int,
        player_id: str,
        player_name: Optional[str] = None,
        kingdom: Optional[str] = None,
        castle_level: Optional[str] = None,
        success: bool = True,
        guild_id: Optional[int] = None,
        channel_id: Optional[int] = None,
    ) -> Optional[RegisteredPlayer]:
        """
        Sync a player lookup into the unified player table.

        Args:
            session: Database session
            user_id: Discord user ID who requested the lookup
            player_id: The player ID that was looked up
            player_name: Player name (if found)
            kingdom: Player's kingdom (if found)
            castle_level: Player's castle level (if found)
            success: Whether the lookup was successful
            guild_id: Unused, kept for backward compatibility
            channel_id: Unused, kept for backward compatibility

        Returns:
            RegisteredPlayer object when upserted, else None
        """
        # Keep arguments referenced so linters don't flag intentionally retained compatibility args.
        _ = guild_id
        _ = channel_id

        if not success:
            logger.info(f"Player lookup failed for {player_id}; no player profile upsert performed")
            return None

        player = await DatabaseService._upsert_player_profile(
            session=session,
            player_id=player_id,
            player_name=player_name,
            kingdom=kingdom,
            castle_level=castle_level,
            added_by_user_id=user_id,
        )
        if player:
            logger.info(f"Synced player lookup by user {user_id}: player {player_id}")
        return player

    @staticmethod
    async def log_gift_code_redemption(
        session: AsyncSession,
        user_id: int,
        player_id: str,
        gift_code: str,
        success: bool,
        response_message: Optional[str] = None,
        error_code: Optional[str] = None,
        guild_id: Optional[int] = None,
        channel_id: Optional[int] = None,
    ) -> GiftCodeRedemption:
        """
        Log a gift code redemption attempt to the database.

        Args:
            session: Database session
            user_id: Discord user ID who requested the redemption
            player_id: The player ID for whom the code was redeemed
            gift_code: The gift code that was used
            success: Whether the redemption was successful
            response_message: Response message from the API
            error_code: Error code if redemption failed
            guild_id: Discord guild ID (optional)
            channel_id: Discord channel ID (optional)

        Returns:
            GiftCodeRedemption object
        """
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
        session.add(log)
        await session.flush()
        logger.info(
            f"Logged gift code redemption by user {user_id}: "
            f"player {player_id}, code '{gift_code}' (success={success})"
        )
        return log

    @staticmethod
    async def add_registered_player(
        session: AsyncSession,
        player_id: str,
        added_by_user_id: int,
        player_name: Optional[str] = None,
        kingdom: Optional[str] = None,
        castle_level: Optional[str] = None,
        enabled: bool = True,
    ) -> RegisteredPlayer:
        """
        Add or update a registered player for gift code redemption.

        Args:
            session: Database session
            player_id: The player ID to register
            added_by_user_id: Discord user ID who added the player
            player_name: Player name (optional)
            kingdom: Player kingdom (optional)
            castle_level: Player castle level (optional)
            enabled: Whether the player is enabled for redemption

        Returns:
            RegisteredPlayer object
        """
        player = await DatabaseService._upsert_player_profile(
            session=session,
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

        logger.info(f"Upserted registered player {player_id} (enabled={enabled})")
        return player

    @staticmethod
    async def get_registered_players(
        session: AsyncSession,
        enabled_only: bool = True,
    ) -> list[RegisteredPlayer]:
        """
        Get all registered players.

        Args:
            session: Database session
            enabled_only: If True, only return enabled players

        Returns:
            List of RegisteredPlayer objects
        """
        query = select(RegisteredPlayer)
        if enabled_only:
            query = query.where(RegisteredPlayer.enabled.is_(True))
        query = query.order_by(RegisteredPlayer.player_id)

        result = await session.execute(query)
        players = result.scalars().all()
        logger.info(f"Retrieved {len(players)} registered players (enabled_only={enabled_only})")
        return list(players)

    @staticmethod
    async def get_registered_player(
        session: AsyncSession,
        player_id: str,
    ) -> Optional[RegisteredPlayer]:
        """
        Get a single registered player by ID.

        Args:
            session: Database session
            player_id: The player ID to look up

        Returns:
            RegisteredPlayer or None
        """
        result = await session.execute(select(RegisteredPlayer).where(RegisteredPlayer.player_id == player_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def remove_registered_player(
        session: AsyncSession,
        player_id: str,
    ) -> bool:
        """
        Remove a player profile.

        Args:
            session: Database session
            player_id: The player ID to remove

        Returns:
            True if player was removed, False if not found
        """
        result = await session.execute(select(RegisteredPlayer).where(RegisteredPlayer.player_id == player_id))
        player = result.scalar_one_or_none()

        if player:
            await session.delete(player)
            await session.flush()
            logger.info(f"Removed registered player {player_id}")
            return True
        else:
            logger.warning(f"Attempted to remove non-existent player {player_id}")
            return False

    @staticmethod
    async def toggle_registered_player(
        session: AsyncSession,
        player_id: str,
    ) -> Optional[bool]:
        """
        Toggle a registered player's enabled status.

        Args:
            session: Database session
            player_id: The player ID to toggle

        Returns:
            New enabled status, or None if player not found
        """
        result = await session.execute(select(RegisteredPlayer).where(RegisteredPlayer.player_id == player_id))
        player = result.scalar_one_or_none()

        if player:
            player.enabled = not player.enabled
            await session.flush()
            logger.info(f"Toggled registered player {player_id} to enabled={player.enabled}")
            return player.enabled
        else:
            logger.warning(f"Attempted to toggle non-existent player {player_id}")
            return None

    @staticmethod
    async def update_registered_player_metadata(
        session: AsyncSession,
        player_id: str,
        player_name: Optional[str] = None,
        kingdom: Optional[str] = None,
        castle_level: Optional[str] = None,
        added_by_user_id: Optional[int] = None,
    ) -> bool:
        """Update metadata for a player, creating a disabled profile if user context is provided."""
        player = await DatabaseService._upsert_player_profile(
            session=session,
            player_id=player_id,
            player_name=player_name,
            kingdom=kingdom,
            castle_level=castle_level,
            added_by_user_id=added_by_user_id,
        )
        if not player:
            return False

        logger.debug("Refreshed metadata for player %s", player_id)
        return True

    @staticmethod
    async def add_or_update_gift_code(
        session: AsyncSession,
        code_id: int,
        code: str,
        created_at_api: datetime,
        expires_at: Optional[datetime] = None,
    ) -> tuple[bool, GiftCode]:
        """
        Add a new gift code or update an existing one.

        Args:
            session: Database session
            code_id: Gift code ID from API
            code: The gift code string
            created_at_api: When the code was created in the API
            expires_at: When the code expires (optional)

        Returns:
            Tuple of (is_new, GiftCode)
        """
        result = await session.execute(select(GiftCode).where(GiftCode.id == code_id))
        existing_code = result.scalar_one_or_none()

        if existing_code:
            existing_code.expires_at = expires_at
            logger.debug(f"Updated existing gift code {code} (ID: {code_id})")
            return False, existing_code
        else:
            new_code = GiftCode(
                id=code_id,
                code=code,
                expires_at=expires_at,
                created_at_api=created_at_api,
            )
            session.add(new_code)
            logger.info(f"Added new tracked gift code: {code} (ID: {code_id})")
            return True, new_code

    @staticmethod
    async def get_all_gift_codes(session: AsyncSession) -> list[GiftCode]:
        """
        Get all tracked gift codes.

        Args:
            session: Database session

        Returns:
            List of GiftCode objects
        """
        result = await session.execute(select(GiftCode).order_by(GiftCode.created_at_api.desc()))
        return list(result.scalars().all())
