"""Player registry service: the gift-code list.

The list is shared by every server. One row of ``registered_players`` is one game
account; a gift code is redeemed for every enabled account, whichever server it was
added from. A server's roster is a view of that list: the accounts of the kingdom
chosen in the server's settings, whoever added them.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, List, Optional

from db.session import DatabaseManager, get_db
from repositories.discord_repositories import RegisteredPlayerRepository
from services.transactions import run_db_operation

logger = logging.getLogger(__name__)


def kingdom_key(kingdom: Any) -> Optional[str]:
    """A kingdom as profiles store it (``"795"``), or None when there is none."""
    if kingdom is None or isinstance(kingdom, bool):
        return None
    text = str(kingdom).strip()
    return text or None


@dataclass
class RosterEntry:
    """One account of the gift-code list."""

    player_id: str
    player_name: Optional[str] = None
    player_uid: Optional[str] = None
    kingdom: Optional[str] = None
    castle_level: Optional[str] = None
    enabled: bool = True
    added_by_user_id: Optional[int] = None

    @classmethod
    def from_profile(cls, player) -> "RosterEntry":
        return cls(
            player_id=str(player.player_id),
            player_name=player.player_name,
            player_uid=player.player_uid,
            kingdom=player.kingdom,
            castle_level=player.castle_level,
            enabled=bool(player.enabled),
            added_by_user_id=player.added_by_user_id,
        )


@dataclass
class RegistrationOutcome:
    """Result of adding players to the list."""

    added: list[str] = field(default_factory=list)
    already_registered: list[str] = field(default_factory=list)
    reenabled: list[str] = field(default_factory=list)
    # Not added: the member's own limit of self-registered accounts was reached.
    over_limit: list[str] = field(default_factory=list)


@dataclass
class RemovalOutcome:
    removed: bool
    entry: Optional[RosterEntry] = None


def order_redeem_targets(targets: list, self_registered_max: int) -> list:
    """Members' own accounts first, then accounts added in bulk.

    An account counts as self-registered when its owner added at most
    ``self_registered_max`` of the targets (a manager's alliance import is far above
    that). A code with a claim limit therefore reaches the people who enrolled
    themselves before bulk imports.
    """
    owned_counts: dict[Any, int] = {}
    for player in targets:
        owner = getattr(player, "added_by_user_id", None)
        owned_counts[owner] = owned_counts.get(owner, 0) + 1

    def tier(player) -> int:
        return 0 if owned_counts[getattr(player, "added_by_user_id", None)] <= self_registered_max else 1

    return sorted(targets, key=lambda player: (tier(player), str(player.player_id)))


class PlayerRegistryService:
    """Service managing the shared gift-code list and its kingdom rosters."""

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
        """Every profile of the list (operator/diagnostic use)."""
        async with self._session(session) as db_session:
            return await RegisteredPlayerRepository(db_session).list(enabled_only=enabled_only)

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

    async def get_auto_redeem_targets(
        self, session: Optional[Any] = None, *, self_registered_max: int | None = None
    ) -> list:
        """Every enabled account: what a gift code is redeemed for, automatically or by hand.

        With ``self_registered_max`` the run is ordered so members' own accounts go
        first (see ``order_redeem_targets``).
        """
        async with self._session(session) as db_session:
            targets = await RegisteredPlayerRepository(db_session).list_auto_redeem_targets()
        if self_registered_max is None or self_registered_max <= 0:
            return targets
        return order_redeem_targets(targets, self_registered_max)

    # ------------------------------------------------------------------- rosters

    async def list_kingdom_roster(self, kingdom: Any, session: Optional[Any] = None) -> list[RosterEntry]:
        """The accounts of one kingdom, whoever added them."""
        key = kingdom_key(kingdom)
        if key is None:
            return []
        async with self._session(session) as db_session:
            players = await RegisteredPlayerRepository(db_session).list_by_kingdom(key)
        return [RosterEntry.from_profile(player) for player in players]

    async def list_owned_entries(self, owner_id: int, session: Optional[Any] = None) -> list[RosterEntry]:
        """The accounts one member added."""
        async with self._session(session) as db_session:
            players = await RegisteredPlayerRepository(db_session).list_owned(owner_id)
        return [RosterEntry.from_profile(player) for player in players]

    async def count_owned(self, owner_id: int, session: Optional[Any] = None) -> int:
        async with self._session(session) as db_session:
            return await RegisteredPlayerRepository(db_session).count_owned(owner_id)

    async def get_entry(self, player_id: str, session: Optional[Any] = None) -> Optional[RosterEntry]:
        async with self._session(session) as db_session:
            player = await RegisteredPlayerRepository(db_session).get(str(player_id))
        return RosterEntry.from_profile(player) if player is not None else None

    async def get_entries(self, player_ids: list[str], session: Optional[Any] = None) -> dict[str, RosterEntry]:
        """The listed accounts among ``player_ids``, by Governor ID."""
        async with self._session(session) as db_session:
            players = await RegisteredPlayerRepository(db_session).get_many(player_ids)
        return {player_id: RosterEntry.from_profile(player) for player_id, player in players.items()}

    async def set_players_enabled(
        self, player_ids: list[str], enabled: bool, session: Optional[Any] = None
    ) -> int:
        """Switch redemption on or off for these accounts; returns how many changed."""
        if not player_ids:
            return 0

        async def operation(db_session) -> int:
            return await RegisteredPlayerRepository(db_session).set_enabled_many(player_ids, enabled)

        return await self._run(operation, session=session)

    async def disable_broken_players(self, player_ids: list[str], session: Optional[Any] = None) -> int:
        """Stop redeeming for accounts that keep failing as invalid.

        A manager of the account's kingdom, or its owner, can switch it back on in
        ``/gift roster`` once the Governor ID is fixed. Returns how many were disabled.
        """
        return await self.set_players_enabled(player_ids, False, session=session)

    async def register_players(
        self,
        profiles: list[dict[str, Any]],
        *,
        added_by_user_id: int,
        max_owned: int | None = None,
        enable_existing: bool = False,
        session: Optional[Any] = None,
    ) -> RegistrationOutcome:
        """Add accounts to the list, within the member's own limit when there is one.

        ``profiles`` rows carry ``player_id`` plus optional ``player_uid``,
        ``player_name``, ``kingdom`` and ``castle_level``. Accounts already on the list
        keep their owner and are refreshed (``enable_existing`` switches disabled ones
        back on). ``max_owned`` caps how many accounts ``added_by_user_id`` may own; it
        is counted and the rows inserted in one SERIALIZABLE transaction, so two
        concurrent additions cannot overshoot it together. A caller-supplied
        ``session`` keeps its own isolation level.
        """
        unique_profiles: dict[str, dict[str, Any]] = {}
        for profile in profiles:
            player_id = str(profile.get("player_id") or "").strip()
            if player_id and player_id not in unique_profiles:
                unique_profiles[player_id] = {**profile, "player_id": player_id}

        async def operation(db_session) -> RegistrationOutcome:
            players = RegisteredPlayerRepository(db_session)
            outcome = RegistrationOutcome()
            existing = await players.get_many(list(unique_profiles))
            capacity: int | None = None
            if max_owned is not None:
                capacity = max(0, max_owned - await players.count_owned(added_by_user_id))

            accepted: list[dict[str, Any]] = []
            refresh_only: list[dict[str, Any]] = []
            for player_id, profile in unique_profiles.items():
                listed = existing.get(player_id)
                if listed is not None:
                    outcome.already_registered.append(player_id)
                    if enable_existing and not listed.enabled:
                        outcome.reenabled.append(player_id)
                    refresh_only.append(profile)
                    continue
                if capacity is not None and len(accepted) >= capacity:
                    outcome.over_limit.append(player_id)
                    continue
                accepted.append(profile)

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
            outcome.added = [profile["player_id"] for profile in accepted]
            await db_session.flush()
            return outcome

        outcome = await self._run(
            operation,
            session=session,
            retry_on_integrity_error=True,
            isolation_level="SERIALIZABLE",
        )
        logger.info(
            "Gift-code list: %s added, %s already listed, %s over the personal limit (by user %s)",
            len(outcome.added),
            len(outcome.already_registered),
            len(outcome.over_limit),
            added_by_user_id,
        )
        return outcome

    async def remove_player(self, player_id: str, session: Optional[Any] = None) -> RemovalOutcome:
        """Take one account off the list."""

        async def operation(db_session) -> RemovalOutcome:
            players = RegisteredPlayerRepository(db_session)
            player = await players.get(str(player_id))
            if player is None:
                return RemovalOutcome(removed=False)
            entry = RosterEntry.from_profile(player)
            return RemovalOutcome(removed=await players.remove(entry.player_id), entry=entry)

        return await self._run(operation, session=session)
