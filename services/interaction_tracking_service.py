"""Cross-cutting interaction tracking service."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from db.session import DatabaseManager, get_db
from repositories.discord_repositories import (
    GiftCodeRedemptionRepository,
    RegisteredPlayerRepository,
    TranslationLogRepository,
    UserRepository,
)
from services.transactions import run_db_operation

logger = logging.getLogger(__name__)

# Rows deleted per transaction when purging translation logs (keeps CockroachDB
# transactions small).
TRANSLATION_PURGE_BATCH_SIZE = 5000


class InteractionTrackingService:
    """Tracks user activity and related persistence side effects.

    Writes that create rows on first sight (users, player profiles) are
    read-then-insert, so they run through ``run_in_transaction`` with
    ``retry_on_integrity_error=True``: a concurrent insert of the same key makes the
    loser retry and find the existing row instead of failing.
    """

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

    async def _run(self, operation, *, session=None, retry_on_integrity_error: bool = False):
        if session is not None:
            return await operation(session)
        return await run_db_operation(
            await self._get_db_manager(),
            operation,
            retry_on_integrity_error=retry_on_integrity_error,
        )

    async def track_user(self, *, session=None, user_id, username, discriminator, display_name):
        async def operation(db_session):
            return await UserRepository(db_session).upsert(user_id, username, discriminator, display_name)

        return await self._run(operation, session=session, retry_on_integrity_error=True)

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
        async def operation(db_session):
            if username is not None:
                await UserRepository(db_session).upsert(user_id, username, discriminator, display_name)

            await TranslationLogRepository(db_session).create(
                user_id=user_id,
                original_text=original_text,
                translated_text=translated_text,
                target_language=target_language,
                source_language=source_language,
                translation_type=translation_type,
                guild_id=guild_id,
                channel_id=channel_id,
            )

        await self._run(operation, session=session, retry_on_integrity_error=True)

    async def purge_old_translation_logs(self, older_than_days: int) -> int:
        """Delete translation logs older than ``older_than_days`` days; return the count.

        Deletes in batches of ``TRANSLATION_PURGE_BATCH_SIZE`` rows, one transaction
        per batch, so a large backlog never builds one huge transaction.
        """
        if older_than_days < 1:
            raise ValueError("older_than_days must be >= 1")
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

        async def operation(db_session) -> int:
            return await TranslationLogRepository(db_session).delete_older_than(
                cutoff, limit=TRANSLATION_PURGE_BATCH_SIZE
            )

        total = 0
        while True:
            deleted = await self._run(operation)
            total += deleted
            if deleted < TRANSLATION_PURGE_BATCH_SIZE:
                break
        if total:
            logger.info("Purged %s translation logs older than %s days", total, older_than_days)
        return total

    async def track_player_lookup(
        self,
        user_id: int,
        player_id: str,
        player_uid: Optional[str] = None,
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
        """Record a lookup: refresh the user and, when the player is on the gift-code list, its profile.

        A lookup never puts a player on the list: every profile row is a listed
        account (shown in its kingdom's roster, owned by who added it), so a player
        that is not listed gets no row. There is no lookup log table, so
        ``guild_id``/``channel_id`` are only used for diagnostics.
        """
        logger.debug(
            "Player lookup by user %s for %s (success=%s, guild=%s, channel=%s)",
            user_id,
            player_id,
            success,
            guild_id,
            channel_id,
        )

        async def operation(db_session):
            if username is not None:
                await UserRepository(db_session).upsert(user_id, username, discriminator, display_name)

            if not success:
                return None

            return await RegisteredPlayerRepository(db_session).upsert_profile(
                player_id=player_id,
                player_uid=player_uid,
                player_name=player_name,
                kingdom=kingdom,
                castle_level=castle_level,
                # Without an owner the profile is only refreshed, never created.
                added_by_user_id=None,
            )

        return await self._run(operation, session=session, retry_on_integrity_error=True)

    async def sync_player_metadata(
        self,
        player_id: str,
        player_uid: Optional[str] = None,
        player_name: Optional[str] = None,
        kingdom: Optional[str] = None,
        castle_level: Optional[str] = None,
        added_by_user_id: Optional[int] = None,
        session: Optional[Any] = None,
    ) -> bool:
        async def operation(db_session):
            return await RegisteredPlayerRepository(db_session).sync_metadata(
                player_id=player_id,
                player_uid=player_uid,
                player_name=player_name,
                kingdom=kingdom,
                castle_level=castle_level,
                added_by_user_id=added_by_user_id,
            )

        return await self._run(operation, session=session, retry_on_integrity_error=True)

    async def sync_player_metadata_many(
        self,
        players: list[dict[str, Any]],
        session: Optional[Any] = None,
    ) -> int:
        async def operation(db_session):
            return await RegisteredPlayerRepository(db_session).sync_metadata_many(players)

        return await self._run(operation, session=session, retry_on_integrity_error=True)

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
        async def operation(db_session):
            await GiftCodeRedemptionRepository(db_session).create(
                user_id=user_id,
                player_id=player_id,
                gift_code=gift_code,
                success=success,
                response_message=response_message,
                error_code=error_code,
                guild_id=guild_id,
                channel_id=channel_id,
            )

        await self._run(operation, session=session)

    async def log_gift_code_redemptions_many(
        self,
        rows: list[dict[str, Any]],
        session: Optional[Any] = None,
    ):
        async def operation(db_session):
            return await GiftCodeRedemptionRepository(db_session).create_many(rows)

        return await self._run(operation, session=session)
