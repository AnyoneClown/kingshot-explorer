import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from services.chat_tools import (
    ChatTool,
    ChatToolRegistry,
    ToolContext,
    build_chat_tool_registry,
    make_alliance_power_tool,
    make_gift_codes_tool,
    make_governor_lookup_tool,
    make_knowledge_search_tool,
    make_reminders_tool,
)

CONTEXT = ToolContext(guild_id=456, channel_id=789, user_id=1)
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def run(registry, name, arguments="{}", context=CONTEXT):
    return json.loads(asyncio.run(registry.execute(name, arguments, context)))


def registry_with(*tools):
    return ChatToolRegistry(tools)


def test_registry_rejects_unknown_tool_and_bad_arguments():
    async def handler(context, arguments):
        return {"echo": arguments}

    registry = registry_with(ChatTool("echo", "echo", {"type": "object", "properties": {}}, handler))

    assert run(registry, "nope")["ok"] is False
    assert run(registry, "echo", "not json")["ok"] is False
    assert run(registry, "echo", "[1, 2]")["ok"] is False
    assert run(registry, "echo", '{"a": 1}') == {"ok": True, "data": {"echo": {"a": 1}}}
    assert registry.definitions()[0] == {
        "type": "function",
        "function": {"name": "echo", "description": "echo", "parameters": {"type": "object", "properties": {}}},
    }


def test_registry_turns_exceptions_and_timeouts_into_errors():
    async def broken(context, arguments):
        raise RuntimeError("password=secret")

    async def slow(context, arguments):
        await asyncio.sleep(1)

    registry = ChatToolRegistry(
        [
            ChatTool("broken", "", {"type": "object"}, broken),
            ChatTool("slow", "", {"type": "object"}, slow),
        ],
        timeout_seconds=0.01,
    )

    broken_result = run(registry, "broken")
    assert broken_result["ok"] is False
    assert "secret" not in broken_result["error"]
    assert run(registry, "slow")["ok"] is False


def test_registry_requires_a_guild():
    async def handler(context, arguments):
        return {}

    registry = registry_with(ChatTool("x", "", {"type": "object"}, handler))

    assert run(registry, "x", context=ToolContext(guild_id=0))["ok"] is False


def snapshot(day, total, members):
    return SimpleNamespace(
        snapshot_date=day,
        captured_at=datetime(day.year, day.month, day.day, tzinfo=timezone.utc),
        alliance_name="Aquarium",
        alliance_tag="AQM",
        total_power=total,
        members=members,
    )


class FakeAllianceService:
    def __init__(self, report):
        self.report = report
        self.calls = []

    async def get_report(self, guild_id):
        self.calls.append(guild_id)
        return self.report


def test_alliance_power_tool_summarises_report_for_current_guild_only():
    today = date(2026, 11, 5)
    latest = snapshot(
        today,
        300,
        {"1": {"name": "Ann", "power": 200}, "2": {"name": "Bo", "power": 100}},
    )
    daily = snapshot(today - timedelta(days=1), 250, {"1": {"name": "Ann", "power": 180}})
    weekly = snapshot(today - timedelta(days=7), 200, {"1": {"name": "Ann", "power": 150}, "2": {"name": "Bo", "power": 120}})
    service = FakeAllianceService(
        {
            "kingdom_id": 3,
            "alliance_id": 99,
            "latest": latest,
            "daily": daily,
            "weekly": weekly,
            "history": [weekly, daily, latest],
            "changes": SimpleNamespace(older_date=daily.snapshot_date, joined=[("2", {"name": "Bo"})], left=[]),
            "inactive": {7: [], 14: None},
        }
    )
    registry = registry_with(make_alliance_power_tool(service))

    data = run(registry, "get_alliance_power_report")["data"]

    assert service.calls == [456]
    assert data["alliance_tag"] == "AQM"
    assert data["latest"] == {"date": "2026-11-05", "total_power": 300, "member_count": 2}
    assert data["daily_change"]["total_power_change"] == 50
    assert data["weekly_change"]["percent"] == 50.0
    assert data["top_members"][0] == {"name": "Ann", "power": 200}
    assert data["weekly_member_movers"]["top_gainers"] == [{"name": "Ann", "power_change": 50}]
    assert data["weekly_member_movers"]["top_decliners"] == [{"name": "Bo", "power_change": -20}]
    assert data["roster_changes"]["joined"] == ["Bo"]
    assert data["flat_power_member_counts"] == {"7_days": 0}


def test_alliance_power_tool_reports_untracked_guild():
    registry = registry_with(make_alliance_power_tool(FakeAllianceService(None)))

    assert run(registry, "get_alliance_power_report")["data"]["tracked"] is False


class FakeScheduler:
    def __init__(self, events):
        self.events = events
        self.channel_calls = []

    async def get_events_for_channel(self, channel_id):
        self.channel_calls.append(channel_id)
        return self.events


def event(event_id, when, message="Bear trap"):
    return SimpleNamespace(id=event_id, event_time=when, role_names=["R4"], message=message, repeat_every_days=None)


