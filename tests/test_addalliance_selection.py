from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

from handlers.gift_code_handler import GiftCodeHandler
from services.kingshot_data_service import KingshotDataService
from services.mightpulse_service import MightPulseUnavailableError


class FakeResponse:
    def __init__(self):
        self.defer_kwargs = None

    async def defer(self, **kwargs):
        self.defer_kwargs = kwargs


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append({"content": content, **kwargs})


class FakeInteraction:
    def __init__(self, *, kid=None):
        self.namespace = SimpleNamespace(kid=kid)
        self.user = SimpleNamespace(
            id=111,
            name="tester",
            discriminator="0",
            display_name="Tester",
        )
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class FakeRankedAllianceService:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def get_ranked_alliances(self, kid, limit=25, *, exhaustive=False):
        self.calls.append((kid, limit, exhaustive))
        if isinstance(self.result, dict) and isinstance(self.result.get("data"), list):
            return {**self.result, "data": self.result["data"][:limit]}
        return self.result


class BlockingRankedAllianceService(FakeRankedAllianceService):
    def __init__(self, result):
        super().__init__(result)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def get_ranked_alliances(self, kid, limit=25, *, exhaustive=False):
        self.calls.append((kid, limit, exhaustive))
        self.started.set()
        await self.release.wait()
        if isinstance(self.result, dict) and isinstance(self.result.get("data"), list):
            return {**self.result, "data": self.result["data"][:limit]}
        return self.result


class ProgressiveRankedAllianceService:
    def __init__(self, partial_result, complete_result):
        self.partial_result = partial_result
        self.complete_result = complete_result
        self.calls = []
        self.completion_started = asyncio.Event()
        self.release_completion = asyncio.Event()

    async def get_ranked_alliances(self, kid, limit=25, *, exhaustive=False):
        self.calls.append((kid, limit, exhaustive))
        if not exhaustive:
            return {
                **self.partial_result,
                "data": self.partial_result.get("data", [])[:limit],
            }

        self.completion_started.set()
        await self.release_completion.wait()
        return {
            **self.complete_result,
            "data": self.complete_result.get("data", [])[:limit],
        }


def _result(*alliances, resolution_complete=True):
    return {
        "success": True,
        "data": list(alliances),
        "resolution_complete": resolution_complete,
    }


def _label(tag, name="Unknown Alliance", member_count="?"):
    return f"[{tag}] {name} - {member_count} members"


def _alliances(count):
    return [
        {
            "aid": 83900000 + index,
            "abbr": f"A{index:02d}",
            "name": f"Alliance {index}",
            "member_count": 90 - index,
            "power": 10_000 - index,
            "rank": index + 1,
        }
        for index in range(count)
    ]


def _build_handler(service, *, bot=None, directory=None):
    return GiftCodeHandler(
        gift_code_service=SimpleNamespace(),
        player_info_service=SimpleNamespace(),
        bot=bot or SimpleNamespace(),
        config=SimpleNamespace(admin_user_ids=[]),
        interaction_tracking_service=SimpleNamespace(),
        player_registry_service=SimpleNamespace(),
        kingshot_data_service=service,
        alliance_directory_service=directory,
    )


