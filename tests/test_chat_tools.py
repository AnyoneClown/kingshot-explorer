import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from services.chat_tools import (
    ChatTool,
    ChatToolRegistry,
    ToolContext,
    build_chat_tool_registry,
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


def test_knowledge_search_falls_back_to_unfiltered_when_the_filter_misses():
    class FakeRag:
        max_distance = 0.7

        def __init__(self):
            self.calls = []

        async def search(self, query, *, limit, entity_type=None):
            self.calls.append(entity_type)
            if entity_type == "building":  # the model guessed the wrong type
                return [SimpleNamespace(chunk_id="b1", name="Embassy", entity_type="building", content="Embassy",
                                        entity_data={}, distance=0.68)]
            return [
                SimpleNamespace(chunk_id="g1", name="Governor Gear", entity_type="governor_gear_overview",
                                content="Mythic satin", entity_data={}, distance=0.35),
                SimpleNamespace(chunk_id="b1", name="Embassy", entity_type="building", content="Embassy",
                                entity_data={}, distance=0.68),
            ]

    rag = FakeRag()
    registry = registry_with(make_knowledge_search_tool(rag))

    data = run(registry, "search_kingshot_knowledge", '{"query": "satin to mythic", "entity_type": "building"}')["data"]

    assert rag.calls == ["building", None]
    assert [item["name"] for item in data["results"]] == ["Governor Gear", "Embassy"]


def test_knowledge_search_keeps_a_good_filtered_result_without_a_second_search():
    class FakeRag:
        max_distance = 0.7

        def __init__(self):
            self.calls = []

        async def search(self, query, *, limit, entity_type=None):
            self.calls.append(entity_type)
            return [SimpleNamespace(chunk_id="h1", name="Ava", entity_type="hero", content="Ava skills",
                                    entity_data={}, distance=0.3)]

    rag = FakeRag()
    registry = registry_with(make_knowledge_search_tool(rag))

    data = run(registry, "search_kingshot_knowledge", '{"query": "ava skills", "entity_type": "hero"}')["data"]

    assert rag.calls == ["hero"]
    assert [item["name"] for item in data["results"]] == ["Ava"]


def test_event_calendar_tool_uses_the_servers_kingdom():
    from datetime import datetime, timedelta, timezone

    from services.chat_tools import make_event_calendar_tool
    from services.kingshot_calendar_service import (
        CalendarEvent,
        CalendarStatus,
        CalendarUnavailableError,
        KingdomCalendar,
        KingdomNotTrackedError,
    )

    now = datetime(2026, 11, 11, 8, tzinfo=timezone.utc)

    def event(title, category, start_days, end_days, **kwargs):
        return CalendarEvent(
            key=title,
            title=title,
            category=category,
            start=now + timedelta(days=start_days),
            end=now + timedelta(days=end_days),
            **kwargs,
        )

    class FakeCalendarService:
        def __init__(self):
            self.calls = []
            self.error = None

        async def get_calendar(self, kingdom):
            self.calls.append(kingdom)
            if self.error is not None:
                raise self.error
            return KingdomCalendar(
                kingdom=kingdom,
                fetched_at=now,
                current_week=1,
                events=(
                    event("Sanctuary Battle", "WAR", -1, 3, is_main=True),
                    event("Enhance Gear", "PACK", -2, 5),
                    event("Old Event", "GROWTH", -5, -1),
                    event("Mystic Divination", "DEALS", 2, 4, estimated=True),
                    event("Far Away", "SPECIAL", 30, 31),
                ),
                kvk=CalendarStatus(
                    kind="kvk", phase="countdown", phase_name="Next KvK", number=21, target=now + timedelta(days=18)
                ),
                transfer=None,
            )

    class FakeSettings:
        def __init__(self, kingdom):
            self.kingdom = kingdom

        async def get_settings(self, guild_id):
            assert guild_id == CONTEXT.guild_id
            return SimpleNamespace(kingdom_id=self.kingdom)

    service = FakeCalendarService()
    registry = registry_with(make_event_calendar_tool(service, FakeSettings(795), now_provider=lambda: now))

    data = run(registry, "get_event_calendar", "{}")["data"]

    assert service.calls == [795]
    assert data["kingdom"] == 795
    assert data["kvk"]["number"] == 21 and data["kvk"]["phase_changes_utc"] == "2026-11-29T08:00+00:00"
    assert data["transfer"] is None
    assert [row["title"] for row in data["running_now"]] == ["Sanctuary Battle"]
    assert data["running_now"][0]["ends_utc"] == "2026-11-14T08:00+00:00"
    assert [(row["title"], row["date_confirmed"]) for row in data["upcoming"]] == [("Mystic Divination", False)]

    assert run(registry, "get_event_calendar", '{"kingdom": 830}')["data"]["kingdom"] == 830
    assert service.calls == [795, 830]

    service.error = KingdomNotTrackedError(4242)
    missing = run(registry, "get_event_calendar", '{"kingdom": 4242}')
    assert missing["ok"] is False and "not on the kingshot.net calendar" in missing["error"]
    service.error = CalendarUnavailableError("down")
    assert "unavailable" in run(registry, "get_event_calendar", "{}")["error"]

    unset = registry_with(make_event_calendar_tool(service, FakeSettings(None), now_provider=lambda: now))
    assert "No kingdom is set" in run(unset, "get_event_calendar", "{}")["error"]


def test_registry_includes_the_calendar_tool_when_the_service_is_available():
    registry = build_chat_tool_registry(calendar_service=object(), guild_configuration_service=object())

    assert registry.names == ["get_event_calendar"]