def test_reminders_tool_lists_upcoming_events_in_current_channel():
    scheduler = FakeScheduler(
        [
            event(2, NOW + timedelta(hours=5), "Later"),
            event(1, NOW - timedelta(hours=1), "Past"),
            event(3, NOW + timedelta(hours=1), "Soon"),
        ]
    )
    registry = registry_with(make_reminders_tool(scheduler, now_provider=lambda: NOW))

    data = run(registry, "list_upcoming_reminders")["data"]

    assert scheduler.channel_calls == [789]
    assert [item["message"] for item in data["reminders"]] == ["Soon", "Later"]
    assert data["reminders"][0]["discord_timestamp"].startswith("<t:")
    # No server scope is offered when the service cannot list by guild.
    assert "scope" not in registry.definitions()[0]["function"]["parameters"]["properties"]


def test_reminders_tool_never_lists_other_channels():
    class GuildScheduler(FakeScheduler):
        def __init__(self):
            super().__init__([])
            self.guild_calls = []

        async def get_events_for_guild(self, guild_id):  # pragma: no cover - must not be used
            self.guild_calls.append(guild_id)
            return [event(5, NOW + timedelta(days=1))]

    scheduler = GuildScheduler()
    registry = registry_with(make_reminders_tool(scheduler, now_provider=lambda: NOW))

    assert "scope" not in registry.definitions()[0]["function"]["parameters"]["properties"]
    data = run(registry, "list_upcoming_reminders", '{"scope": "server"}')["data"]

    assert scheduler.guild_calls == []
    assert scheduler.channel_calls == [789]
    assert data["scope"] == "channel"


def test_gift_codes_tool_lists_codes_and_handles_failure():
    class Gifts:
        def __init__(self, response):
            self.response = response

        async def get_available_gift_codes(self):
            return self.response

    ok = registry_with(make_gift_codes_tool(Gifts({"success": True, "data": [{"code": "KS2026", "expiresAt": "2026-12-01T00:00:00Z"}, {"x": 1}]})))
    failing = registry_with(make_gift_codes_tool(Gifts({"success": False, "message": "HTTP Error 500"})))

    assert run(ok, "list_active_gift_codes")["data"]["codes"] == [
        {"code": "KS2026", "expires_at": "2026-12-01T00:00:00Z"}
    ]
    assert run(failing, "list_active_gift_codes")["ok"] is False


class FakeDataService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def get_player_by_fid(self, fid):
        self.calls.append(fid)
        return self.result


def test_governor_lookup_validates_and_maps_profile():
    service = FakeDataService(
        {
            "success": True,
            "data": {
                "uid": 1,
                "fid": 279237115,
                "kid": 3,
                "name": "Cassidy",
                "power": 52922243,
                "lv": 30,
                "vip": 5,
                "stove_lv": 27,
                "alliance": {"aid": 1, "abbr": "AQM", "name": "Aquarium"},
                "raw": {"detail": "large"},
            },
        }
    )
    registry = registry_with(make_governor_lookup_tool(service))

    assert run(registry, "lookup_governor", '{"governor_id": "12; drop"}')["ok"] is False
    assert service.calls == []

    data = run(registry, "lookup_governor", '{"governor_id": "279237115"}')["data"]
    assert service.calls == ["279237115"]
    assert data["name"] == "Cassidy" and data["power"] == 52922243
    assert data["alliance"] == {"tag": "AQM", "name": "Aquarium"}
    assert "raw" not in data


def test_governor_lookup_reports_missing_player():
    registry = registry_with(make_governor_lookup_tool(FakeDataService({"success": False, "status_code": 404})))

    result = run(registry, "lookup_governor", '{"governor_id": "123456"}')

    assert result == {"ok": False, "error": "No player was found with that Governor ID."}


def test_knowledge_search_applies_distance_cutoff():
    class FakeRag:
        max_distance = 0.5

        def __init__(self):
            self.calls = []

        async def search(self, query, *, limit, entity_type=None):
            self.calls.append((query, limit, entity_type))
            return [
                SimpleNamespace(name="Amane", entity_type="hero", content="Amane can blind people.", entity_data={}, distance=0.2),
                SimpleNamespace(name="Bear", entity_type="event", content="Unrelated", entity_data={}, distance=0.9),
            ]

    rag = FakeRag()
    registry = registry_with(make_knowledge_search_tool(rag))

    data = run(registry, "search_kingshot_knowledge", '{"query": "what can amane do"}')["data"]

    assert rag.calls == [("what can amane do", 4, None)]
    assert [item["name"] for item in data["results"]] == ["Amane"]
    assert run(registry, "search_kingshot_knowledge", "{}")["ok"] is False


def test_build_registry_only_includes_available_services():
    registry = build_chat_tool_registry(gift_code_service=object(), kingshot_data_service=object())

    assert registry.names == ["list_active_gift_codes", "lookup_governor"]
    assert not build_chat_tool_registry()