@pytest.mark.asyncio
async def test_command_autocompletes_kid_and_alliance_and_prefetches_selected_kingdom():
    service = FakeRankedAllianceService(
        _result(
            {
                "aid": 83900009,
                "abbr": "FKA",
                "name": "Fate Kills All",
                "member_count": 88,
                "power": 200,
                "rank": 1,
            }
        )
    )
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    handler = _build_handler(service, bot=bot)
    handler.ALLIANCE_KID_PREWARM_DEBOUNCE_SECONDS = 0

    try:
        handler.register_commands()
        command = bot.tree.get_command("addalliance")
        assert command is not None
        assert [parameter.name for parameter in command.parameters] == ["kid", "alliance"]
        assert command.parameters[0].type is discord.AppCommandOptionType.integer
        assert command.parameters[0].autocomplete is True
        assert command.parameters[1].type is discord.AppCommandOptionType.string
        assert command.parameters[1].autocomplete is True

        interaction = FakeInteraction(kid=830)
        kid_callback = command._params["kid"].autocomplete
        assert kid_callback is not None
        kid_choices = await kid_callback(interaction, 830)
        assert [(choice.name, choice.value) for choice in kid_choices] == [("830", 830)]

        _, prewarm = handler._alliance_prewarm_tasks[interaction.user.id]
        await prewarm
        refresh = handler._alliance_refresh_tasks.get(830)
        if refresh is not None:
            await refresh

        alliance_callback = command._params["alliance"].autocomplete
        assert alliance_callback is not None
        choices = await alliance_callback(interaction, "")
        assert [(choice.name, choice.value) for choice in choices] == [
            (_label("FKA", "Fate Kills All", 88), "83900009")
        ]
        assert service.calls == [(830, 15, False)]
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_kid_autocomplete_debounces_intermediate_kingdoms_per_user():
    service = BlockingRankedAllianceService(
        _result({"aid": 83900009, "abbr": "FKA", "power": 200, "rank": 1})
    )
    handler = _build_handler(service)
    handler.ALLIANCE_KID_PREWARM_DEBOUNCE_SECONDS = 0.01
    interaction = FakeInteraction()

    for current in (8, 83, 830):
        choices = await handler._get_kid_autocomplete_choices(interaction, current)
        assert choices[0].value == current

    await asyncio.wait_for(service.started.wait(), timeout=0.5)
    assert service.calls == [(830, 15, False)]

    service.release.set()
    refresh = handler._alliance_refresh_tasks[830]
    await refresh


@pytest.mark.asyncio
async def test_autocomplete_requires_positive_kingdom_without_api_call():
    service = FakeRankedAllianceService(_result())
    handler = _build_handler(service)

    for kid in (None, 0, -1, True, "bad"):
        assert await handler._get_alliance_autocomplete_choices(kid, "") == []
    assert service.calls == []


@pytest.mark.asyncio
async def test_autocomplete_orders_by_power_filters_case_insensitively_and_hides_aids():
    service = FakeRankedAllianceService(
        _result(
            {
                "aid": "83900003",
                "abbr": "car",
                "name": "Carpathian Guard",
                "member_count": 73,
                "power": 100,
                "rank": 3,
            },
            {
                "aid": 83900001,
                "abbr": "FKA",
                "name": "Fate Kills All",
                "member_count": 88,
                "power": 900,
                "rank": 1,
            },
            {
                "aid": 83900002,
                "abbr": "aBc",
                "name": "Alpha Beta Coalition",
                "member_count": 64,
                "power": 500,
                "rank": 2,
            },
            {"aid": 0, "abbr": "BAD", "power": 1000, "rank": 0},
            {"aid": 83900004, "abbr": "NO", "power": 800, "rank": 4},
        )
    )
    handler = _build_handler(service)

    choices = await handler._get_alliance_autocomplete_choices(830, "")
    assert [(choice.name, choice.value) for choice in choices] == [
        (_label("FKA", "Fate Kills All", 88), "83900001"),
        ("Alliance 83900004 - ? members", "83900004"),
        (_label("aBc", "Alpha Beta Coalition", 64), "83900002"),
        (_label("car", "Carpathian Guard", 73), "83900003"),
    ]
    assert all(choice.value not in choice.name for choice in choices if choice.value != "83900004")

    by_name = await handler._get_alliance_autocomplete_choices(830, "KILLS")
    assert [(choice.name, choice.value) for choice in by_name] == [
        (_label("FKA", "Fate Kills All", 88), "83900001")
    ]

    by_tag = await handler._get_alliance_autocomplete_choices(830, "ABC")
    assert [(choice.name, choice.value) for choice in by_tag] == [
        (_label("aBc", "Alpha Beta Coalition", 64), "83900002")
    ]


