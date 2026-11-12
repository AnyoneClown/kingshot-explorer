"""Repository layer for Discord bot persistence operations."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Iterable, Iterator, Optional, Sequence, Set

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import (
    CommandUsageDaily,
    GiftCode,
    GiftCodeAnnouncement,
    GiftCodeRedemption,
    GuildConfiguration,
    RegisteredPlayer,
    TranslationLog,
    User,
)

logger = logging.getLogger(__name__)

# Keep IN (...) lists comfortably below driver/planner limits.
_IN_CLAUSE_CHUNK = 500

AUTO_REDEEM_PENDING = "pending"
AUTO_REDEEM_COMPLETED = "completed"


def _chunks(values: Sequence[Any], size: int = _IN_CLAUSE_CHUNK) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _unique(values: Iterable[Any]) -> list[Any]:
    return list(dict.fromkeys(values))


class UserRepository:
    """Repository for user records.

    ``upsert`` is read-then-insert: two concurrent first interactions of the same
    user race on the primary key. Callers run it through
    ``DatabaseManager.run_in_transaction(..., retry_on_integrity_error=True)`` so the
    loser retries and then finds the row.
    """

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get_by_id(self, user_id: int) -> Optional[User]:
        result = await self._session.execute(select(User).where(User.id == user_id))
        return result.scalar_one_or_none()

    async def upsert(self, user_id: int, username: str, discriminator: Optional[str], display_name: Optional[str]) -> User:
        user = await self.get_by_id(user_id)

        if user:
            user.last_seen = datetime.now(timezone.utc)
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

    async def delete_older_than(self, cutoff: datetime, *, limit: int) -> int:
        """Delete up to ``limit`` logs created before ``cutoff``; return the count deleted."""
        result = await self._session.execute(
            select(TranslationLog.id).where(TranslationLog.created_at < cutoff).limit(limit)
        )
        ids = list(result.scalars().all())
        if not ids:
            return 0
        await self._session.execute(delete(TranslationLog).where(TranslationLog.id.in_(ids)))
        return len(ids)

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

    async def get_or_create(self, guild_id: int, default_use_random_replies: bool = False) -> GuildConfiguration:
        guild_config = await self.get(guild_id)
        if guild_config is not None:
            return guild_config

        guild_config = GuildConfiguration(guild_id=guild_id, use_random_replies=default_use_random_replies)
        self._session.add(guild_config)
        await self._session.flush()
        return guild_config

    async def set_random_replies(
        self,
        guild_id: int,
        use_random_replies: bool,
        default_use_random_replies: bool = False,
    ):
        guild_config = await self.get_or_create(guild_id, default_use_random_replies)
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

    async def get_by_uid(self, player_uid: str) -> Optional[RegisteredPlayer]:
        result = await self._session.execute(
            select(RegisteredPlayer).where(RegisteredPlayer.player_uid == player_uid)
        )
        return result.scalar_one_or_none()

    async def list_by_uids(self, player_uids: list[str]) -> dict[str, RegisteredPlayer]:
        normalized_uids = [str(uid) for uid in player_uids if uid not in (None, "", "0", 0)]
        if not normalized_uids:
            return {}

        result = await self._session.execute(
            select(RegisteredPlayer).where(RegisteredPlayer.player_uid.in_(normalized_uids))
        )
        return {str(player.player_uid): player for player in result.scalars().all() if player.player_uid}

    async def _assign_uid_if_available(self, player: RegisteredPlayer, player_uid: Optional[str]) -> None:
        if player_uid in (None, "", "0", 0):
            return

        normalized_uid = str(player_uid)
        existing = await self.get_by_uid(normalized_uid)
        if existing is not None and existing.id != player.id:
            logger.warning(
                "Skipping uid cache update for player %s because uid %s is already mapped to player %s",
                player.player_id,
                normalized_uid,
                existing.player_id,
            )
            return

        player.player_uid = normalized_uid

    @staticmethod
    def _assign_uid_from_maps(
        player: RegisteredPlayer,
        player_uid: Optional[str],
        uid_map: dict[str, RegisteredPlayer],
    ) -> None:
        if player_uid in (None, "", "0", 0):
            return

        normalized_uid = str(player_uid)
        existing = uid_map.get(normalized_uid)
        if existing is not None and existing is not player:
            logger.warning(
                "Skipping uid cache update for player %s because uid %s is already mapped to player %s",
                player.player_id,
                normalized_uid,
                existing.player_id,
            )
            return

        player.player_uid = normalized_uid
        uid_map[normalized_uid] = player

    @staticmethod
    def _apply_profile_updates(
        player: RegisteredPlayer,
        *,
        player_name: Optional[str],
        kingdom: Optional[str],
        castle_level: Optional[str],
        enabled: Optional[bool],
        added_by_user_id: Optional[int],
        overwrite_owner: bool,
    ) -> None:
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

    async def upsert_profile(
        self,
        player_id: str,
        player_uid: Optional[str] = None,
        player_name: Optional[str] = None,
        kingdom: Optional[str] = None,
        castle_level: Optional[str] = None,
        enabled: Optional[bool] = None,
        added_by_user_id: Optional[int] = None,
        overwrite_owner: bool = False,
    ) -> Optional[RegisteredPlayer]:
        player = await self._get(player_id)
        if player is None and player_uid not in (None, "", "0", 0):
            player = await self.get_by_uid(str(player_uid))
            if player is not None and player.player_id != player_id:
                logger.info("Updating uid-cached player %s to canonical fid %s", player.player_id, player_id)
                player.player_id = player_id

        if player:
            await self._assign_uid_if_available(player, player_uid)
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
            player_uid=str(player_uid) if player_uid not in (None, "", "0", 0) else None,
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
        player_uid: Optional[str] = None,
        player_name: Optional[str] = None,
        kingdom: Optional[str] = None,
        castle_level: Optional[str] = None,
        enabled: bool = True,
    ) -> RegisteredPlayer:
        player = await self.upsert_profile(
            player_id=player_id,
            player_uid=player_uid,
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

    async def add_or_update_many(
        self,
        players: list[dict[str, Any]],
        *,
        overwrite_owner: bool = True,
    ) -> list[RegisteredPlayer]:
        """Create or refresh profiles in one round trip.

        A row's ``enabled`` of ``None`` leaves an existing profile's flag unchanged
        (new profiles are created enabled). ``overwrite_owner=False`` keeps the
        original ``added_by_user_id`` of shared profiles.
        """
        if not players:
            return []

        player_ids = [str(player["player_id"]) for player in players]
        player_uids = [
            str(player.get("player_uid"))
            for player in players
            if player.get("player_uid") not in (None, "", "0", 0)
        ]

        query = select(RegisteredPlayer).where(
            or_(
                RegisteredPlayer.player_id.in_(player_ids),
                RegisteredPlayer.player_uid.in_(player_uids),
            )
        )
        result = await self._session.execute(query)
        existing_players = list(result.scalars().all())
        player_id_map = {player.player_id: player for player in existing_players}
        uid_map = {str(player.player_uid): player for player in existing_players if player.player_uid}

        upserted_players: list[RegisteredPlayer] = []
        # Profiles already used by an earlier row of this batch (by object identity).
        claimed: set[int] = set()
        created_count = 0
        updated_count = 0

        for row in players:
            player_id = str(row["player_id"])
            player_uid = row.get("player_uid")
            normalized_uid = str(player_uid) if player_uid not in (None, "", "0", 0) else None
            player = player_id_map.get(player_id)
            if player is None and normalized_uid:
                player = uid_map.get(normalized_uid)
                if player is not None and id(player) in claimed:
                    # Two Governor IDs with one uid in the same batch (upstream rosters
                    # occasionally repeat a uid). Renaming the earlier row's profile would
                    # leave that row's roster entry without a profile (FK violation) and
                    # fail the whole import, so this row gets its own profile, uid-less.
                    logger.warning(
                        "Uid %s is listed for both %s and %s; keeping it on %s",
                        normalized_uid,
                        player.player_id,
                        player_id,
                        player.player_id,
                    )
                    player = None
                    player_uid = normalized_uid = None
                if player is not None and player.player_id != player_id:
                    logger.info("Updating uid-cached player %s to canonical fid %s", player.player_id, player_id)
                    player_id_map.pop(player.player_id, None)
                    player.player_id = player_id
                    player_id_map[player_id] = player

            if player is None:
                player = RegisteredPlayer(
                    player_id=player_id,
                    player_uid=normalized_uid,
                    player_name=row.get("player_name"),
                    kingdom=row.get("kingdom"),
                    castle_level=row.get("castle_level"),
                    enabled=row.get("enabled") is not False,
                    added_by_user_id=row["added_by_user_id"],
                )
                self._session.add(player)
                player_id_map[player_id] = player
                if normalized_uid:
                    uid_map[normalized_uid] = player
                created_count += 1
            else:
                self._assign_uid_from_maps(player, player_uid, uid_map)
                self._apply_profile_updates(
                    player,
                    player_name=row.get("player_name"),
                    kingdom=row.get("kingdom"),
                    castle_level=row.get("castle_level"),
                    enabled=row.get("enabled", True),
                    added_by_user_id=row.get("added_by_user_id"),
                    overwrite_owner=overwrite_owner,
                )
                updated_count += 1

            claimed.add(id(player))
            upserted_players.append(player)

        await self._session.flush()
        logger.info(
            "Bulk upserted %s registered players (%s created, %s updated)",
            len(upserted_players),
            created_count,
            updated_count,
        )
        return upserted_players

    async def list_auto_redeem_targets(self) -> list[RegisteredPlayer]:
        """Every enabled profile: the accounts a gift code is redeemed for."""
        result = await self._session.execute(
            select(RegisteredPlayer).where(RegisteredPlayer.enabled.is_(True)).order_by(RegisteredPlayer.player_id)
        )
        return list(result.scalars().all())

    async def get_many(self, player_ids: Sequence[str]) -> dict[str, RegisteredPlayer]:
        players: dict[str, RegisteredPlayer] = {}
        for chunk in _chunks(_unique(str(player_id) for player_id in player_ids)):
            result = await self._session.execute(
                select(RegisteredPlayer).where(RegisteredPlayer.player_id.in_(chunk))
            )
            players.update({str(player.player_id): player for player in result.scalars().all()})
        return players

    async def list_by_kingdom(self, kingdom: str) -> list[RegisteredPlayer]:
        """The roster of a kingdom: every profile stored with that kingdom."""
        result = await self._session.execute(
            select(RegisteredPlayer)
            .where(RegisteredPlayer.kingdom == str(kingdom))
            .order_by(RegisteredPlayer.player_id)
        )
        return list(result.scalars().all())

    async def list_owned(self, added_by_user_id: int) -> list[RegisteredPlayer]:
        """The profiles one Discord user added."""
        result = await self._session.execute(
            select(RegisteredPlayer)
            .where(RegisteredPlayer.added_by_user_id == added_by_user_id)
            .order_by(RegisteredPlayer.player_id)
        )
        return list(result.scalars().all())

    async def count_owned(self, added_by_user_id: int) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(RegisteredPlayer)
            .where(RegisteredPlayer.added_by_user_id == added_by_user_id)
        )
        return int(result.scalar_one())

    async def set_enabled_many(self, player_ids: Sequence[str], enabled: bool) -> int:
        """Switch these profiles on or off; returns how many changed."""
        changed = 0
        for chunk in _chunks(_unique(str(player_id) for player_id in player_ids)):
            result = await self._session.execute(
                update(RegisteredPlayer)
                .where(RegisteredPlayer.player_id.in_(chunk))
                .where(RegisteredPlayer.enabled.is_(not enabled))
                .values(enabled=enabled)
            )
            changed += int(result.rowcount or 0)
        return changed

    async def set_enabled(self, player_id: str, enabled: bool) -> bool:
        player = await self._get(player_id)
        if player is None:
            return False
        player.enabled = enabled
        await self._session.flush()
        return True

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
        player_uid: Optional[str] = None,
        player_name: Optional[str] = None,
        kingdom: Optional[str] = None,
        castle_level: Optional[str] = None,
        added_by_user_id: Optional[int] = None,
    ) -> bool:
        player = await self.upsert_profile(
            player_id=player_id,
            player_uid=player_uid,
            player_name=player_name,
            kingdom=kingdom,
            castle_level=castle_level,
            added_by_user_id=added_by_user_id,
        )
        if player is None:
            return False

        logger.debug("Refreshed metadata for player %s", player_id)
        return True

    async def sync_metadata_many(self, players: list[dict[str, Any]]) -> int:
        """Bulk-refresh player metadata without changing redemption enabled state."""
        if not players:
            return 0

        player_ids = [str(player["player_id"]) for player in players if player.get("player_id") is not None]
        player_uids = [
            str(player.get("player_uid"))
            for player in players
            if player.get("player_uid") not in (None, "", "0", 0)
        ]

        filters = []
        if player_ids:
            filters.append(RegisteredPlayer.player_id.in_(player_ids))
        if player_uids:
            filters.append(RegisteredPlayer.player_uid.in_(player_uids))

        existing_players: list[RegisteredPlayer] = []
        if filters:
            result = await self._session.execute(select(RegisteredPlayer).where(or_(*filters)))
            existing_players = list(result.scalars().all())

        player_id_map = {player.player_id: player for player in existing_players}
        uid_map = {str(player.player_uid): player for player in existing_players if player.player_uid}
        synced_count = 0

        for row in players:
            player_id = str(row["player_id"])
            player_uid = row.get("player_uid")
            normalized_uid = str(player_uid) if player_uid not in (None, "", "0", 0) else None

            player = player_id_map.get(player_id)
            if player is None and normalized_uid:
                player = uid_map.get(normalized_uid)
                if player is not None and player.player_id != player_id:
                    logger.info("Updating uid-cached player %s to canonical fid %s", player.player_id, player_id)
                    player_id_map.pop(player.player_id, None)
                    player.player_id = player_id
                    player_id_map[player_id] = player

            if player is None:
                added_by_user_id = row.get("added_by_user_id")
                if added_by_user_id is None:
                    continue

                player = RegisteredPlayer(
                    player_id=player_id,
                    player_uid=normalized_uid,
                    player_name=row.get("player_name"),
                    kingdom=row.get("kingdom"),
                    castle_level=row.get("castle_level"),
                    enabled=False,
                    added_by_user_id=added_by_user_id,
                )
                self._session.add(player)
                player_id_map[player_id] = player
                if normalized_uid:
                    uid_map[normalized_uid] = player
            else:
                self._assign_uid_from_maps(player, player_uid, uid_map)
                self._apply_profile_updates(
                    player,
                    player_name=row.get("player_name"),
                    kingdom=row.get("kingdom"),
                    castle_level=row.get("castle_level"),
                    enabled=None,
                    added_by_user_id=row.get("added_by_user_id"),
                    overwrite_owner=False,
                )

            synced_count += 1

        await self._session.flush()
        logger.debug("Bulk-refreshed metadata for %s registered players", synced_count)
        return synced_count


class GiftCodeRepository:
    """Repository for gift code catalog records."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def get(self, code_id: int) -> Optional[GiftCode]:
        result = await self._session.execute(select(GiftCode).where(GiftCode.id == code_id))
        return result.scalar_one_or_none()

    async def get_by_code(self, code: str) -> Optional[GiftCode]:
        result = await self._session.execute(select(GiftCode).where(GiftCode.code == code))
        return result.scalar_one_or_none()

    async def add_or_update(
        self,
        code_id: int,
        code: str,
        created_at_api: datetime,
        expires_at: Optional[datetime] = None,
    ) -> tuple[bool, GiftCode]:
        """Track a code from the upstream list; new codes start ``pending`` auto-redemption."""
        # The code text identifies a code. The upstream list occasionally re-issues a
        # known code under a new ID, and may reuse an ID for a different code.
        existing_code = await self.get_by_code(code)
        if existing_code:
            existing_code.expires_at = expires_at
            await self._session.flush()
            logger.debug("Updated existing gift code %s (ID: %s)", code, code_id)
            return False, existing_code
        if await self.get(code_id) is not None:
            # The ID belongs to another code already: store this one under a local ID
            # (negative, like member-submitted codes) instead of dropping it.
            logger.warning("Upstream gift code ID %s was reused for a new code %s", code_id, code)
            code_id = -int(datetime.now(timezone.utc).timestamp() * 1000)

        new_code = GiftCode(
            id=code_id,
            code=code,
            expires_at=expires_at,
            created_at_api=created_at_api,
            # The column's server default is "completed" (for pre-existing rows), so
            # newly discovered codes must be marked pending explicitly.
            auto_redeem_status=AUTO_REDEEM_PENDING,
        )
        self._session.add(new_code)
        logger.info("Added new tracked gift code: %s (ID: %s)", code, code_id)
        await self._session.flush()
        return True, new_code

    async def add_submitted(self, code: str, submitted_at: datetime | None = None) -> tuple[bool, GiftCode]:
        """Track a code a member submitted before the upstream list published it.

        Submitted codes get a negative ID (the upstream list uses positive ones); when
        the list later publishes the same code, ``add_or_update`` finds it by its text.
        """
        existing_code = await self.get_by_code(code)
        if existing_code is not None:
            return False, existing_code
        submitted_at = submitted_at or datetime.now(timezone.utc)
        new_code = GiftCode(
            id=-int(submitted_at.timestamp() * 1000),
            code=code,
            expires_at=None,
            created_at_api=submitted_at,
            auto_redeem_status=AUTO_REDEEM_PENDING,
        )
        self._session.add(new_code)
        await self._session.flush()
        logger.info("Added member-submitted gift code: %s", code)
        return True, new_code

    async def list_all(self):
        result = await self._session.execute(select(GiftCode).order_by(GiftCode.created_at_api.desc()))
        return list(result.scalars().all())

    async def list_pending(self) -> list[GiftCode]:
        result = await self._session.execute(
            select(GiftCode)
            .where(GiftCode.auto_redeem_status == AUTO_REDEEM_PENDING)
            .order_by(GiftCode.created_at_api, GiftCode.id)
        )
        return list(result.scalars().all())

    async def list_recent(self, limit: int) -> list[GiftCode]:
        result = await self._session.execute(
            select(GiftCode).order_by(GiftCode.created_at_api.desc(), GiftCode.id.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def mark_completed(self, code_id: int, completed_at: datetime | None = None) -> bool:
        result = await self._session.execute(
            update(GiftCode)
            .where(GiftCode.id == code_id)
            .values(
                auto_redeem_status=AUTO_REDEEM_COMPLETED,
                auto_redeem_completed_at=completed_at or datetime.now(timezone.utc),
            )
        )
        return bool(result.rowcount)


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

    async def create_many(self, rows: list[dict[str, Any]]) -> list[GiftCodeRedemption]:
        if not rows:
            return []

        logs = [GiftCodeRedemption(**row) for row in rows]
        self._session.add_all(logs)
        await self._session.flush()
        logger.info("Bulk logged %s gift code redemption attempts", len(logs))
        return logs

    async def find_successful_redemption(self, player_id: int, gift_code: str):
        """Latest successful redemption of ``gift_code`` for a player, if any."""
        result = await self._session.execute(
            select(GiftCodeRedemption)
            .where(GiftCodeRedemption.player_id == str(player_id))
            .where(GiftCodeRedemption.gift_code == gift_code)
            .where(GiftCodeRedemption.success.is_(True))
            .order_by(GiftCodeRedemption.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def recorded_outcomes(self, gift_code: str, final_error_codes: Iterable[str]) -> list[Any]:
        """Rows that settle ``gift_code`` for a player: successes and final failures.

        Returned oldest first as rows with ``player_id``, ``success``, ``error_code``,
        ``response_message`` and ``user_id``.
        """
        codes = sorted(set(final_error_codes))
        result = await self._session.execute(
            select(
                GiftCodeRedemption.player_id,
                GiftCodeRedemption.success,
                GiftCodeRedemption.error_code,
                GiftCodeRedemption.response_message,
                GiftCodeRedemption.user_id,
            )
            .where(GiftCodeRedemption.gift_code == gift_code)
            .where(or_(GiftCodeRedemption.success.is_(True), GiftCodeRedemption.error_code.in_(codes)))
            .order_by(GiftCodeRedemption.created_at, GiftCodeRedemption.id)
        )
        return list(result.all())

    async def rows_for_codes(self, gift_codes: Sequence[str], player_ids: Sequence[str]) -> list[Any]:
        """Attempt rows (``gift_code``, ``player_id``, ``success``, ``error_code``) for a roster."""
        if not gift_codes or not player_ids:
            return []
        rows: list[Any] = []
        for chunk in _chunks(_unique(str(player_id) for player_id in player_ids)):
            result = await self._session.execute(
                select(
                    GiftCodeRedemption.gift_code,
                    GiftCodeRedemption.player_id,
                    GiftCodeRedemption.success,
                    GiftCodeRedemption.error_code,
                )
                .where(GiftCodeRedemption.gift_code.in_(list(gift_codes)))
                .where(GiftCodeRedemption.player_id.in_(chunk))
                .order_by(GiftCodeRedemption.created_at, GiftCodeRedemption.id)
            )
            rows.extend(result.all())
        return rows

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


class GiftCodeAnnouncementRepository:
    """Announcement messages already posted for a gift code, by channel."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def for_code(self, gift_code: str) -> dict[int, int]:
        result = await self._session.execute(
            select(GiftCodeAnnouncement.channel_id, GiftCodeAnnouncement.message_id).where(
                GiftCodeAnnouncement.gift_code == gift_code
            )
        )
        return {int(channel_id): int(message_id) for channel_id, message_id in result.all()}

    async def save(self, gift_code: str, channel_id: int, message_id: int) -> None:
        result = await self._session.execute(
            select(GiftCodeAnnouncement)
            .where(GiftCodeAnnouncement.gift_code == gift_code)
            .where(GiftCodeAnnouncement.channel_id == channel_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            self._session.add(GiftCodeAnnouncement(gift_code=gift_code, channel_id=channel_id, message_id=message_id))
        else:
            row.message_id = message_id
        await self._session.flush()

    async def delete_older_than(self, cutoff: datetime) -> int:
        result = await self._session.execute(
            delete(GiftCodeAnnouncement).where(GiftCodeAnnouncement.created_at < cutoff)
        )
        return int(result.rowcount or 0)


class CommandUsageRepository:
    """Per-day command counters."""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def add(self, usage_date: date, guild_id: int, command_name: str, uses: int) -> None:
        result = await self._session.execute(
            select(CommandUsageDaily)
            .where(CommandUsageDaily.usage_date == usage_date)
            .where(CommandUsageDaily.guild_id == guild_id)
            .where(CommandUsageDaily.command_name == command_name)
        )
        row = result.scalar_one_or_none()
        if row is None:
            self._session.add(
                CommandUsageDaily(usage_date=usage_date, guild_id=guild_id, command_name=command_name, uses=uses)
            )
        else:
            row.uses = int(row.uses or 0) + uses
        await self._session.flush()

    async def totals_since(self, since: date, *, guild_id: int | None = None) -> list[tuple[str, int]]:
        """``(command_name, uses)`` since ``since`` (inclusive), most used first."""
        total = func.sum(CommandUsageDaily.uses)
        query = (
            select(CommandUsageDaily.command_name, total)
            .where(CommandUsageDaily.usage_date >= since)
            .group_by(CommandUsageDaily.command_name)
            .order_by(total.desc(), CommandUsageDaily.command_name)
        )
        if guild_id is not None:
            query = query.where(CommandUsageDaily.guild_id == guild_id)
        result = await self._session.execute(query)
        return [(str(name), int(uses or 0)) for name, uses in result.all()]

    async def delete_older_than(self, cutoff: date) -> int:
        result = await self._session.execute(delete(CommandUsageDaily).where(CommandUsageDaily.usage_date < cutoff))
        return int(result.rowcount or 0)
