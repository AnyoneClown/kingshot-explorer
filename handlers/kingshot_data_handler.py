"""Discord slash commands for KingShot read-only data API interactions."""

from __future__ import annotations

import logging
import json
from typing import Any, Callable, Dict

import discord
from discord import app_commands
from discord.ext import commands

from handlers.ui import build_status_embed, EmbedColors
from services.kingshot_data_service import KingshotDataService

logger = logging.getLogger(__name__)


class KingshotDataHandler:
    """Handles slash commands for `/ks_*` KingShot data endpoints."""

    def __init__(self, service: KingshotDataService, bot: commands.Bot):
        self._service = service
        self._bot = bot
        logger.info("KingshotDataHandler initialized")

    def register_commands(self):
        """Register all KingShot data commands."""

        @self._bot.tree.command(name="ks_arena", description="Get arena defense heroes for a player")
        @app_commands.describe(fid="Governor ID (fid)")
        async def ks_arena(interaction: discord.Interaction, fid: str):
            await self._handle_arena(interaction, fid.strip())

        @self._bot.tree.command(name="ks_alliance", description="Get alliance info and roster by aid + kid")
        @app_commands.describe(
            aid="Alliance id",
            kid="Kingdom id",
        )
        async def ks_alliance(interaction: discord.Interaction, aid: str, kid: int):
            await self._handle_alliance(interaction, aid.strip(), kid)

        @self._bot.tree.command(name="ks_alliance_full", description="Get alliance info + full member profiles")
        @app_commands.describe(
            aid="Alliance id",
            kid="Kingdom id",
        )
        async def ks_alliance_full(interaction: discord.Interaction, aid: str, kid: int):
            await self._handle_alliance_full(interaction, aid.strip(), kid)

        @self._bot.tree.command(name="ks_kingdom_board", description="Get kingdom leaderboard by board type")
        @app_commands.describe(
            board_type="Board type integer (1 Alliance Power, 2 Alliance Kills, 3 Personal Power, 20 Mystic Trial)",
            kid="Kingdom id",
            limit="Rows to return (1-500)",
        )
        async def ks_kingdom_board(interaction: discord.Interaction, board_type: int, kid: int, limit: int = 100):
            await self._handle_kingdom_board(interaction, board_type, kid, limit)

        @self._bot.tree.command(name="ks_global_board", description="Get cross-kingdom leaderboard by board type")
        @app_commands.describe(
            board_type="Board type integer (1 Alliance Power, 2 Alliance Kills, 3 Personal Power, 20 Mystic Trial)",
            limit="Rows to return (1-500)",
        )
        async def ks_global_board(interaction: discord.Interaction, board_type: int, limit: int = 100):
            await self._handle_global_board(interaction, board_type, limit)

        @self._bot.tree.command(name="ks_board_search", description="Find a player's leaderboard rank")
        @app_commands.describe(
            board_type="Board type integer",
            fid="Governor ID (fid)",
            kid="Kingdom id",
        )
        async def ks_board_search(interaction: discord.Interaction, board_type: int, fid: str, kid: int):
            await self._handle_board_search(interaction, board_type, fid.strip(), kid)

    async def _handle_arena(self, interaction: discord.Interaction, fid: str) -> None:
        if not fid:
            await interaction.response.defer(thinking=True)
            await interaction.followup.send(
                embed=build_status_embed(
                    title="Invalid Input",
                    description="`fid` is required.",
                    color=EmbedColors.WARNING,
                    footer="Source: ks.jeab.dev",
                )
            )
            return
        await interaction.response.defer(thinking=True)
        uid = await self._resolve_uid(interaction, fid, endpoint_name="Arena")
        if uid is None:
            return
        result = await self._service.get_arena(uid=uid)
        await self._send_result(
            interaction=interaction,
            endpoint_name="Arena",
            display_name="Arena",
            endpoint=f"/v1/arena/{uid}",
            result=result,
            request_params={"fid": fid, "uid": uid},
            formatter=self._format_arena,
        )

    async def _handle_alliance(self, interaction: discord.Interaction, aid: str, kid: int) -> None:
        if not aid or kid <= 0:
            await interaction.response.defer(thinking=True)
            await interaction.followup.send(
                embed=build_status_embed(
                    title="Invalid Input",
                    description="`aid` and positive `kid` are required.",
                    color=EmbedColors.WARNING,
                )
            )
            return
        await interaction.response.defer(thinking=True)
        result = await self._service.get_alliance(aid=aid, kid=kid)
        await self._send_result(
            interaction=interaction,
            endpoint_name="Alliance",
            display_name="Alliance",
            endpoint=f"/v1/alliances/{aid}?kid={kid}",
            result=result,
            request_params={"aid": aid, "kid": str(kid)},
            formatter=self._format_alliance,
        )

    async def _handle_alliance_full(self, interaction: discord.Interaction, aid: str, kid: int) -> None:
        if not aid or kid <= 0:
            await interaction.response.defer(thinking=True)
            await interaction.followup.send(
                embed=build_status_embed(
                    title="Invalid Input",
                    description="`aid` and positive `kid` are required.",
                    color=EmbedColors.WARNING,
                )
            )
            return
        await interaction.response.defer(thinking=True)
        result = await self._service.get_alliance_full(aid=aid, kid=kid)
        await self._send_result(
            interaction=interaction,
            endpoint_name="Alliance Full",
            display_name="Alliance Full",
            endpoint=f"/v1/alliances/{aid}/full?kid={kid}",
            result=result,
            request_params={"aid": aid, "kid": str(kid)},
            formatter=self._format_alliance_full,
        )

    async def _handle_kingdom_board(self, interaction: discord.Interaction, board_type: int, kid: int, limit: int) -> None:
        if board_type <= 0 or kid <= 0:
            await interaction.response.defer(thinking=True)
            await interaction.followup.send(
                embed=build_status_embed(
                    title="Invalid Input",
                    description="`board_type` and `kid` must be positive integers.",
                    color=EmbedColors.WARNING,
                )
            )
            return
        normalized_limit = self._normalize_limit(limit)
        await interaction.response.defer(thinking=True)
        result = await self._service.get_kingdom_board(
            board_type=board_type,
            kid=kid,
            limit=normalized_limit,
            resolve=True,
        )
        await self._send_result(
            interaction=interaction,
            endpoint_name="Kingdom Board",
            display_name=f"Kingdom Board {self._board_label(board_type)}",
            endpoint=f"/v1/leaderboards/kingdom/{board_type}?kid={kid}&limit={normalized_limit}&resolve=true",
            result=result,
            request_params={"type": str(board_type), "kid": str(kid), "limit": str(normalized_limit), "resolve": "true"},
            formatter=self._format_kingdom_board,
        )

    async def _handle_global_board(self, interaction: discord.Interaction, board_type: int, limit: int) -> None:
        if board_type <= 0:
            await interaction.response.defer(thinking=True)
            await interaction.followup.send(
                embed=build_status_embed(
                    title="Invalid Input",
                    description="`board_type` must be a positive integer.",
                    color=EmbedColors.WARNING,
                )
            )
            return
        normalized_limit = self._normalize_limit(limit)
        await interaction.response.defer(thinking=True)
        result = await self._service.get_global_board(board_type=board_type, limit=normalized_limit, resolve=True)
        await self._send_result(
            interaction=interaction,
            endpoint_name="Global Board",
            display_name=f"Global Board {self._board_label(board_type)}",
            endpoint=f"/v1/leaderboards/global/{board_type}?limit={normalized_limit}&resolve=true",
            result=result,
            request_params={"type": str(board_type), "limit": str(normalized_limit), "resolve": "true"},
            formatter=self._format_global_board,
        )

    async def _handle_board_search(self, interaction: discord.Interaction, board_type: int, fid: str, kid: int) -> None:
        if board_type <= 0 or kid <= 0 or not fid:
            await interaction.response.defer(thinking=True)
            await interaction.followup.send(
                embed=build_status_embed(
                    title="Invalid Input",
                    description="`board_type`, `fid`, and positive `kid` are required.",
                    color=EmbedColors.WARNING,
                )
            )
            return
        await interaction.response.defer(thinking=True)
        uid = await self._resolve_uid(interaction, fid, endpoint_name="Board Search")
        if uid is None:
            return
        result = await self._service.search_leaderboard(board_type=board_type, uid=uid, kid=kid)
        await self._send_result(
            interaction=interaction,
            endpoint_name="Board Search",
            display_name=f"Board Search {self._board_label(board_type)}",
            endpoint="/v1/leaderboards/search",
            result=result,
            request_params={"type": str(board_type), "fid": fid, "uid": uid, "kid": str(kid)},
            formatter=self._format_board_search,
        )

    async def _resolve_uid(self, interaction: discord.Interaction, fid: str, endpoint_name: str) -> str | None:
        lookup = await self._service.get_player_by_fid(fid=fid)
        if not lookup.get("success"):
            error_embed = build_status_embed(
                title=f"KingShot Data Error - {endpoint_name}",
                description=lookup.get("error_message", "Unable to resolve player profile by fid."),
                color=EmbedColors.ERROR,
                footer="Source: ks.jeab.dev",
            )
            error_embed.add_field(name="Endpoint", value=self._truncate(f"/v1/players/by-fid/{fid}"), inline=False)
            error_embed.add_field(name="Status", value=str(lookup.get("status_code")), inline=True)
            error_embed.add_field(name="Error", value=self._truncate(str(lookup.get("error_code", "UNKNOWN"))), inline=True)
            await interaction.followup.send(embed=error_embed)
            return None

        uid = self._extract_uid(lookup.get("data"))
        if uid is None:
            payload = lookup.get("data")
            if isinstance(payload, dict) and isinstance(payload.get("error"), str):
                error_message = payload["error"]
            else:
                error_message = "Unable to resolve uid from fid response."
            error_embed = build_status_embed(
                title=f"KingShot Data Error - {endpoint_name}",
                description=error_message,
                color=EmbedColors.WARNING,
                footer="Source: ks.jeab.dev",
            )
            error_embed.add_field(name="Endpoint", value=self._truncate(f"/v1/players/by-fid/{fid}"), inline=False)
            error_embed.add_field(name="Parsed payload", value=self._truncate(self._stringify_value(payload)), inline=False)
            await interaction.followup.send(embed=error_embed)
            return None
        return str(uid)

    async def _send_result(
        self,
        interaction: discord.Interaction,
        endpoint_name: str,
        display_name: str,
        endpoint: str,
        result: Dict[str, Any],
        request_params: Dict[str, str],
        formatter: Callable[[discord.Embed, Any], None],
    ) -> None:
        if not result.get("success"):
            embed = build_status_embed(
                title=f"{endpoint_name} Unavailable",
                description=result.get("error_message", "Request to KingShot Data API failed."),
                color=EmbedColors.ERROR,
                footer="KingShot Data API",
            )
            embed.add_field(name="Status", value=self._value(result.get("status_code")), inline=True)
            embed.add_field(name="Reason", value=self._value(result.get("error_code", "UNKNOWN")), inline=True)
            embed.add_field(name="Route", value=f"`{self._truncate(endpoint, 900)}`", inline=False)
            await interaction.followup.send(embed=embed)
            return

        data = result.get("data")
        if data is None:
            embed = build_status_embed(
                title=display_name,
                description="No data came back for this request.",
                color=EmbedColors.INFO,
                footer="KingShot Data API",
            )
            embed.add_field(name="Request", value=self._format_request_params(request_params), inline=False)
            await interaction.followup.send(embed=embed)
            return

        embed = build_status_embed(
            title=display_name,
            description="Live game gateway data.",
            color=EmbedColors.SUCCESS,
            footer="KingShot Data API",
        )
        formatter(embed, data)
        embed.add_field(name="Request", value=self._format_request_params(request_params), inline=False)
        await interaction.followup.send(embed=embed)

    def _format_arena(self, embed: discord.Embed, data: Any) -> None:
        payload = self._ensure_dict(data)
        if not payload:
            embed.description = "No arena defense data was returned."
            return

        name = self._first(payload, ["name", "playerName", "nickname", "allyName"]) or "Unknown player"
        uid = self._first(payload, ["uid", "playerId", "player_id", "id"])
        kingdom = self._first(payload, ["kid", "kingdom", "kingdomId"])
        heroes = self._extract_hero_list(payload)

        embed.description = self._identity_line(name=name, uid=uid, kingdom=kingdom)
        self._add_metric_fields(
            embed,
            payload,
            [
                ("Power", ["power", "totalPower", "battlePower"]),
                ("Castle", ["castleLevel", "castle", "cityLevel"]),
                ("Arena Rank", ["arenaRank", "rank", "position"]),
            ],
        )
        embed.add_field(name="Defense Team", value=self._render_hero_table(heroes), inline=False)

    def _format_alliance(self, embed: discord.Embed, data: Any) -> None:
        payload = self._ensure_dict(data)
        if not payload:
            embed.description = "No alliance data was returned."
            return

        alliance = self._first(payload, ["alliance", "info", "summary"], default=payload) or payload
        members = self._extract_member_list(payload)
        alliance_name = self._first(alliance, ["name", "allianceName", "alliance_name", "tag"]) or "Unknown alliance"
        aid = self._first(alliance, ["aid", "allianceId", "id"])
        kingdom = self._first(alliance, ["kid", "kingdomId", "kingdom"])

        embed.description = self._identity_line(name=alliance_name, uid=aid, kingdom=kingdom, id_label="AID")
        self._add_metric_fields(
            embed,
            alliance,
            [
                ("Level", ["level", "allianceLevel"]),
                ("Members", ["memberCount", "membersCount", "numMembers", "count"]),
                ("Power", ["power", "totalPower", "score"]),
                ("Kills", ["kills", "killCount"]),
            ],
        )
        embed.add_field(name="Roster Preview", value=self._render_member_table(members, full=False), inline=False)

    def _format_alliance_full(self, embed: discord.Embed, data: Any) -> None:
        payload = self._ensure_dict(data)
        if not payload:
            embed.description = "No alliance data was returned."
            return

        alliance = self._first(payload, ["alliance", "info", "summary"], default=payload) or payload
        members = self._extract_member_list(payload)
        alliance_name = self._first(alliance, ["name", "allianceName", "alliance_name", "tag"]) or "Unknown alliance"
        aid = self._first(alliance, ["aid", "allianceId", "id"])
        kingdom = self._first(alliance, ["kid", "kingdomId", "kingdom"])

        embed.description = self._identity_line(name=alliance_name, uid=aid, kingdom=kingdom, id_label="AID")
        self._add_metric_fields(
            embed,
            alliance,
            [
                ("Level", ["level", "allianceLevel"]),
                ("Members", ["memberCount", "membersCount", "numMembers", "count"]),
                ("Power", ["power", "totalPower", "score"]),
                ("Kills", ["kills", "killCount"]),
            ],
        )
        embed.add_field(name="Member Profiles", value=self._render_member_table(members, full=True), inline=False)

    def _format_kingdom_board(self, embed: discord.Embed, data: Any) -> None:
        self._format_board_payload(embed, data, scope="Kingdom")

    def _format_global_board(self, embed: discord.Embed, data: Any) -> None:
        self._format_board_payload(embed, data, scope="Global")

    def _format_board_payload(self, embed: discord.Embed, data: Any, scope: str) -> None:
        entries = self._sort_board_entries(self._extract_entry_list(data))
        meta = self._ensure_dict(data)
        board_type = self._first(meta, ["type", "boardType", "board"])
        kid = self._first(meta, ["kid", "kingdom", "kingdomId"])
        board_name = self._board_label(int(board_type)) if self._is_int_like(board_type) else "Leaderboard"

        embed.description = f"{scope} {board_name}"
        if kid is not None:
            embed.add_field(name="Kingdom", value=self._value(kid), inline=True)
        embed.add_field(name="Rows", value=self._value(len(entries)), inline=True)

        if not entries:
            embed.add_field(name="Leaderboard", value="No entries returned for this board.", inline=False)
            return

        embed.add_field(name="Top Rankings", value=self._render_board_table(entries), inline=False)

    def _format_board_search(self, embed: discord.Embed, data: Any) -> None:
        payload = self._ensure_dict(data)
        entry = payload.get("entry") if isinstance(payload, dict) else None
        if not payload and not entry:
            embed.description = "No matching rank data was returned."
            return

        target = entry if isinstance(entry, dict) else payload
        target = self._ensure_dict(target) or {}

        rank = self._first(target, ["rank", "position", "place"])
        score = self._first(target, ["score", "value", "points", "power"])
        uid = self._first(target, ["uid", "playerId", "id"])
        name = self._first(target, ["name", "playerName", "nickname", "tag"])
        guild = self._first(target, ["kingdom", "kid", "kingdomId"])
        board = self._first(target, ["type", "boardType", "board"]) or "unknown"

        embed.description = self._identity_line(name=name or "Unknown player", uid=uid, kingdom=guild)
        embed.add_field(name="Board", value=self._value(self._board_label(int(board)) if self._is_int_like(board) else board), inline=True)
        embed.add_field(name="Rank", value=self._value(rank), inline=True)
        embed.add_field(name="Score", value=self._value(score, numeric=True), inline=True)

    @staticmethod
    def _format_request_params(request_params: Dict[str, str]) -> str:
        lines = [f"{key.upper()}: `{value}`" for key, value in request_params.items()]
        return " | ".join(lines) or "No params."

    @staticmethod
    def _stringify_value(value: Any) -> str:
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, indent=2)
        return str(value)

    @staticmethod
    def _truncate(value: str, max_len: int = 1024) -> str:
        if len(value) <= max_len:
            return value
        return value[: max(0, max_len - 3)] + "..."

    @staticmethod
    def _normalize_limit(limit: int) -> int:
        try:
            normalized = int(limit)
        except (TypeError, ValueError):
            return 100
        if normalized < 1:
            return 1
        if normalized > 500:
            return 500
        return normalized

    @staticmethod
    def _ensure_dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _first(value: Any, keys: list[str], default: Any = None) -> Any:
        if isinstance(value, dict):
            for key in keys:
                if key in value and value.get(key) is not None:
                    return value.get(key)
        return default

    @staticmethod
    def _is_int_like(value: Any) -> bool:
        try:
            int(value)
        except (TypeError, ValueError):
            return False
        return True

    @classmethod
    def _value(cls, value: Any, *, numeric: bool = False) -> str:
        if value is None or value == "":
            return "`n/a`"
        if numeric:
            return f"`{cls._format_number(value)}`"
        return f"`{cls._truncate(str(value), 90)}`"

    @classmethod
    def _identity_line(cls, *, name: Any, uid: Any, kingdom: Any = None, id_label: str = "UID") -> str:
        parts = [f"**{cls._truncate(str(name or 'Unknown'), 180)}**"]
        if uid is not None:
            parts.append(f"{id_label}: `{uid}`")
        if kingdom is not None:
            parts.append(f"Kingdom: `{kingdom}`")
        return "\n".join(parts)

    @classmethod
    def _format_number(cls, value: Any) -> str:
        try:
            number = float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return cls._truncate(str(value), 90)
        if number.is_integer():
            return f"{int(number):,}"
        return f"{number:,.2f}"

    @classmethod
    def _add_metric_fields(cls, embed: discord.Embed, payload: dict[str, Any], specs: list[tuple[str, list[str]]]) -> None:
        for label, keys in specs:
            value = cls._first(payload, keys)
            if value is not None:
                embed.add_field(name=label, value=cls._value(value, numeric=label not in {"Level", "Castle"}), inline=True)

    @classmethod
    def _extract_uid(cls, payload: Any) -> str | None:
        if payload is None:
            return None
        if isinstance(payload, dict):
            for key in ["uid", "id", "player_id", "playerId", "playerUid", "gid", "player"]:
                value = payload.get(key)
                if value is not None:
                    if key == "player":
                        nested = cls._extract_uid(value)
                        if nested is not None:
                            return nested
                    else:
                        return str(value)
            nested_player = payload.get("player")
            nested = cls._extract_uid(nested_player)
            if nested is not None:
                return nested
            for key in ["data", "result", "payload"]:
                nested = cls._extract_uid(payload.get(key))
                if nested is not None:
                    return nested
        if isinstance(payload, list):
            for item in payload:
                nested = cls._extract_uid(item)
                if nested is not None:
                    return nested
        return None

    @classmethod
    def _extract_entry_list(cls, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [entry for entry in payload if isinstance(entry, dict)]

        if not isinstance(payload, dict):
            return []

        for key in ["entries", "rows", "data", "board", "leaderboard", "items", "results"]:
            value = payload.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
            if isinstance(value, dict):
                nested = cls._extract_entry_list(value)
                if nested:
                    return nested
        return []

    @classmethod
    def _extract_member_list(cls, payload: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ["members", "roster", "memberList", "players", "member_profiles", "profiles"]:
            value = payload.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
            if isinstance(value, dict):
                nested = cls._extract_member_list(value)
                if nested:
                    return nested
        return []

    @classmethod
    def _extract_hero_list(cls, payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [entry for entry in payload if isinstance(entry, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ["heroes", "arena", "team", "defenseTeam", "defense", "lineup", "formations"]:
            value = payload.get(key)
            if isinstance(value, list):
                return [entry for entry in value if isinstance(entry, dict)]
            if isinstance(value, dict):
                nested = cls._extract_hero_list(value)
                if nested:
                    return nested
        return []

    @classmethod
    def _render_hero_table(cls, heroes: list[dict[str, Any]]) -> str:
        if not heroes:
            return "No defense heroes listed."
        lines = []
        for index, item in enumerate(heroes[:8], start=1):
            hero = cls._first(item, ["heroName", "name", "id"]) or "Unknown Hero"
            position = cls._first(item, ["pos", "slot", "position", "index"]) or "?"
            star = cls._first(item, ["stars", "quality", "star"])
            level = cls._first(item, ["level", "lv", "lvl"])
            lines.append(
                f"`{index:>2}.` **{cls._truncate(str(hero), 26)}** "
                f"| Slot `{position}` | Lvl `{level or 'n/a'}` | Stars `{star or 'n/a'}`"
            )
        return cls._truncate("\n".join(lines), 1000)

    @classmethod
    def _render_member_table(cls, members: list[dict[str, Any]], *, full: bool) -> str:
        if not members:
            return "No roster entries returned."
        lines = []
        limit = 10 if full else 12
        for index, member in enumerate(members[:limit], start=1):
            name = cls._first(member, ["name", "nickname", "playerName"]) or "Unknown"
            uid = cls._first(member, ["uid", "id", "playerId"])
            power = cls._first(member, ["power", "powerScore", "score"])
            alliance_rank = cls._first(member, ["rank", "position"])
            castle = cls._first(member, ["castle", "castleLevel", "cityLevel"])
            if full:
                lines.append(
                    f"`{index:>2}.` **{cls._truncate(str(name), 22)}** "
                    f"| Pwr `{cls._format_number(power) if power is not None else 'n/a'}` "
                    f"| C `{castle or 'n/a'}` | R `{alliance_rank or 'n/a'}`"
                )
            else:
                lines.append(
                    f"`{index:>2}.` **{cls._truncate(str(name), 24)}** "
                    f"| UID `{uid or 'n/a'}` | Pwr `{cls._format_number(power) if power is not None else 'n/a'}`"
                )
        if len(members) > limit:
            lines.append(f"`...` {len(members) - limit} more members")
        return cls._truncate("\n".join(lines), 1000)

    @classmethod
    def _render_board_table(cls, entries: list[dict[str, Any]]) -> str:
        lines = []
        for index, entry in enumerate(entries[:15], start=1):
            name = cls._first(entry, ["name", "playerName", "allianceName", "nickname", "tag"]) or "Unknown"
            entity_id = cls._first(entry, ["uid", "id", "playerId", "allianceId"])
            score = cls._first(entry, ["value", "score", "power", "points", "rankValue"])
            rank = cls._first(entry, ["rank", "position", "place", "idx"]) or index
            lines.append(
                f"`#{str(rank).rjust(3)}` **{cls._truncate(str(name), 24)}** "
                f"| `{cls._format_number(score) if score is not None else 'n/a'}` "
                f"| ID `{entity_id or 'n/a'}`"
            )
        if len(entries) > 15:
            lines.append(f"`...` {len(entries) - 15} more entries")
        return cls._truncate("\n".join(lines), 1000) if lines else "No readable leaderboard rows."

    @classmethod
    def _sort_board_entries(cls, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            entries,
            key=lambda entry: (
                cls._sort_number(cls._first(entry, ["rank", "position", "place", "idx"]), default=10**12),
                -cls._sort_number(cls._first(entry, ["score", "value", "power", "points", "rankValue"]), default=0),
            ),
        )

    @staticmethod
    def _sort_number(value: Any, *, default: int) -> int:
        try:
            return int(float(str(value).replace(",", "")))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _board_label(board_type: int) -> str:
        labels = {
            1: "Alliance Power",
            2: "Alliance Kills",
            3: "Personal Power",
            20: "Mystic Trial",
        }
        return labels.get(board_type, f"Type {board_type}")
