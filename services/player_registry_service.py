"""Player registry service for gift code redemption targets.

Player profiles (``registered_players``) are shared by every server: one in-game
account is redeemed once per code no matter how many servers list it. Each
server's roster lives in ``guild_player_registrations`` with its own enabled flag
and owner. Profiles without any registration are legacy rows from before
per-server rosters; enabled legacy profiles keep receiving auto-redemption until a
bot operator assigns them to a server.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, List, Optional

from db.session import DatabaseManager, get_db
from repositories.discord_repositories import (
    GuildPlayerRegistrationRepository,
    RegisteredPlayerRepository,
)
from services.transactions import run_db_operation

logger = logging.getLogger(__name__)

# Upper bound for the operator's "Unassigned (legacy)" list.
MAX_UNASSIGNED_LISTED = 2000


@dataclass
class RosterEntry:
    """One player as seen from a server roster (or the unassigned legacy list)."""

    player_id: str
    player_name: Optional[str] = None
    player_uid: Optional[str] = None
    kingdom: Optional[str] = None
    castle_level: Optional[str] = None
    # Per-server flag for roster entries; the global profile flag for unassigned ones.
    enabled: bool = True
    # Global profile flag (bot operators can pause a profile everywhere).
    profile_enabled: bool = True
    added_by_user_id: Optional[int] = None
    guild_id: Optional[int] = None

    @property
    def unassigned(self) -> bool:
        return self.guild_id is None

    @classmethod
    def from_rows(cls, registration, player) -> "RosterEntry":
        return cls(
            player_id=str(player.player_id),
            player_name=player.player_name,
            player_uid=player.player_uid,
            kingdom=player.kingdom,
            castle_level=player.castle_level,
            enabled=bool(registration.enabled),
            profile_enabled=bool(player.enabled),
            added_by_user_id=registration.added_by_user_id,
            guild_id=registration.guild_id,
        )

    @classmethod
    def unassigned_from_profile(cls, player) -> "RosterEntry":
        return cls(
            player_id=str(player.player_id),
            player_name=player.player_name,
            player_uid=player.player_uid,
            kingdom=player.kingdom,
            castle_level=player.castle_level,
            enabled=bool(player.enabled),
            profile_enabled=bool(player.enabled),
            added_by_user_id=player.added_by_user_id,
            guild_id=None,
        )


@dataclass
class RegistrationOutcome:
    """Result of adding players to one server roster."""

    added: list[str] = field(default_factory=list)
    already_registered: list[str] = field(default_factory=list)
    reenabled: list[str] = field(default_factory=list)
    over_limit: list[str] = field(default_factory=list)
    # "server" when the server cap stopped additions, "personal" for the self-registration cap.
    limit_kind: Optional[str] = None
    roster_size: int = 0


@dataclass
class RemovalOutcome:
    removed: bool
    profile_deleted: bool = False
    entry: Optional[RosterEntry] = None


class PlayerRegistryService:
    """Service managing gift-code registered players and per-server rosters."""

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

    async def _run(
        self,
        operation,
        *,
        session=None,
        retry_on_integrity_error: bool = False,
        isolation_level: str | None = None,
    ):
        if session is not None:
            return await operation(session)
        db = await self._get_db_manager()
        return await run_db_operation(
            db,
            operation,
            retry_on_integrity_error=retry_on_integrity_error,
            isolation_level=isolation_level,
        )

    # ------------------------------------------------------------------ profiles

    async def get_registered_player(self, player_id: str, session: Optional[Any] = None):
        async with self._session(session) as db_session:
            return await RegisteredPlayerRepository(db_session).get(player_id)

    async def get_registered_player_by_uid(self, player_uid: str, session: Optional[Any] = None):
        async with self._session(session) as db_session:
            return await RegisteredPlayerRepository(db_session).get_by_uid(player_uid)

    async def get_registered_players_by_uids(self, player_uids: list[str], session: Optional[Any] = None):
        async with self._session(session) as db_session:
            return await RegisteredPlayerRepository(db_session).list_by_uids(player_uids)

    async def get_registered_players(self, enabled_only: bool = True, session: Optional[Any] = None) -> List:
        """Every profile regardless of server (operator/diagnostic use)."""
        async with self._session(session) as db_session:
            return await RegisteredPlayerRepository(db_session).list(enabled_only=enabled_only)

    async def set_profile_enabled(self, player_id: str, enabled: bool, session: Optional[Any] = None) -> bool:
        """Pause or resume a profile in every server (bot operators only)."""

        async def operation(db_session):
            return await RegisteredPlayerRepository(db_session).set_enabled(player_id, enabled)

        return await self._run(operation, session=session)

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

    # --------------------------------------------------------- redemption targets

    async def get_auto_redeem_targets(self, session: Optional[Any] = None) -> list:
        """Enabled profiles enabled in ≥1 roster, plus enabled unassigned legacy profiles."""
        async with self._session(session) as db_session:
            return await RegisteredPlayerRepository(db_session).list_auto_redeem_targets()

    async def get_guild_redeem_targets(self, guild_id: int, session: Optional[Any] = None) -> list:
        """Profiles enabled in this server's roster (and not paused globally)."""
        async with self._session(session) as db_session:
            return await GuildPlayerRegistrationRepository(db_session).list_enabled_targets(guild_id)

    async def get_enabled_guilds_for_players(
        self, player_ids: list[str], session: Optional[Any] = None
    ) -> dict[str, set[int]]:
        async with self._session(session) as db_session:
            return await GuildPlayerRegistrationRepository(db_session).enabled_guilds_by_player(player_ids)

    # ------------------------------------------------------------------- rosters

    async def list_guild_roster(self, guild_id: int, session: Optional[Any] = None) -> list[RosterEntry]:
        async with self._session(session) as db_session:
            rows = await GuildPlayerRegistrationRepository(db_session).list_roster(guild_id)
        return [RosterEntry.from_rows(registration, player) for registration, player in rows]

    async def list_unassigned_players(self, session: Optional[Any] = None) -> list[RosterEntry]:
        async with self._session(session) as db_session:
            players = await RegisteredPlayerRepository(db_session).list_unassigned(limit=MAX_UNASSIGNED_LISTED)
        return [RosterEntry.unassigned_from_profile(player) for player in players]

    async def get_guild_registration(
        self, guild_id: int, player_id: str, session: Optional[Any] = None
    ) -> Optional[RosterEntry]:
        async with self._session(session) as db_session:
            row = await GuildPlayerRegistrationRepository(db_session).get_with_profile(guild_id, player_id)
        return RosterEntry.from_rows(*row) if row is not None else None

    async def get_guild_registrations(
        self, guild_id: int, player_ids: list[str], session: Optional[Any] = None
    ) -> dict[str, Any]:
        async with self._session(session) as db_session:
            return await GuildPlayerRegistrationRepository(db_session).get_many(guild_id, player_ids)

    async def count_guild_registrations(
        self, guild_id: int, *, owner_id: int | None = None, session: Optional[Any] = None
    ) -> tuple[int, int]:
        """Return (players in this roster, players in this roster added by ``owner_id``)."""
        async with self._session(session) as db_session:
            repo = GuildPlayerRegistrationRepository(db_session)
            total = await repo.count(guild_id)
            owned = await repo.count(guild_id, added_by_user_id=owner_id) if owner_id is not None else 0
        return total, owned

    async def register_players_for_guild(
        self,
        guild_id: int,
        profiles: list[dict[str, Any]],
        *,
        added_by_user_id: int,
        max_total: int,
        max_owned: int | None = None,
        enable_existing: bool = False,
        session: Optional[Any] = None,
    ) -> RegistrationOutcome:
        """Add players to a server roster within the server cap (and personal cap).

        ``profiles`` rows carry ``player_id`` plus optional ``player_uid``,
        ``player_name``, ``kingdom`` and ``castle_level``. Shared profiles are
        created or refreshed; players already in this roster are left as they are
        (``enable_existing`` re-enables disabled ones). The caps are counted and the
        rows inserted in one SERIALIZABLE transaction: two concurrent imports that read
        the same count conflict, and the retried one sees the other's rows, so they
        cannot overshoot the caps together. A caller-supplied ``session`` keeps its
        own isolation level.
        """
        unique_profiles: dict[str, dict[str, Any]] = {}
        for profile in profiles:
            player_id = str(profile.get("player_id") or "").strip()
            if player_id and player_id not in unique_profiles:
                unique_profiles[player_id] = {**profile, "player_id": player_id}

        async def operation(db_session) -> RegistrationOutcome:
            registrations = GuildPlayerRegistrationRepository(db_session)
            players = RegisteredPlayerRepository(db_session)
            outcome = RegistrationOutcome()
            existing = await registrations.get_many(guild_id, list(unique_profiles))
            total = await registrations.count(guild_id)
            capacity = max_total - total
            limit_kind = "server"
            if max_owned is not None:
                owned = await registrations.count(guild_id, added_by_user_id=added_by_user_id)
                if max_owned - owned < capacity:
                    capacity, limit_kind = max_owned - owned, "personal"
            capacity = max(0, capacity)

            accepted: list[dict[str, Any]] = []
            refresh_only: list[dict[str, Any]] = []
            for player_id, profile in unique_profiles.items():
                registration = existing.get(player_id)
                if registration is not None:
                    outcome.already_registered.append(player_id)
                    if enable_existing and not registration.enabled:
                        registration.enabled = True
                        outcome.reenabled.append(player_id)
                    refresh_only.append(profile)
                    continue
                if len(accepted) >= capacity:
                    outcome.over_limit.append(player_id)
                    continue
                accepted.append(profile)

            if outcome.over_limit:
                outcome.limit_kind = limit_kind

            rows = [
                {**profile, "added_by_user_id": added_by_user_id, "enabled": True}
                for profile in accepted
            ] + [
                {
                    **profile,
                    "added_by_user_id": added_by_user_id,
                    "enabled": True if profile["player_id"] in outcome.reenabled else None,
                }
                for profile in refresh_only
            ]
            if rows:
                await players.add_or_update_many(rows, overwrite_owner=False)
            for profile in accepted:
                registrations.add(guild_id, profile["player_id"], added_by_user_id)
                outcome.added.append(profile["player_id"])
            await db_session.flush()
            outcome.roster_size = total + len(outcome.added)
            return outcome

        outcome = await self._run(
            operation,
            session=session,
            retry_on_integrity_error=True,
            isolation_level="SERIALIZABLE",
        )
        logger.info(
            "Guild %s roster: %s added, %s already registered, %s over the %s limit (by user %s)",
            guild_id,
            len(outcome.added),
            len(outcome.already_registered),
            len(outcome.over_limit),
            outcome.limit_kind or "-",
            added_by_user_id,
        )
        return outcome

    async def remove_player_from_guild(
        self, guild_id: int, player_id: str, session: Optional[Any] = None
    ) -> RemovalOutcome:
        """Remove one roster entry; delete the shared profile when no roster lists it."""

        async def operation(db_session) -> RemovalOutcome:
            registrations = GuildPlayerRegistrationRepository(db_session)
            row = await registrations.get_with_profile(guild_id, player_id)
            if row is None:
                return RemovalOutcome(removed=False)
            registration, profile = row
            entry = RosterEntry.from_rows(registration, profile)
            await registrations.delete(registration)
            profile_deleted = False
            if await registrations.count_for_player(entry.player_id) == 0:
                # Without this the profile would become an "unassigned legacy" player
                # and keep receiving auto-redemption.
                profile_deleted = await RegisteredPlayerRepository(db_session).remove(entry.player_id)
            return RemovalOutcome(removed=True, profile_deleted=profile_deleted, entry=entry)

        return await self._run(operation, session=session)

    async def set_guild_players_enabled(
        self, guild_id: int, player_ids: list[str], enabled: bool, session: Optional[Any] = None
    ) -> int:
        async def operation(db_session) -> int:
            return await GuildPlayerRegistrationRepository(db_session).set_enabled(guild_id, player_ids, enabled)

        return await self._run(operation, session=session)

    async def assign_unassigned_to_guild(
        self, guild_id: int, *, max_total: int, session: Optional[Any] = None
    ) -> tuple[int, int]:
        """Register enabled legacy profiles to a server, within the server cap.

        Original owners are kept, so they can still manage their entries. Returns
        (assigned, enabled legacy profiles still unassigned).
        """

        async def operation(db_session) -> tuple[int, int]:
            registrations = GuildPlayerRegistrationRepository(db_session)
            players = RegisteredPlayerRepository(db_session)
            unassigned = await players.list_unassigned(enabled_only=True)
            capacity = max(0, max_total - await registrations.count(guild_id))
            selected = unassigned[:capacity]
            for player in selected:
                registrations.add(guild_id, player.player_id, player.added_by_user_id)
            await db_session.flush()
            return len(selected), len(unassigned) - len(selected)

        return await self._run(operation, session=session, retry_on_integrity_error=True)