@pytest.mark.asyncio
async def test_named_directory_populates_alliance_choices_without_a_jeab_name():
    directory = SimpleNamespace(get_ranked_alliances=AsyncMock(return_value=_result(
        {"aid": 83900004, "abbr": "KOR", "name": "Serendipity",
         "member_count": 98, "power": 300, "rank": 1},
        {"aid": 83900009, "abbr": "FKA", "name": "FateKillsAll",
         "member_count": 93, "power": 200, "rank": 2},
    )))
    service = FakeRankedAllianceService(_result())
    handler = _build_handler(service, directory=directory)

    choices = await handler._get_alliance_autocomplete_choices(830, "")

    assert [(choice.name, choice.value) for choice in choices] == [
        ("[KOR] Serendipity - 98 members", "83900004"),
        ("[FKA] FateKillsAll - 93 members", "83900009"),
    ]
    assert handler._alliance_cache[830][2] is True
    assert service.calls == []
    directory.get_ranked_alliances.assert_awaited_once_with(830, limit=15)

    handler._handle_add_alliance_slash = AsyncMock()
    interaction = FakeInteraction()
    await handler._handle_add_alliance_choice_slash(interaction, 830, choices[1].value)
    handler._handle_add_alliance_slash.assert_awaited_once_with(
        interaction,
        aid="83900009",
        kid=830,
        defer_response=False,
        alliance_label="FKA",
    )


@pytest.mark.asyncio
async def test_named_directory_failure_falls_back_to_kingshot_leaderboard():
    directory = SimpleNamespace(get_ranked_alliances=AsyncMock(
        side_effect=MightPulseUnavailableError("offline")
    ))
    service = FakeRankedAllianceService(_result(
        {"aid": 83900004, "abbr": None, "name": None, "power": 300, "rank": 1}
    ))
    handler = _build_handler(service, directory=directory)

    choices = await handler._get_alliance_autocomplete_choices(830, "")

    assert [(choice.name, choice.value) for choice in choices] == [
        ("Alliance 83900004 - ? members", "83900004")
    ]
    assert service.calls == [(830, 15, False)]


@pytest.mark.asyncio
async def test_autocomplete_and_selection_work_with_aid_only_alliances():
    service = FakeRankedAllianceService(_result(*[
        {
            "aid": 83900000 + index,
            "abbr": None,
            "name": None,
            "member_count": 90 - index,
            "power": 1000 - index,
            "rank": index,
        }
        for index in range(1, 16)
    ]))
    handler = _build_handler(service)

    choices = await handler._get_alliance_autocomplete_choices(830, "")
    assert len(choices) == 15
    assert choices[0].name == "Alliance 83900001 - 89 members"
    assert choices[0].value == "83900001"
    assert [choice.value for choice in await handler._get_alliance_autocomplete_choices(830, "83900015")] == ["83900015"]

    handler._handle_add_alliance_slash = AsyncMock()
    interaction = FakeInteraction()
    await handler._handle_add_alliance_choice_slash(interaction, 830, choices[0].value)
    handler._handle_add_alliance_slash.assert_awaited_once_with(
        interaction,
        aid="83900001",
        kid=830,
        defer_response=False,
        alliance_label="Alliance 83900001",
    )


@pytest.mark.asyncio
async def test_autocomplete_returns_aids_before_slow_metadata_exceeds_discord_budget():
    class SlowMetadataService(KingshotDataService):
        def __init__(self):
            super().__init__(api_key="test")
            self.release = asyncio.Event()

        async def get_kingdom_board(self, board_type, kid, limit=100, resolve=False):
            await asyncio.sleep(0.02)
            return {"success": True, "data": {"entries": [
                {"rank": rank, "uid": 83900000 + rank, "score": 1000 - rank}
                for rank in range(1, 16)
            ]}}

        async def _resolve_ranked_alliance(self, candidate, kid):
            await self.release.wait()
            return self._ranked_alliance(candidate, "FKA", "Later", 90)

    service = SlowMetadataService()
    handler = _build_handler(service)
    handler.ALLIANCE_AUTOCOMPLETE_WAIT_SECONDS = 0.5

    try:
        choices = await asyncio.wait_for(
            handler._get_alliance_autocomplete_choices(830, ""),
            timeout=0.2,
        )
        assert len(choices) == 15
        assert choices[0].name.startswith("Alliance 83900001")
        assert handler._alliance_cache[830][2] is False
    finally:
        service.release.set()
        completion = handler._alliance_completion_tasks.get(830)
        if completion is not None:
            await completion
        await service.close()


