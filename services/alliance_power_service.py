"""Daily power observations for explicitly tracked alliances."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from db.models import AlliancePowerSnapshot, AlliancePowerTracking
from services.kingshot_data_service import KingshotDataService

logger = logging.getLogger(__name__)


class AlliancePowerService:
    def __init__(self, db_manager, kingshot_data_service, *, now_provider=None):
        self._db = db_manager
        self._api = kingshot_data_service
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        self._locks = {}

    async def track_alliance(self, guild_id: int, kingdom_id: int, alliance_id: int) -> None:
        if any(KingshotDataService._positive_int(value) is None for value in (guild_id, kingdom_id, alliance_id)):
            raise ValueError("Enter a positive kingdom ID and select an alliance from the suggestions.")
        # Validate and capture before replacing an existing server selection.
        await self._ensure_snapshot(kingdom_id, alliance_id)
        async with self._db.session() as session:
            await session.execute(insert(AlliancePowerTracking).values(
                guild_id=guild_id, kingdom_id=kingdom_id, alliance_id=alliance_id,
            ).on_conflict_do_update(
                index_elements=["guild_id"],
                set_={"kingdom_id": kingdom_id, "alliance_id": alliance_id},
            ))

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
            snapshots = list((await session.execute(select(AlliancePowerSnapshot).where(
                AlliancePowerSnapshot.kingdom_id == tracking.kingdom_id,
                AlliancePowerSnapshot.alliance_id == tracking.alliance_id,
            ).order_by(AlliancePowerSnapshot.snapshot_date.desc()).limit(8))).scalars())
            latest = snapshots[0] if snapshots else None
            by_date = {snapshot.snapshot_date: snapshot for snapshot in snapshots}
            return {
                "kingdom_id": tracking.kingdom_id,
                "alliance_id": tracking.alliance_id,
                "latest": latest,
                "daily": by_date.get(latest.snapshot_date - timedelta(days=1)) if latest else None,
                "weekly": by_date.get(latest.snapshot_date - timedelta(days=7)) if latest else None,
                "history": list(reversed(snapshots[:7])),
            }

    async def collect_due_snapshots(self, guild_ids: set[int]) -> None:
        if not guild_ids:
            return
        async with self._db.session() as session:
            alliances = list((await session.execute(select(
                AlliancePowerTracking.kingdom_id, AlliancePowerTracking.alliance_id,
            ).where(AlliancePowerTracking.guild_id.in_(guild_ids)).distinct())).all())
        for kingdom_id, alliance_id in alliances:
            try:
                await self._ensure_snapshot(kingdom_id, alliance_id)
            except Exception:
                logger.warning("Could not capture power for kingdom %s alliance %s; retrying next hour",
                               kingdom_id, alliance_id, exc_info=True)

    async def _ensure_snapshot(self, kingdom_id: int, alliance_id: int) -> None:
        lock = self._locks.setdefault((kingdom_id, alliance_id), asyncio.Lock())
        async with lock:
            async with self._db.session() as session:
                existing = (await session.execute(select(AlliancePowerSnapshot.snapshot_date).where(
                    AlliancePowerSnapshot.kingdom_id == kingdom_id,
                    AlliancePowerSnapshot.alliance_id == alliance_id,
                    AlliancePowerSnapshot.snapshot_date == self._now().astimezone(timezone.utc).date(),
                ))).scalar_one_or_none()
            if existing is not None:
                return
            try:
                async with asyncio.timeout(120):
                    values = await self._fetch_snapshot(kingdom_id, alliance_id)
            except Exception as error:
                logger.warning("Incomplete power snapshot for kingdom %s alliance %s", kingdom_id, alliance_id,
                               exc_info=True)
                raise ValueError("Could not collect complete member power data. Try again later; existing history is preserved.") from error
            captured_at = self._now().astimezone(timezone.utc)
            async with self._db.session() as session:
                await session.execute(insert(AlliancePowerSnapshot).values(
                    kingdom_id=kingdom_id, alliance_id=alliance_id, snapshot_date=captured_at.date(),
                    captured_at=captured_at, **values,
                ).on_conflict_do_nothing(index_elements=["kingdom_id", "alliance_id", "snapshot_date"]))

    @staticmethod
    def _payload(result) -> dict:
        data = result.get("data") if isinstance(result, dict) else None
        if (not isinstance(result, dict) or not result.get("success") or result.get("partial")
                or not isinstance(data, dict) or data.get("error") or data.get("partial")):
            raise ValueError("Incomplete KingShot API response")
        return data

    async def _fetch_snapshot(self, kingdom_id: int, alliance_id: int) -> dict:
        roster = self._payload(await self._api.get_alliance(alliance_id, kingdom_id))
        number = KingshotDataService._positive_int
        if roster.get("aid") is not None and number(roster["aid"]) != alliance_id:
            raise ValueError("Alliance identity does not match")
        rows = roster.get("members")
        if not isinstance(rows, list) or not rows:
            raise ValueError("No member roster returned")
        uids = [number(row.get("uid")) if isinstance(row, dict) else None for row in rows]
        if None in uids or len(set(uids)) != len(uids):
            raise ValueError("Incomplete member identities")
        if roster.get("count") is not None and number(roster["count"]) != len(uids):
            raise ValueError("Incomplete member roster")

        semaphore = asyncio.Semaphore(5)

        async def fetch(uid):
            async with semaphore:
                return await self._api.get_player(str(uid))

        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(fetch(uid)) for uid in uids]
        members = {}
        name, tag = roster.get("name"), roster.get("abbr")
        for uid, task in zip(uids, tasks):
            profile = self._payload(task.result())
            if number(profile.get("uid")) != uid:
                raise ValueError("Member identity does not match")
            if profile.get("kid") is not None and number(profile["kid"]) != kingdom_id:
                raise ValueError("Member kingdom changed during capture")
            alliance = profile.get("alliance")
            if isinstance(alliance, dict):
                if alliance.get("aid") is not None and number(alliance["aid"]) != alliance_id:
                    raise ValueError("Member alliance changed during capture")
                name = name or alliance.get("name")
                tag = tag or alliance.get("abbr")
            power = KingshotDataService._nonnegative_int(profile.get("power"))
            if power is None or not isinstance(profile.get("power"), (int, str)):
                raise ValueError("Member power unavailable")
            fid = number(profile.get("fid"))
            members[str(uid)] = {
                "name": str(profile.get("name") or f"Player {uid}")[:255],
                "fid": str(fid) if fid is not None else None,
                "power": power,
            }
        return {
            "alliance_name": str(name or f"Alliance {alliance_id}")[:255],
            "alliance_tag": str(tag or "???")[:32],
            "total_power": sum(member["power"] for member in members.values()),
            "members": members,
        }
