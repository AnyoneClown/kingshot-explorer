"""Optional Governor-ID roster source for alliance imports."""

from __future__ import annotations

from typing import Any

import httpx


class MightPulseUnavailableError(RuntimeError):
    """A trusted alliance roster could not be read from MightPulse."""


class MightPulseAllianceDirectory:
    """Read named kingdom alliances from MightPulse's public kingdom view."""

    def __init__(
        self,
        *,
        base_url: str = "https://mightpulse.com",
        timeout_seconds: float = 2.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def get_ranked_alliances(self, kid: int, limit: int = 15) -> dict[str, Any]:
        """Return named alliances, verifying every AID belongs to the requested kingdom."""
        if isinstance(kid, bool) or kid <= 0:
            raise ValueError("Kingdom ID must be positive")
        requested_limit = max(1, min(int(limit), 100))
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
                headers={"Accept": "application/json"},
            ) as client:
                response = await client.get(
                    f"/api/kingdoms/{kid}",
                    params={"players": "1", "alliances": "100"},
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MightPulseUnavailableError("Public alliance directory request failed") from exc

        if (
            not isinstance(payload, dict)
            or payload.get("ok") is not True
            or str(payload.get("kid")) != str(kid)
            or not isinstance(payload.get("alliances"), list)
        ):
            raise MightPulseUnavailableError("Public alliance directory returned invalid kingdom data")

        rows_by_aid: dict[str, dict[str, Any]] = {}
        for position, row in enumerate(payload["alliances"], start=1):
            if not isinstance(row, dict) or str(row.get("kid")) != str(kid):
                continue
            aid = MightPulseService._positive_id(row.get("aid"))
            name = row.get("name")
            if aid is None or not isinstance(name, str) or not name.strip():
                continue
            rank = MightPulseService._positive_id(row.get("rank"))
            power = row.get("power")
            members = row.get("member_count")
            tag = row.get("abbr")
            candidate = {
                "aid": aid,
                "abbr": tag if MightPulseService._valid_tag(tag) else None,
                "name": name.strip(),
                "rank": int(rank) if rank is not None else position,
                "power": power if isinstance(power, int) and not isinstance(power, bool) and power >= 0 else None,
                "member_count": members if isinstance(members, int) and not isinstance(members, bool) and members >= 0 else None,
            }
            previous = rows_by_aid.get(aid)
            if previous is None or candidate["rank"] < previous["rank"]:
                rows_by_aid[aid] = candidate

        if not rows_by_aid:
            raise MightPulseUnavailableError("Public alliance directory returned no named alliances")
        alliances = sorted(
            rows_by_aid.values(),
            key=lambda row: (row["rank"], -(row["power"] or 0), int(row["aid"])),
        )[:requested_limit]
        return {"success": True, "data": alliances, "resolution_complete": True}


class MightPulseService:
    """Find an alliance by AID and return its Governor-ID-bearing roster."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.mightpulse.com",
        timeout_seconds: float = 95.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def get_alliance_roster_by_aid(self, kid: int, aid: str | int) -> dict[str, Any]:
        """Match the AID in kingdom ranks, then fetch and verify its tagged roster."""
        target_aid = str(int(aid))
        async with httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            headers={"X-Api-Key": self._api_key, "Accept": "application/json"},
        ) as client:
            ranks = await self._get_json(
                client,
                f"/v1/kingdoms/{kid}/ranks",
                params={"board": "alliance_power", "limit": "100"},
            )
            match = next(
                (
                    row for row in self._alliance_rows(ranks)
                    if str(row.get("aid")) == target_aid and self._valid_tag(row.get("abbr"))
                ),
                None,
            )
            if match is None:
                raise MightPulseUnavailableError("Alliance AID is absent from MightPulse kingdom ranks")

            tag = match["abbr"]
            roster = await self._get_json(
                client,
                f"/v1/alliances/{kid}/{tag}",
                params={"include": "info,roster"},
            )

        payload = roster.get("data", roster) if isinstance(roster, dict) else roster
        if not isinstance(payload, dict):
            raise MightPulseUnavailableError("MightPulse returned an invalid alliance roster")
        alliance = payload.get("alliance")
        members = payload.get("members")
        if not isinstance(alliance, dict) or not isinstance(members, list):
            raise MightPulseUnavailableError("MightPulse returned an incomplete alliance roster")
        if str(alliance.get("aid")) != target_aid or str(alliance.get("kid")) != str(kid):
            raise MightPulseUnavailableError("MightPulse roster identity does not match the selected alliance")

        by_uid = {}
        ambiguous_uids = set()
        for member in members:
            if not isinstance(member, dict):
                continue
            uid = self._positive_id(member.get("uid"))
            governor_id = self._positive_id(member.get("governor_id"))
            fid = self._positive_id(member.get("fid"))
            if governor_id and fid and governor_id != fid:
                continue
            resolved_fid = governor_id or fid
            if uid and resolved_fid and (member.get("kid") is None or str(member["kid"]) == str(kid)):
                if uid in by_uid and by_uid[uid]["fid"] != resolved_fid:
                    by_uid.pop(uid)
                    ambiguous_uids.add(uid)
                elif uid not in ambiguous_uids:
                    by_uid[uid] = {**member, "fid": resolved_fid}
        return {
            "abbr": tag,
            "name": alliance.get("name") or match.get("name"),
            "members_by_uid": by_uid,
        }

    async def _get_json(
        self,
        client: httpx.AsyncClient,
        path: str,
        *,
        params: dict[str, str],
    ) -> Any:
        try:
            response = await client.get(path, params=params)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MightPulseUnavailableError("MightPulse API request failed") from exc
        if isinstance(payload, dict) and payload.get("ok") is False:
            raise MightPulseUnavailableError("MightPulse API rejected the request")
        return payload

    @classmethod
    def _alliance_rows(cls, payload: Any) -> list[dict[str, Any]]:
        """Accept direct rank lists and common board envelopes."""
        if isinstance(payload, list):
            return [row for item in payload for row in cls._alliance_rows(item)]
        if not isinstance(payload, dict):
            return []
        if "aid" in payload:
            return [payload]
        return [
            row
            for key in ("entries", "ranks", "boards", "data", "alliance_power")
            for row in cls._alliance_rows(payload.get(key))
        ]

    @staticmethod
    def _valid_tag(value: Any) -> bool:
        return isinstance(value, str) and len(value) == 3 and value.isalnum()

    @staticmethod
    def _positive_id(value: Any) -> str | None:
        if isinstance(value, bool):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return str(parsed) if parsed > 0 else None