@pytest.mark.asyncio
async def test_choice_labels_sanitize_metadata_use_fallbacks_and_cap_at_100_chars():
    service = FakeRankedAllianceService(
        _result(
            {
                "aid": 83900001,
                "abbr": "FKA",
                "name": "  Fate\n\x00Kills\tAll  ",
                "member_count": "88",
                "power": 300,
                "rank": 1,
            },
            {
                "aid": 83900002,
                "abbr": "LNG",
                "name": "A" * 200,
                "member_count": 83,
                "power": 200,
                "rank": 2,
            },
            {
                "aid": 83900003,
                "abbr": "UNK",
                "name": " \n\t ",
                "member_count": -1,
                "power": 100,
                "rank": 3,
            },
        )
    )
    handler = _build_handler(service)

    choices = await handler._get_alliance_autocomplete_choices(830, "")

    assert choices[0].name == _label("FKA", "Fate Kills All", 88)
    assert len(choices[1].name) == 100
    assert choices[1].name.startswith("[LNG] AAAAA")
    assert choices[1].name.endswith("… - 83 members")
    assert choices[2].name == _label("UNK")


@pytest.mark.asyncio
async def test_autocomplete_returns_top_15_and_reuses_fresh_cache():
    service = FakeRankedAllianceService(
        _result(
            *(
                {
                    "aid": 83900000 + index,
                    "abbr": f"A{index:02d}",
                    "power": 10_000 - index,
                    "rank": index + 1,
                }
                for index in range(30)
            )
        )
    )
    handler = _build_handler(service)

    first = await handler._get_alliance_autocomplete_choices(830, "")
    second = await handler._get_alliance_autocomplete_choices(830, "")
    assert len(first) == 15
    assert [(c.name, c.value) for c in second] == [
        (c.name, c.value) for c in first
    ]
    assert service.calls == [(830, 15, False)]


@pytest.mark.asyncio
async def test_partial_preview_is_served_then_single_completion_upgrades_cache():
    complete_rows = _alliances(15)
    service = ProgressiveRankedAllianceService(
        _result(*complete_rows[:4], resolution_complete=False),
        _result(*complete_rows),
    )
    handler = _build_handler(service)
    handler.ALLIANCE_AUTOCOMPLETE_WAIT_SECONDS = 0.01

    preview = await handler._get_alliance_autocomplete_choices(830, "")
    assert [choice.value for choice in preview] == [
        str(alliance["aid"]) for alliance in complete_rows[:4]
    ]
    assert handler._alliance_cache[830][2] is False
    await asyncio.wait_for(service.completion_started.wait(), timeout=0.5)

    repeated = await handler._get_alliance_autocomplete_choices(830, "")
    assert [choice.value for choice in repeated] == [choice.value for choice in preview]
    assert service.calls == [(830, 15, False), (830, 15, True)]

    service.release_completion.set()
    completion = handler._alliance_completion_tasks[830]
    await completion

    completed = await handler._get_alliance_autocomplete_choices(830, "")
    assert len(completed) == 15
    assert [choice.value for choice in completed] == [
        str(alliance["aid"]) for alliance in complete_rows
    ]
    assert handler._alliance_cache[830][2] is True
    assert service.calls == [(830, 15, False), (830, 15, True)]


@pytest.mark.asyncio
async def test_empty_partial_preview_uses_existing_completion_without_new_preview():
    complete_rows = _alliances(15)
    service = ProgressiveRankedAllianceService(
        _result(resolution_complete=False),
        _result(*complete_rows),
    )
    handler = _build_handler(service)
    handler.ALLIANCE_AUTOCOMPLETE_WAIT_SECONDS = 0.01

    loading = await handler._get_alliance_autocomplete_choices(830, "")
    assert [choice.value for choice in loading] == ["loading:830"]
    await asyncio.wait_for(service.completion_started.wait(), timeout=0.5)
    loading_again = await handler._get_alliance_autocomplete_choices(830, "")
    assert [choice.value for choice in loading_again] == ["loading:830"]
    assert service.calls == [(830, 15, False), (830, 15, True)]

    service.release_completion.set()
    await handler._alliance_completion_tasks[830]
    assert len(await handler._get_alliance_autocomplete_choices(830, "")) == 15


@pytest.mark.asyncio
async def test_complete_short_snapshot_does_not_trigger_exhaustive_retry():
    result = {
        **_result(*_alliances(4)),
        "partial": True,
        "candidate_count": 15,
        "resolved_count": 4,
    }
    service = FakeRankedAllianceService(result)
    handler = _build_handler(service)

    choices = await handler._get_alliance_autocomplete_choices(830, "")

    assert len(choices) == 4
    assert handler._alliance_cache[830][2] is True
    assert 830 not in handler._alliance_completion_tasks
    assert service.calls == [(830, 15, False)]


