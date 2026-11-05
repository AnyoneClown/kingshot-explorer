"""Daily power observations for explicitly tracked alliances."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from db.models import AlliancePowerSnapshot, AlliancePowerTracking
from services.kingshot_data_service import KingshotDataService

logger = logging.getLogger(__name__)

# Snapshots needed to answer every report question (exact daily/weekly baselines,
# the previous complete snapshot, and 7/14-day inactivity windows).
REPORT_HISTORY_DAYS = 14
INACTIVITY_WINDOWS = (7, 14)


@dataclass(frozen=True)
class RosterChanges:
    """Members who joined, left, or were renamed between two complete snapshots."""

    newer_date: date
    older_date: date
    joined: list
    left: list
    renamed: list

    @property
    def gap_days(self) -> int:
        return (self.newer_date - self.older_date).days

    @property
    def is_empty(self) -> bool:
        return not (self.joined or self.left or self.renamed)


@dataclass(frozen=True)
class FlatPowerMember:
    uid: str
    name: str
    power: int
    since: date
    stove_lv: int | None = None


def _fallback_name(uid: str, name) -> bool:
    return not name or name == f"Player {uid}"


def _by_power(pair) -> tuple:
    uid, member = pair
    power = member.get("power")
    return (power is None, -(power or 0), str(member.get("name") or ""), uid)


def roster_changes(newer, older) -> RosterChanges | None:
    """Compare two complete snapshots (any dates; gaps are covered as one period)."""
    if newer is None or older is None:
        return None
    current, previous = dict(newer.members or {}), dict(older.members or {})
    joined = sorted(((uid, current[uid]) for uid in current.keys() - previous.keys()), key=_by_power)
    left = sorted(((uid, previous[uid]) for uid in previous.keys() - current.keys()), key=_by_power)
    renamed = []
    for uid in sorted(current.keys() & previous.keys()):
        old_name, new_name = previous[uid].get("name"), current[uid].get("name")
        # Fallback labels ("Player 123") are not real names, so they are not renames.
        if old_name != new_name and not _fallback_name(uid, old_name) and not _fallback_name(uid, new_name):
            renamed.append((uid, str(old_name), str(new_name)))
    return RosterChanges(
        newer_date=newer.snapshot_date,
        older_date=older.snapshot_date,
        joined=joined,
        left=left,
        renamed=renamed,
    )


def flat_power_members(snapshots, days: int, *, tolerance_days: int = 1) -> list[FlatPowerMember] | None:
    """Members whose power did not change in any snapshot of the last ``days`` days.

    Returns None when saved history does not reach back far enough (allowing
    ``tolerance_days`` for a missed capture at the window edge). Members who
    joined during the window are excluded. Flat power is only a hint of
    inactivity: maxed or saving players also stop growing.
    """
    ordered = sorted(snapshots or (), key=lambda snapshot: snapshot.snapshot_date, reverse=True)
    if not ordered:
        return None
    latest = ordered[0]
    cutoff = latest.snapshot_date - timedelta(days=days)
    window = [snapshot for snapshot in ordered if snapshot.snapshot_date >= cutoff]
    baseline = window[-1]
    if (latest.snapshot_date - baseline.snapshot_date).days < days - tolerance_days:
        return None
    flat = []
    for uid, member in (latest.members or {}).items():
        power = member.get("power")
        if power is None or uid not in (baseline.members or {}):
            continue
        observed = [snapshot.members[uid].get("power") for snapshot in window[1:] if uid in snapshot.members]
        if all(value == power for value in observed):
            flat.append(FlatPowerMember(
                uid=uid,
                name=str(member.get("name") or f"Player {uid}"),
                power=power,
                since=baseline.snapshot_date,
                stove_lv=member.get("stove_lv"),
            ))
    flat.sort(key=lambda item: (-item.power, item.name, item.uid))
    return flat


def _optional_level(value) -> int | None:
    return KingshotDataService._nonnegative_int(value) if isinstance(value, (int, str)) else None


def _rank(value) -> int | None:
    rank = KingshotDataService._positive_int(value) if isinstance(value, (int, str)) else None
    return rank if rank is not None and rank <= 5 else None


class AlliancePowerService:
    FULL_ROSTER_TIMEOUT_SECONDS = 90
    MEMBER_FETCH_TIMEOUT_SECONDS = 120
    MEMBER_FETCH_CONCURRENCY = 5
    TAG_RESOLUTION_TIMEOUT_SECONDS = 20
    TAG_RESOLUTION_LIMIT = 15

    def __init__(self, db_manager, kingshot_data_service, *, now_provider=None):
        self._db = db_manager
        self._api = kingshot_data_service
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        self._locks = {}

    async def track_alliance(self, guild_id: int, kingdom_id: int, alliance_id: int) -> bool:
        """Select a server's alliance. Returns True when a new snapshot was captured."""
        if any(KingshotDataService._positive_int(value) is None for value in (guild_id, kingdom_id, alliance_id)):
            raise ValueError("Enter a positive kingdom ID and select an alliance from the suggestions.")
        # Validate and capture before replacing an existing server selection.
        captured = await self._ensure_snapshot(kingdom_id, alliance_id)
        async with self._db.session() as session:
            await session.execute(insert(AlliancePowerTracking).values(
                guild_id=guild_id, kingdom_id=kingdom_id, alliance_id=alliance_id,
            ).on_conflict_do_update(
                index_elements=["guild_id"],
                set_={"kingdom_id": kingdom_id, "alliance_id": alliance_id},
            ))
        return captured

    async def stop_tracking(self, guild_id: int, kingdom_id: int, alliance_id: int) -> bool:
        async with self._db.session() as session:
            result = await session.execute(delete(AlliancePowerTracking).where(
                AlliancePowerTracking.guild_id == guild_id,
                AlliancePowerTracking.kingdom_id == kingdom_id,
                AlliancePowerTracking.alliance_id == alliance_id,
            ))
            return result.rowcount > 0

    async def get_report(self, guild_id: int) -> dict | None:
        async with self._db.session() as session:
            tracking = (await session.execute(select(AlliancePowerTracking).where(
                AlliancePowerTracking.guild_id == guild_id,
            ))).scalar_one_or_none()
            if tracking is None:
                return None
            return await self._build_report(session, tracking.kingdom_id, tracking.alliance_id)

    async def get_alliance_report(self, kingdom_id: int, alliance_id: int) -> dict:
        async with self._db.session() as session:
            return await self._build_report(session, kingdom_id, alliance_id)

    async def guilds_tracking(self, kingdom_id: int, alliance_id: int) -> list[int]:
        async with self._db.session() as session:
            rows = await session.execute(select(AlliancePowerTracking.guild_id).where(
                AlliancePowerTracking.kingdom_id == kingdom_id,
                AlliancePowerTracking.alliance_id == alliance_id,
            ))
            return sorted(row[0] for row in rows.all())

    async def _build_report(self, session, kingdom_id: int, alliance_id: int) -> dict:
        # One snapshot per UTC day, so this many rows always covers the 14-day window.
        snapshots = list((await session.execute(select(AlliancePowerSnapshot).where(
            AlliancePowerSnapshot.kingdom_id == kingdom_id,
            AlliancePowerSnapshot.alliance_id == alliance_id,
        ).order_by(AlliancePowerSnapshot.snapshot_date.desc()).limit(REPORT_HISTORY_DAYS + 1))).scalars())
        latest = snapshots[0] if snapshots else None
        previous = snapshots[1] if len(snapshots) > 1 else None
        by_date = {snapshot.snapshot_date: snapshot for snapshot in snapshots}
        return {
            "kingdom_id": kingdom_id,
            "alliance_id": alliance_id,
            "latest": latest,
            "previous": previous,
            "daily": by_date.get(latest.snapshot_date - timedelta(days=1)) if latest else None,
            "weekly": by_date.get(latest.snapshot_date - timedelta(days=7)) if latest else None,
            "history": list(reversed(snapshots[:7])),
            "changes": roster_changes(latest, previous),
            "inactive": {days: flat_power_members(snapshots, days) for days in INACTIVITY_WINDOWS},
        }

    async def collect_due_snapshots(self, guild_ids: set[int]) -> list[tuple[int, int]]:
        """Capture missing daily snapshots; returns (kingdom_id, alliance_id) of new ones."""
        if not guild_ids:
            return []
        async with self._db.session() as session:
            alliances = list((await session.execute(select(
                AlliancePowerTracking.kingdom_id, AlliancePowerTracking.alliance_id,
            ).where(AlliancePowerTracking.guild_id.in_(guild_ids)).distinct())).all())
        captured = []
        for kingdom_id, alliance_id in alliances:
            try:
                if await self._ensure_snapshot(kingdom_id, alliance_id):
                    captured.append((kingdom_id, alliance_id))
            except Exception:
                logger.warning("Could not capture power for kingdom %s alliance %s; retrying next hour",
                               kingdom_id, alliance_id, exc_info=True)
        return captured

    async def resolve_alliance_tag(self, kingdom_id: int, tag: str) -> int | None:
        """Resolve an exact alliance tag among the kingdom's top alliances by power."""
        get_ranked = getattr(self._api, "get_ranked_alliances", None)
        needle = str(tag or "").strip().casefold()
        if get_ranked is None or not needle:
            return None
        try:
            async with asyncio.timeout(self.TAG_RESOLUTION_TIMEOUT_SECONDS):
                result = await get_ranked(kingdom_id, self.TAG_RESOLUTION_LIMIT, exhaustive=True)
        except Exception:
            logger.warning("Could not resolve alliance tag %r in kingdom %s", tag, kingdom_id, exc_info=True)
            return None
        data = result.get("data") if isinstance(result, dict) and result.get("success") else None
        matches = {
            KingshotDataService._positive_int(alliance.get("aid"))
            for alliance in data or ()
            if isinstance(alliance, dict) and str(alliance.get("abbr") or "").casefold() == needle
        }
        matches.discard(None)
        return matches.pop() if len(matches) == 1 else None

    async def _ensure_snapshot(self, kingdom_id: int, alliance_id: int) -> bool:
        """Capture today's snapshot if missing. Returns True only for a new snapshot."""
        lock = self._locks.setdefault((kingdom_id, alliance_id), asyncio.Lock())
        async with lock:
            async with self._db.session() as session:
                existing = (await session.execute(select(AlliancePowerSnapshot.snapshot_date).where(
                    AlliancePowerSnapshot.kingdom_id == kingdom_id,
                    AlliancePowerSnapshot.alliance_id == alliance_id,
                    AlliancePowerSnapshot.snapshot_date == self._now().astimezone(timezone.utc).date(),
                ))).scalar_one_or_none()
            if existing is not None:
                return False
            try:
                values = await self._fetch_snapshot(kingdom_id, alliance_id)
            except Exception as error:
                logger.warning("Incomplete power snapshot for kingdom %s alliance %s", kingdom_id, alliance_id,
                               exc_info=True)
                raise ValueError("Could not collect complete member power data. Try again later; existing history is preserved.") from error
            captured_at = self._now().astimezone(timezone.utc)
            async with self._db.session() as session:
                inserted = (await session.execute(insert(AlliancePowerSnapshot).values(
                    kingdom_id=kingdom_id, alliance_id=alliance_id, snapshot_date=captured_at.date(),
                    captured_at=captured_at, **values,
                ).on_conflict_do_nothing(
                    index_elements=["kingdom_id", "alliance_id", "snapshot_date"],
                ).returning(AlliancePowerSnapshot.snapshot_date))).scalar_one_or_none()
            return inserted is not None

    @staticmethod
    def _payload(result) -> dict:
        data = result.get("data") if isinstance(result, dict) else None
        if (not isinstance(result, dict) or not result.get("success") or result.get("partial")
                or not isinstance(data, dict) or data.get("error") or data.get("partial")):
            raise ValueError("Incomplete KingShot API response")
        return data

    def _roster(self, payload: dict, alliance_id: int) -> tuple[dict, list, list[int]]:
        number = KingshotDataService._positive_int
        if payload.get("aid") is not None and number(payload["aid"]) != alliance_id:
            raise ValueError("Alliance identity does not match")
        rows = payload.get("members")
        if not isinstance(rows, list) or not rows:
            raise ValueError("No member roster returned")
        uids = [number(row.get("uid")) if isinstance(row, dict) else None for row in rows]
        if None in uids or len(set(uids)) != len(uids):
            raise ValueError("Incomplete member identities")
        if payload.get("count") is not None and number(payload["count"]) != len(uids):
            raise ValueError("Incomplete member roster")
        return payload, rows, uids

    async def _load_roster(self, kingdom_id: int, alliance_id: int):
        """Prefer /full (one paced call); fall back to the lean roster."""
        get_full = getattr(self._api, "get_alliance_full", None)
        if get_full is not None:
            try:
                async with asyncio.timeout(self.FULL_ROSTER_TIMEOUT_SECONDS):
                    return self._roster(self._payload(await get_full(alliance_id, kingdom_id)), alliance_id)
            except Exception:
                logger.info("Full roster unavailable for kingdom %s alliance %s; using member profiles",
                            kingdom_id, alliance_id, exc_info=True)
        return self._roster(self._payload(await self._api.get_alliance(alliance_id, kingdom_id)), alliance_id)

    @staticmethod
    def _embedded_profile(row: dict) -> dict | None:
        if row.get("error"):
            return None
        player = row.get("player")
        if isinstance(player, dict):
            return player
        # Tolerate /full rows that inline the profile next to uid/rank.
        return row if "power" in row else None

    def _member_entry(self, uid: int, profile, rank, kingdom_id: int, alliance_id: int) -> dict:
        number = KingshotDataService._positive_int
        if not isinstance(profile, dict) or number(profile.get("uid")) != uid:
            raise ValueError("Member identity does not match")
        if profile.get("kid") is not None and number(profile["kid"]) != kingdom_id:
            raise ValueError("Member kingdom changed during capture")
        alliance = profile.get("alliance")
        if isinstance(alliance, dict) and alliance.get("aid") is not None and number(alliance["aid"]) != alliance_id:
            raise ValueError("Member alliance changed during capture")
        power = KingshotDataService._nonnegative_int(profile.get("power"))
        if power is None or not isinstance(profile.get("power"), (int, str)):
            raise ValueError("Member power unavailable")
        fid = number(profile.get("fid"))
        return {
            "name": str(profile.get("name") or f"Player {uid}")[:255],
            "fid": str(fid) if fid is not None else None,
            "power": power,
            "stove_lv": _optional_level(profile.get("stove_lv")),
            "vip": _optional_level(profile.get("vip")),
            "life_tree_level": _optional_level(profile.get("life_tree_level")),
            "rank": rank,
        }

    async def _fetch_snapshot(self, kingdom_id: int, alliance_id: int) -> dict:
        roster, rows, uids = await self._load_roster(kingdom_id, alliance_id)
        name, tag = roster.get("name"), roster.get("abbr")
        entries, profiles, pending = {}, {}, []
        for uid, row in zip(uids, rows):
            rank = _rank(row.get("rank"))
            profile = self._embedded_profile(row)
            if profile is not None:
                try:
                    entries[uid] = self._member_entry(uid, profile, rank, kingdom_id, alliance_id)
                    profiles[uid] = profile
                    continue
                except ValueError:
                    logger.debug("Refetching member %s of alliance %s individually", uid, alliance_id)
            pending.append((uid, rank))

        if pending:
            semaphore = asyncio.Semaphore(self.MEMBER_FETCH_CONCURRENCY)

            async def fetch(uid):
                async with semaphore:
                    return await self._api.get_player(str(uid))

            async with asyncio.timeout(self.MEMBER_FETCH_TIMEOUT_SECONDS):
                async with asyncio.TaskGroup() as group:
                    tasks = [group.create_task(fetch(uid)) for uid, _ in pending]
            for (uid, rank), task in zip(pending, tasks):
                profile = self._payload(task.result())
                # All-or-nothing: one unusable member rejects the whole capture.
                entries[uid] = self._member_entry(uid, profile, rank, kingdom_id, alliance_id)
                profiles[uid] = profile

        for uid in uids:
            alliance = profiles[uid].get("alliance")
            if isinstance(alliance, dict):
                name = name or alliance.get("name")
                tag = tag or alliance.get("abbr")
        members = {str(uid): entries[uid] for uid in uids}
        return {
            "alliance_name": str(name or f"Alliance {alliance_id}")[:255],
            "alliance_tag": str(tag or "???")[:32],
            "total_power": sum(member["power"] for member in members.values()),
            "members": members,
        }
