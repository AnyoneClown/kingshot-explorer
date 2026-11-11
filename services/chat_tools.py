"""Read-only tools the chat model may call, scoped to the Discord server it is talking in.

Each tool is a thin adapter over an existing service. Adapters never raise: every
failure becomes a short JSON error the model can explain. Tool results are untrusted
data (they contain player names, reminder text and knowledge-base content written by
other people), and the chat system prompt says so.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

ToolHandler = Callable[["ToolContext", dict], Awaitable[Any]]


@dataclass(frozen=True)
class ToolContext:
    """Where the conversation happens. Tools may only read data for this guild/channel."""

    guild_id: int
    channel_id: Optional[int] = None
    user_id: Optional[int] = None


@dataclass(frozen=True)
class ChatTool:
    name: str
    description: str
    parameters: dict
    handler: ToolHandler

    def definition(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class ToolError(Exception):
    """A user-facing tool failure (bad arguments, missing data)."""


class ChatToolRegistry:
    """Holds the available tools and executes calls safely."""

    def __init__(
        self,
        tools: Iterable[ChatTool] = (),
        *,
        timeout_seconds: float = 15.0,
        max_result_chars: int = 4000,
    ):
        self._tools: dict[str, ChatTool] = {}
        self._timeout_seconds = timeout_seconds
        self._max_result_chars = max_result_chars
        for tool in tools:
            self.register(tool)

    def register(self, tool: ChatTool) -> None:
        self._tools[tool.name] = tool

    @property
    def names(self) -> list[str]:
        return list(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __bool__(self) -> bool:
        return bool(self._tools)

    def definitions(self) -> list[dict]:
        return [tool.definition() for tool in self._tools.values()]

    async def execute(self, name: str, arguments: Any, context: ToolContext) -> str:
        """Run one tool call and return its JSON result. Never raises."""
        tool = self._tools.get(name)
        if tool is None:
            return self._encode({"ok": False, "error": f"Unknown tool {name!r}."})
        if context is None or not context.guild_id:
            return self._encode({"ok": False, "error": "Tools are only available inside a server."})

        try:
            parsed = self._parse_arguments(arguments)
        except ToolError as exc:
            return self._encode({"ok": False, "error": str(exc)})

        try:
            data = await asyncio.wait_for(tool.handler(context, parsed), timeout=self._timeout_seconds)
            payload = {"ok": True, "data": data}
        except ToolError as exc:
            payload = {"ok": False, "error": str(exc)}
        except asyncio.TimeoutError:
            logger.warning("Chat tool %s timed out in guild %s", name, context.guild_id)
            payload = {"ok": False, "error": "The data source did not answer in time."}
        except Exception:
            logger.warning("Chat tool %s failed in guild %s", name, context.guild_id, exc_info=True)
            payload = {"ok": False, "error": "The data source is unavailable right now."}

        logger.info("Chat tool %s executed in guild %s ok=%s", name, context.guild_id, payload["ok"])
        return self._encode(payload)

    @staticmethod
    def _parse_arguments(arguments: Any) -> dict:
        if arguments is None or arguments == "":
            return {}
        if isinstance(arguments, dict):
            return arguments
        try:
            parsed = json.loads(arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ToolError("Tool arguments must be a JSON object.") from exc
        if parsed is None:
            return {}
        if not isinstance(parsed, dict):
            raise ToolError("Tool arguments must be a JSON object.")
        return parsed

    def _encode(self, payload: dict) -> str:
        encoded = json.dumps(payload, ensure_ascii=False, default=_json_default)
        if len(encoded) <= self._max_result_chars:
            return encoded
        return json.dumps(
            {"ok": payload.get("ok", False), "truncated": True, "data_preview": encoded[: self._max_result_chars]},
            ensure_ascii=False,
        )


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _field(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` from a dict or an object (service return shapes may be either)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _iso(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    return str(value) if value is not None else None


def _discord_timestamp(value: Any) -> Optional[str]:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return f"<t:{int(value.timestamp())}:F>"


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Alliance power (AlliancePowerService.get_report)
# ---------------------------------------------------------------------------


def _member_rows(snapshot: Any) -> dict[str, dict]:
    members = _field(snapshot, "members") or {}
    if not isinstance(members, dict):
        return {}
    return {str(uid): row for uid, row in members.items() if isinstance(row, dict)}


def _snapshot_summary(snapshot: Any) -> Optional[dict]:
    if snapshot is None:
        return None
    return {
        "date": _iso(_field(snapshot, "snapshot_date")),
        "total_power": _int_or_none(_field(snapshot, "total_power")),
        "member_count": len(_member_rows(snapshot)),
    }


def _change(latest: Any, previous: Any) -> Optional[dict]:
    if latest is None or previous is None:
        return None
    now, then = _int_or_none(_field(latest, "total_power")), _int_or_none(_field(previous, "total_power"))
    if now is None or then is None:
        return None
    return {
        "since": _iso(_field(previous, "snapshot_date")),
        "total_power_change": now - then,
        "percent": round((now - then) * 100 / then, 2) if then else None,
    }


def _member_movers(latest: Any, previous: Any, *, count: int = 3) -> Optional[dict]:
    if latest is None or previous is None:
        return None
    before = _member_rows(previous)
    deltas = []
    for uid, row in _member_rows(latest).items():
        old = before.get(uid)
        now_power, old_power = _int_or_none(row.get("power")), _int_or_none((old or {}).get("power"))
        if now_power is None or old_power is None:
            continue
        deltas.append((now_power - old_power, _clip(row.get("name"), 40)))
    deltas.sort()
    return {
        "top_gainers": [{"name": name, "power_change": delta} for delta, name in reversed(deltas[-count:]) if delta > 0],
        "top_decliners": [{"name": name, "power_change": delta} for delta, name in deltas[:count] if delta < 0],
    }


def _names(rows: Any, limit: int = 5) -> list[str]:
    names = []
    for row in list(rows or [])[:limit]:
        if isinstance(row, (tuple, list)) and row:
            row = row[-1] if isinstance(row[-1], (str, dict)) else row[0]
        name = _field(row, "name") if not isinstance(row, str) else row
        names.append(_clip(name, 40))
    return names


def _optional_report_extras(report: Any) -> dict:
    """Roster changes and inactivity, when the service provides them (shape may evolve)."""
    extras: dict = {}
    try:
        changes = _field(report, "changes")
        if changes is not None:
            extras["roster_changes"] = {
                "since": _iso(_field(changes, "older_date")),
                "joined": _names(_field(changes, "joined")),
                "left": _names(_field(changes, "left")),
                "joined_count": len(_field(changes, "joined") or []),
                "left_count": len(_field(changes, "left") or []),
            }
        inactive = _field(report, "inactive")
        if isinstance(inactive, dict):
            extras["flat_power_member_counts"] = {
                f"{days}_days": len(members) for days, members in inactive.items() if members is not None
            }
    except Exception:
        logger.debug("Optional alliance report fields had an unexpected shape", exc_info=True)
        return {}
    return extras


def make_alliance_power_tool(alliance_power_service: Any) -> ChatTool:
    async def handler(context: ToolContext, _arguments: dict) -> Any:
        report = await alliance_power_service.get_report(context.guild_id)
        if not report:
            return {"tracked": False, "note": "This server is not tracking an alliance. Admins can set one up with /alliance."}
        latest = _field(report, "latest")
        members = sorted(
            _member_rows(latest).values(),
            key=lambda row: _int_or_none(row.get("power")) or 0,
            reverse=True,
        )
        return _optional_report_extras(report) | {
            "tracked": True,
            "kingdom_id": _field(report, "kingdom_id"),
            "alliance_id": _field(report, "alliance_id"),
            "alliance_name": _clip(_field(latest, "alliance_name"), 60) if latest is not None else None,
            "alliance_tag": _clip(_field(latest, "alliance_tag"), 10) if latest is not None else None,
            "latest": _snapshot_summary(latest),
            "daily_change": _change(latest, _field(report, "daily")),
            "weekly_change": _change(latest, _field(report, "weekly")),
            "weekly_member_movers": _member_movers(latest, _field(report, "weekly")),
            "history": [_snapshot_summary(item) for item in (_field(report, "history") or [])][-7:],
            "top_members": [
                {"name": _clip(row.get("name"), 40), "power": _int_or_none(row.get("power"))} for row in members[:5]
            ],
        }

    return ChatTool(
        name="get_alliance_power_report",
        description=(
            "Get the power report of the alliance this Discord server tracks: total power, daily and weekly "
            "change, recent history, strongest members and biggest weekly movers."
        ),
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
    )


# ---------------------------------------------------------------------------
# Reminders (EventSchedulerService.get_events_for_channel / get_events_for_guild)
# ---------------------------------------------------------------------------


def make_reminders_tool(event_scheduler_service: Any, *, now_provider=None, limit: int = 10) -> ChatTool:
    now_provider = now_provider or (lambda: datetime.now(timezone.utc))

    # Channel scope only: a server-wide listing would reveal reminders from private
    # channels (e.g. officer chats) to anyone who can mention the bot.
    async def handler(context: ToolContext, arguments: dict) -> Any:
        del arguments
        if context.channel_id is None:
            raise ToolError("No channel is available for this request.")
        scope = "channel"
        events = await event_scheduler_service.get_events_for_channel(context.channel_id)

        now = now_provider()
        upcoming = []
        for event in events or []:
            when = _field(event, "event_time")
            if isinstance(when, datetime):
                aware = when if when.tzinfo else when.replace(tzinfo=timezone.utc)
                if aware < now:
                    continue
            upcoming.append(event)
        upcoming.sort(key=lambda event: _iso(_field(event, "event_time")) or "")
        return {
            "scope": scope,
            "count": len(upcoming),
            "reminders": [
                {
                    "id": _field(event, "id"),
                    "time_utc": _iso(_field(event, "event_time")),
                    "discord_timestamp": _discord_timestamp(_field(event, "event_time")),
                    "message": _clip(_field(event, "message"), 200),
                    "repeat_every_days": _field(event, "repeat_every_days"),
                }
                for event in upcoming[:limit]
            ],
        }

    properties: dict = {}
    description = "List upcoming scheduled reminders/events in the current channel, soonest first."
    return ChatTool(
        name="list_upcoming_reminders",
        description=description + " discord_timestamp can be pasted into the reply to show local time.",
        parameters={"type": "object", "properties": properties, "additionalProperties": False},
        handler=handler,
    )


# ---------------------------------------------------------------------------
# Gift codes (GiftCodeService.get_available_gift_codes)
# ---------------------------------------------------------------------------


def make_gift_codes_tool(gift_code_service: Any, *, limit: int = 15) -> ChatTool:
    async def handler(_context: ToolContext, _arguments: dict) -> Any:
        response = await gift_code_service.get_available_gift_codes()
        if not isinstance(response, dict) or not response.get("success"):
            raise ToolError("The gift code list could not be retrieved right now.")
        codes = [row for row in response.get("data") or [] if isinstance(row, dict) and row.get("code")]
        return {
            "count": len(codes),
            "codes": [
                {"code": _clip(row.get("code"), 40), "expires_at": row.get("expiresAt") or row.get("expires_at")}
                for row in codes[:limit]
            ],
            "note": "Players can redeem codes in game; bot admins can use /redeem.",
        }

    return ChatTool(
        name="list_active_gift_codes",
        description="List currently active Kingshot gift codes and when they expire.",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
        handler=handler,
    )


# ---------------------------------------------------------------------------
# Governor lookup (KingshotDataService.get_player_by_fid)
# ---------------------------------------------------------------------------

_GOVERNOR_ID = re.compile(r"^\d{4,12}$")


def make_governor_lookup_tool(kingshot_data_service: Any) -> ChatTool:
    async def handler(_context: ToolContext, arguments: dict) -> Any:
        governor_id = str(arguments.get("governor_id") or "").strip()
        if not _GOVERNOR_ID.match(governor_id):
            raise ToolError("governor_id must be the numeric in-game Governor ID (4-12 digits).")
        result = await kingshot_data_service.get_player_by_fid(governor_id)
        if not isinstance(result, dict) or not result.get("success"):
            status = result.get("status_code") if isinstance(result, dict) else None
            if status == 404:
                raise ToolError("No player was found with that Governor ID.")
            raise ToolError("The player lookup service is unavailable right now.")
        data = result.get("data") or {}
        if not isinstance(data, dict):
            raise ToolError("The player lookup returned no profile.")
        alliance = data.get("alliance") if isinstance(data.get("alliance"), dict) else {}
        return {
            "governor_id": data.get("fid") or governor_id,
            "name": _clip(data.get("name"), 40),
            "kingdom_id": data.get("kid"),
            "power": data.get("power"),
            "level": data.get("lv"),
            "town_center_level": data.get("stove_lv"),
            "vip": data.get("vip"),
            "alliance": {"tag": alliance.get("abbr"), "name": _clip(alliance.get("name"), 40)}
            if alliance.get("name") or alliance.get("abbr")
            else None,
        }

    return ChatTool(
        name="lookup_governor",
        description="Look up a Kingshot player's public profile (name, kingdom, power, alliance) by Governor ID.",
        parameters={
            "type": "object",
            "properties": {"governor_id": {"type": "string", "description": "Numeric in-game Governor ID."}},
            "required": ["governor_id"],
            "additionalProperties": False,
        },
        handler=handler,
    )


# ---------------------------------------------------------------------------
# Knowledge search (KingshotRAGService.search)
# ---------------------------------------------------------------------------


# A filtered search whose best chunk is farther than this also searches without the filter.
FILTER_FALLBACK_DISTANCE = 0.55


def make_knowledge_search_tool(rag_service: Any, *, max_distance: Optional[float] = None, limit: int = 4) -> ChatTool:
    cutoff = max_distance if max_distance is not None else getattr(rag_service, "max_distance", 0.7)

    async def handler(_context: ToolContext, arguments: dict) -> Any:
        query = " ".join(str(arguments.get("query") or "").split())[:300]
        if not query:
            raise ToolError("query is required.")
        entity_type = " ".join(str(arguments.get("entity_type") or "").split()) or None
        chunks = await rag_service.search(query, limit=limit, entity_type=entity_type)
        matches = [chunk for chunk in chunks or [] if float(_field(chunk, "distance", 99)) <= cutoff]
        best = min((float(_field(chunk, "distance", 99)) for chunk in matches), default=99.0)
        if entity_type and best > FILTER_FALLBACK_DISTANCE:
            # The model guesses filters ("building" for governor gear); a wrong guess must not
            # hide the right answer, so also search everything and keep the closest chunks.
            unfiltered = await rag_service.search(query, limit=limit)
            merged = {
                str(_field(chunk, "chunk_id", id(chunk))): chunk
                for chunk in matches + [c for c in unfiltered or [] if float(_field(c, "distance", 99)) <= cutoff]
            }
            matches = sorted(merged.values(), key=lambda chunk: float(_field(chunk, "distance", 99)))[:limit]
        if not matches:
            return {"results": [], "note": "No matching knowledge in the Kingshot knowledge base."}
        return {
            "results": [
                {
                    "name": _clip(_field(chunk, "name"), 80),
                    "type": _field(chunk, "entity_type"),
                    "content": _clip(_field(chunk, "content"), 900),
                    "data": _clip(json.dumps(_field(chunk, "entity_data") or {}, ensure_ascii=False), 500),
                    "relevance": round(1 - float(_field(chunk, "distance", 1)), 3),
                }
                for chunk in matches
            ]
        }

    return ChatTool(
        name="search_kingshot_knowledge",
        description=(
            "Search the curated Kingshot game knowledge base: heroes (stats, skills per level, "
            "exclusive weapons, tier lists by role), buildings and Truegold upgrade costs, pets, "
            "masters, research (Academy, War Academy, Advanced Truegold, Alliance), governor gear "
            "and charms. Use it for game questions before answering from memory."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to look up, in English."},
                "entity_type": {
                    "type": "string",
                    "description": (
                        "Optional filter: hero, hero_tier_list, building, pet, master, academy_research, "
                        "war_academy_research, advanced_truegold_research, alliance_research, "
                        "truegold_requirements, governor_gear_overview or governor_charm_overview. "
                        "Leave it out when unsure."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
    )


def build_chat_tool_registry(
    *,
    alliance_power_service: Any = None,
    event_scheduler_service: Any = None,
    gift_code_service: Any = None,
    kingshot_data_service: Any = None,
    rag_service: Any = None,
    rag_max_distance: Optional[float] = None,
) -> ChatToolRegistry:
    """Build the default read-only registry from whichever services are available."""
    tools: list[ChatTool] = []
    if alliance_power_service is not None:
        tools.append(make_alliance_power_tool(alliance_power_service))
    if event_scheduler_service is not None:
        tools.append(make_reminders_tool(event_scheduler_service))
    if gift_code_service is not None:
        tools.append(make_gift_codes_tool(gift_code_service))
    if kingshot_data_service is not None:
        tools.append(make_governor_lookup_tool(kingshot_data_service))
    if rag_service is not None:
        tools.append(make_knowledge_search_tool(rag_service, max_distance=rag_max_distance))
    registry = ChatToolRegistry(tools)
    logger.info("Chat tools available: %s", ", ".join(registry.names) or "none")
    return registry