@pytest.mark.asyncio
async def test_failed_exhaustive_completion_is_cooled_down_while_partial_is_served():
    service = ProgressiveRankedAllianceService(
        _result(*_alliances(4), resolution_complete=False),
        {
            "success": False,
            "error_message": "temporary failure",
        },
    )
    service.release_completion.set()
    handler = _build_handler(service)

    first = await handler._get_alliance_autocomplete_choices(830, "")
    await asyncio.sleep(0)
    second = await handler._get_alliance_autocomplete_choices(830, "")

    assert len(first) == len(second) == 4
    assert handler._alliance_cache[830][2] is False
    assert handler._alliance_completion_retry_after[830] > time.monotonic()
    assert service.calls == [(830, 15, False), (830, 15, True)]


@pytest.mark.asyncio
async def test_stale_cache_is_served_while_single_refresh_runs():
    service = BlockingRankedAllianceService(
        _result({"aid": 83900002, "abbr": "NEW", "power": 200, "rank": 1})
    )
    handler = _build_handler(service)
    handler._alliance_cache[830] = (
        time.monotonic() - handler.ALLIANCE_CACHE_TTL_SECONDS - 1,
        [{"aid": "83900001", "abbr": "OLD", "power": 100, "rank": 1}],
        True,
    )

    stale = await handler._get_alliance_autocomplete_choices(830, "")
    again = await handler._get_alliance_autocomplete_choices(830, "")
    assert [(c.name, c.value) for c in stale] == [
        (_label("OLD"), "83900001")
    ]
    assert [(c.name, c.value) for c in again] == [
        (_label("OLD"), "83900001")
    ]
    await service.started.wait()
    assert service.calls == [(830, 15, True)]

    refresh = handler._alliance_completion_tasks[830]
    service.release.set()
    await refresh
    fresh = await handler._get_alliance_autocomplete_choices(830, "")
    assert [(c.name, c.value) for c in fresh] == [
        (_label("NEW"), "83900002")
    ]


@pytest.mark.asyncio
async def test_concurrent_cold_autocomplete_requests_are_single_flight():
    service = BlockingRankedAllianceService(
        _result({"aid": 83900009, "abbr": "FKA", "power": 200, "rank": 1})
    )
    handler = _build_handler(service)

    first = asyncio.create_task(handler._get_alliance_autocomplete_choices(830, ""))
    await service.started.wait()
    second = asyncio.create_task(handler._get_alliance_autocomplete_choices(830, ""))
    await asyncio.sleep(0)
    service.release.set()

    first_choices, second_choices = await asyncio.gather(first, second)
    assert [(c.name, c.value) for c in first_choices] == [
        (_label("FKA"), "83900009")
    ]
    assert [(c.name, c.value) for c in second_choices] == [
        (_label("FKA"), "83900009")
    ]
    assert service.calls == [(830, 15, False)]


@pytest.mark.asyncio
async def test_autocomplete_timeout_does_not_cancel_shared_lookup(caplog):
    service = BlockingRankedAllianceService(
        _result({"aid": 83900009, "abbr": "FKA", "power": 200, "rank": 1})
    )
    handler = _build_handler(service)
    handler.ALLIANCE_AUTOCOMPLETE_WAIT_SECONDS = 0.01

    loading = await handler._get_alliance_autocomplete_choices(830, "")
    assert [choice.value for choice in loading] == ["loading:830"]
    refresh = handler._alliance_refresh_tasks[830]
    assert not refresh.done()
    assert not refresh.cancelled()
    assert "timed out for kingdom 830" in caplog.text
    assert "lookup continues=True" in caplog.text

    service.release.set()
    await refresh
    cached = await handler._get_alliance_autocomplete_choices(830, "")
    assert [(c.name, c.value) for c in cached] == [
        (_label("FKA"), "83900009")
    ]
    assert service.calls == [(830, 15, False)]


@pytest.mark.asyncio
async def test_successful_empty_alliance_result_is_not_cached():
    service = FakeRankedAllianceService(_result())
    handler = _build_handler(service)

    assert await handler._get_alliance_autocomplete_choices(830, "") == []
    assert 830 not in handler._alliance_cache
    assert await handler._get_alliance_autocomplete_choices(830, "") == []
    assert service.calls == [(830, 15, False), (830, 15, False)]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["83900009", "fka"])
