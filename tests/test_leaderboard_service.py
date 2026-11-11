import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.leaderboard_service import (
    BOARDS,
    BOARDS_BY_TYPE,
    LeaderboardService,
    guessed_alliance_kingdom,
    town_center_label,
)


def ok(entries, **extra):
    return {"success": True, "status_code": 200, "data": {"entries": entries, **extra}}


def player(rank, uid, score, name="", alliance=None):
    return {"rank": rank, "uid": uid, "score": score, "name": name, "alliance": alliance}


def data_service(**methods):
    defaults = {
        "get_kingdom_board": AsyncMock(return_value=ok([])),
        "get_global_board": AsyncMock(return_value=ok([])),
        "get_alliance_identity": AsyncMock(return_value=None),
    }
    return SimpleNamespace(**{**defaults, **methods})


def test_catalog_has_unique_boards_that_fit_a_discord_menu():
    assert len({board.type for board in BOARDS}) == len(BOARDS) <= 25
    assert len({board.name for board in BOARDS}) == len(BOARDS)
    assert [board.type for board in BOARDS if board.alliance] == [1, 2]


@pytest.mark.parametrize(
    ("level", "label"),
    [(1, "1"), (30, "30"), (31, "30-1"), (34, "30-4"), (35, "TG1"), (38, "TG1-3"), (65, "TG7"), (70, "TG8")],
)
def test_town_center_levels_use_truegold_names(level, label):
    assert town_center_label(level) == label


def test_scores_are_read_the_way_the_game_shows_them():
    spire, trial, power, town = (BOARDS_BY_TYPE[key] for key in (26, 20, 3, 5))

    # Tower boards carry their tower in the score.
    assert spire.value(60753) == 753 and spire.text(753) == "753"
    assert trial.value(3019) == 3019 and trial.text(3019) == "3,019"
    assert power.text(power.value(772_548_633)) == "772.55M"
    assert town.text(town.value(70)) == "TG8"
    assert power.value(None) is None and power.value("n/a") is None and power.text(None) == "–"


def test_alliance_kingdom_is_read_from_the_alliance_id():
    assert guessed_alliance_kingdom(83_900_004) == 830
    assert guessed_alliance_kingdom(24_900_063) == 240
    assert guessed_alliance_kingdom(300_001) is None


@pytest.mark.asyncio
async def test_player_board_is_resolved_and_cached():
    now = [1000.0]
    data = data_service(
        get_kingdom_board=AsyncMock(
            return_value=ok(
                [
                    player(1, 11, 3019, "Mila", {"aid": 83900004, "abbr": "KOR", "name": "Serendipity"}),
                    player(2, 12, "2986", "  Chii  "),
                    "not an entry",
                    {"uid": 13, "score": None},
                ]
            )
        )
    )
    service = LeaderboardService(data, clock=lambda: now[0])

    snapshot = await service.get_board(BOARDS_BY_TYPE[20], 830)

    data.get_kingdom_board.assert_awaited_once_with(20, 830, limit=100, resolve=True)
    assert snapshot.title == "Kingdom 830 · Mystic Trial" and not snapshot.error
    assert [(entry.rank, entry.name, entry.tag, entry.text) for entry in snapshot.entries] == [
        (1, "Mila", "KOR", "3,019"),
        (2, "Chii", "", "2,986"),
        # A missing rank falls back to the entry's position.
        (4, "", "", "–"),
    ]
    assert snapshot.top_value == 3019

    assert await service.get_board(BOARDS_BY_TYPE[20], 830) is snapshot
    now[0] += LeaderboardService.CACHE_SECONDS + 1
    assert await service.get_board(BOARDS_BY_TYPE[20], 830) is not snapshot
    assert data.get_kingdom_board.await_count == 2


@pytest.mark.asyncio
async def test_failures_and_empty_boards_explain_themselves_and_are_not_cached():
    data = data_service(
        get_kingdom_board=AsyncMock(
            side_effect=[
                {"success": False, "error_message": "HTTP 502"},
                RuntimeError("boom"),
                ok([], error="gateway refusal", code=-1),
                ok([]),
            ]
        )
    )
    service = LeaderboardService(data)
    board = BOARDS_BY_TYPE[3]

    failed = await service.get_board(board, 830)
    crashed = await service.get_board(board, 830)
    refused = await service.get_board(board, 99999)
    empty = await service.get_board(board, 830)

    assert "could not be loaded" in failed.error and "could not be loaded" in crashed.error
    assert "kingdom 99999" in refused.error
    assert "no entries" in empty.error
    assert data.get_kingdom_board.await_count == 4


@pytest.mark.asyncio
async def test_kingdom_alliances_are_named_by_top_players_then_looked_up():
    async def kingdom_board(board_type, kid, limit, resolve):
        if board_type == 1:
            return ok([{"rank": 1, "uid": 83900004, "score": 35_780_820_564}, {"rank": 2, "uid": 83900212, "score": 9},
                       {"rank": 3, "uid": 83900999, "score": 5}])
        if board_type == 3:
            return ok([player(1, 11, 5, "Mila", {"aid": 83900004, "abbr": "KOR", "name": "Serendipity"})])
        return ok([])

    async def identity(aid, kid):
        return {"aid": aid, "abbr": "FFL", "name": "FireLeague"} if aid == 83900212 else None

    data = data_service(
        get_kingdom_board=AsyncMock(side_effect=kingdom_board),
        get_alliance_identity=AsyncMock(side_effect=identity),
    )
    service = LeaderboardService(data)

    snapshot = await service.get_board(BOARDS_BY_TYPE[1], 830)
    # Alliance ids must not be resolved as players.
    assert data.get_kingdom_board.await_args_list[0].kwargs["resolve"] is False
    assert [entry.name for entry in snapshot.entries] == ["", "", ""]

    await service.name_alliances(snapshot, snapshot.entries)

    assert [(entry.tag, entry.name, entry.text) for entry in snapshot.entries] == [
        ("KOR", "Serendipity", "35.78B"),
        ("FFL", "FireLeague", "9"),
        ("", "", "5"),
    ]
    # Only the alliances the player boards did not name are looked up, in this kingdom.
    assert sorted(call.args for call in data.get_alliance_identity.await_args_list) == [
        (83900212, 830),
        (83900999, 830),
    ]
    # A second pass asks for nothing: found or not, every lookup is remembered.
    await service.name_alliances(snapshot, snapshot.entries)
    assert data.get_alliance_identity.await_count == 2
    # The names are reused by other alliance boards.
    data.get_kingdom_board = AsyncMock(return_value=ok([{"rank": 1, "uid": 83900212, "score": 7}]))
    kills = await service.get_board(BOARDS_BY_TYPE[2], 830)
    assert (kills.entries[0].tag, kills.entries[0].lookup_done) == ("FFL", True)


@pytest.mark.asyncio
async def test_global_alliances_are_looked_up_in_the_kingdom_of_their_id():
    data = data_service(
        get_global_board=AsyncMock(
            side_effect=lambda board_type, limit, resolve: ok(
                [{"rank": 1, "uid": 24900063, "score": 10}, {"rank": 2, "uid": 12, "score": 5}] if board_type == 1 else []
            )
        ),
        get_alliance_identity=AsyncMock(return_value={"abbr": "DRX", "name": "Dragonix"}),
    )
    service = LeaderboardService(data)

    snapshot = await service.get_board(BOARDS_BY_TYPE[1], None)
    await service.name_alliances(snapshot, snapshot.entries)

    assert snapshot.scope == "All kingdoms"
    data.get_alliance_identity.assert_awaited_once_with(24900063, 240)
    assert [(entry.tag, entry.lookup_done) for entry in snapshot.entries] == [("DRX", True), ("", True)]


@pytest.mark.asyncio
async def test_slow_alliance_lookups_do_not_hold_the_page_and_are_retried():
    started = asyncio.Event()

    async def identity(aid, kid):
        started.set()
        await asyncio.sleep(60)

    data = data_service(
        get_kingdom_board=AsyncMock(
            side_effect=lambda board_type, kid, limit, resolve: ok(
                [{"rank": 1, "uid": 83900004, "score": 10}] if board_type == 1 else []
            )
        ),
        get_alliance_identity=AsyncMock(side_effect=identity),
    )
    service = LeaderboardService(data)
    service.ALLIANCE_LOOKUP_SECONDS = 0.01

    snapshot = await service.get_board(BOARDS_BY_TYPE[1], 830)
    await service.name_alliances(snapshot, snapshot.entries)

    assert started.is_set()
    assert not snapshot.entries[0].lookup_done and snapshot.entries[0].name == ""
    await service.name_alliances(snapshot, snapshot.entries)
    assert data.get_alliance_identity.await_count == 2


@pytest.mark.asyncio
async def test_player_boards_need_no_alliance_naming():
    data = data_service(get_kingdom_board=AsyncMock(return_value=ok([player(1, 11, 5, "Mila")])))
    service = LeaderboardService(data)

    snapshot = await service.get_board(BOARDS_BY_TYPE[3], 830)
    await service.name_alliances(snapshot, snapshot.entries)

    data.get_kingdom_board.assert_awaited_once()
    data.get_alliance_identity.assert_not_awaited()