async def test_submission_accepts_hidden_aid_or_exact_manual_tag(selection):
    service = FakeRankedAllianceService(_result())
    handler = _build_handler(service)
    handler._alliance_cache[830] = (
        time.monotonic(),
        [{"aid": "83900009", "abbr": "FKA", "power": 200, "rank": 1}],
        True,
    )
    handler._handle_add_alliance_slash = AsyncMock()
    interaction = FakeInteraction()

    await handler._handle_add_alliance_choice_slash(interaction, 830, selection)
    assert interaction.response.defer_kwargs == {"thinking": True}
    handler._handle_add_alliance_slash.assert_awaited_once_with(
        interaction,
        aid="83900009",
        kid=830,
        defer_response=False,
        alliance_label="FKA",
    )
    assert service.calls == []


@pytest.mark.asyncio
async def test_submission_of_loading_choice_asks_user_to_reopen_list():
    service = FakeRankedAllianceService(_result())
    handler = _build_handler(service)
    handler._handle_add_alliance_slash = AsyncMock()
    interaction = FakeInteraction()

    await handler._handle_add_alliance_choice_slash(interaction, 830, "loading:830")

    handler._handle_add_alliance_slash.assert_not_awaited()
    assert "Still Loading" in interaction.followup.messages[0]["embed"].title
    assert service.calls == []


@pytest.mark.asyncio
async def test_submission_refreshes_stale_cache_for_new_exact_tag():
    service = FakeRankedAllianceService(
        _result({"aid": 83900002, "abbr": "NEW", "power": 200, "rank": 1})
    )
    handler = _build_handler(service)
    handler._alliance_cache[830] = (
        time.monotonic() - handler.ALLIANCE_CACHE_TTL_SECONDS - 1,
        [{"aid": "83900001", "abbr": "OLD", "power": 100, "rank": 1}],
        True,
    )
    handler._handle_add_alliance_slash = AsyncMock()
    interaction = FakeInteraction()

    await handler._handle_add_alliance_choice_slash(interaction, 830, "NEW")
    handler._handle_add_alliance_slash.assert_awaited_once_with(
        interaction,
        aid="83900002",
        kid=830,
        defer_response=False,
        alliance_label="NEW",
    )
    assert service.calls == [(830, 15, True)]


@pytest.mark.asyncio
async def test_submission_waits_for_partial_cache_completion_when_tag_is_missing():
    complete_rows = [
        {"aid": 83900001, "abbr": "OLD", "power": 100, "rank": 2},
        {"aid": 83900002, "abbr": "NEW", "power": 200, "rank": 1},
    ]
    service = ProgressiveRankedAllianceService(
        _result(*complete_rows[:1], resolution_complete=False),
        _result(*complete_rows),
    )
    service.release_completion.set()
    handler = _build_handler(service)
    handler._alliance_cache[830] = (
        time.monotonic(),
        complete_rows[:1],
        False,
    )
    handler._handle_add_alliance_slash = AsyncMock()
    interaction = FakeInteraction()

    await handler._handle_add_alliance_choice_slash(interaction, 830, "NEW")

    handler._handle_add_alliance_slash.assert_awaited_once_with(
        interaction,
        aid="83900002",
        kid=830,
        defer_response=False,
        alliance_label="NEW",
    )
    assert service.calls == [(830, 15, True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["83900999", "MISS", "0", "-1"])
async def test_submission_rejects_values_outside_kingdom_snapshot(selection):
    service = FakeRankedAllianceService(_result())
    handler = _build_handler(service)
    handler._alliance_cache[830] = (
        time.monotonic(),
        [{"aid": "83900009", "abbr": "FKA", "power": 200, "rank": 1}],
        True,
    )
    handler._handle_add_alliance_slash = AsyncMock()
    interaction = FakeInteraction()

    await handler._handle_add_alliance_choice_slash(interaction, 830, selection)
    handler._handle_add_alliance_slash.assert_not_awaited()
    assert service.calls == []
    assert "invalid alliance" in interaction.followup.messages[0]["embed"].title.lower()
