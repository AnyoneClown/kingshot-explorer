from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from handlers.leaderboard_handler import PAGE_SIZE, LeaderboardHandler, LeaderboardView, entry_name
from services.leaderboard_service import BOARDS, BOARDS_BY_TYPE, BoardEntry, BoardSnapshot


class FakeLeaderboardService:
    """Boards of 40 players or 3 alliances; alliances are named on request."""

    def __init__(self, error=""):
        self.error = error
        self.requests = []
        self.named = []

    async def get_board(self, board, kingdom):
        self.requests.append((board.type, kingdom))
        if self.error:
            return BoardSnapshot(board=board, kingdom=kingdom, error=self.error)
        count = 3 if board.alliance else 40
        entries = [
            BoardEntry(
                rank=index + 1,
                id=1000 + index,
                value=float(1000 - index),
                text=board.text(float(1000 - index)),
                name="" if board.alliance else f"Player {index + 1}",
                tag="" if board.alliance else "KOR",
            )
            for index in range(count)
        ]
        return BoardSnapshot(board=board, kingdom=kingdom, entries=entries)

    async def name_alliances(self, snapshot, entries):
        self.named.append([entry.rank for entry in entries])
        if snapshot.board.alliance:
            entries[0].tag, entries[0].name = "FFL", "FireLeague"


def interaction(guild_id=5):
    return SimpleNamespace(
        user=SimpleNamespace(id=1),
        guild_id=guild_id,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))),
        edit_original_response=AsyncMock(),
        delete_original_response=AsyncMock(),
    )


def handler(service=None, kingdom=None):
    settings = SimpleNamespace(get_settings=AsyncMock(return_value=SimpleNamespace(kingdom_id=kingdom)))
    return LeaderboardHandler(service or FakeLeaderboardService(), object(), guild_configuration_service=settings)


async def choose(view, ctx, board_type):
    view.board_select._values = [str(board_type)]
    await view.board_select.callback(ctx)


@pytest.mark.asyncio
async def test_leaderboard_posts_a_public_card_and_pages_through_the_board():
    service = FakeLeaderboardService()
    ctx = interaction()

    await handler(service)._handle_leaderboard(ctx, None, 830, None)

    ctx.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
    ctx.delete_original_response.assert_awaited_once()
    sent = ctx.followup.send.call_args.kwargs
    view = sent["view"]
    assert service.requests == [(3, 830)]
    assert sent["embed"].title == "🏆 Kingdom 830 · Personal Power"
    assert [file.filename for file in sent["files"]] == ["leaderboard.png"]
    assert sent["embed"].image.url == "attachment://leaderboard.png"
    assert sent["embed"].footer.text == "Ranks 1–15 of 40 · power · Page 1/3"
    assert view.message is ctx.followup.send.return_value
    assert view.previous_button.disabled and not view.next_button.disabled
    assert view.scope_button.label == "All kingdoms" and not view.scope_button.disabled
    assert [option.value for option in view.board_select.options] == [str(board.type) for board in BOARDS]
    assert [option.label for option in view.board_select.options if option.default] == ["Personal Power"]
    first = view.rows()[0]
    assert (first.rank, first.label, first.text) == (1, "[KOR] Player 1", "1K")

    await view.next_button.callback(ctx)
    await view.next_button.callback(ctx)

    # Paging draws the next ranks without asking the API again.
    assert service.requests == [(3, 830)]
    edited = ctx.edit_original_response.call_args.kwargs
    assert edited["embed"].footer.text == "Ranks 31–40 of 40 · power · Page 3/3"
    assert [file.filename for file in edited["attachments"]] == ["leaderboard.png"]
    assert [row.rank for row in view.rows()] == list(range(31, 41))
    assert view.next_button.disabled and not view.previous_button.disabled

    await view.previous_button.callback(ctx)
    assert view.page == 1 and len(view.rows()) == PAGE_SIZE


@pytest.mark.asyncio
async def test_board_and_scope_can_be_switched_in_the_panel():
    service = FakeLeaderboardService()
    ctx = interaction()
    await handler(service)._handle_leaderboard(ctx, 20, 830, None)
    view = ctx.followup.send.call_args.kwargs["view"]
    await view.next_button.callback(ctx)

    await choose(view, ctx, 1)

    # A new board starts on its first page, and its alliances are named.
    assert service.requests[-1] == (1, 830) and view.page == 0
    assert service.named[-1] == [1, 2, 3]
    assert [row.label for row in view.rows()] == ["[FFL] FireLeague", "Alliance 1001", "Alliance 1002"]
    assert ctx.edit_original_response.call_args.kwargs["embed"].title == "🏆 Kingdom 830 · Alliance Power"
    assert view.previous_button.disabled and view.next_button.disabled

    await view.scope_button.callback(ctx)

    assert service.requests[-1] == (1, None)
    assert ctx.edit_original_response.call_args.kwargs["embed"].title == "🏆 All kingdoms · Alliance Power"
    assert view.scope_button.label == "Kingdom 830"

    await view.scope_button.callback(ctx)
    assert service.requests[-1] == (1, 830) and view.scope_button.label == "All kingdoms"


@pytest.mark.asyncio
async def test_kingdom_defaults_to_the_server_setting_and_is_asked_for_without_one():
    service = FakeLeaderboardService()
    ctx = interaction()
    await handler(service, kingdom=795)._handle_leaderboard(ctx)
    assert service.requests == [(3, 795)]

    # The global board needs no kingdom; without one its panel cannot switch to a kingdom.
    ctx = interaction(guild_id=None)
    await handler(service)._handle_leaderboard(ctx, 4, None, "global")
    view = ctx.followup.send.call_args.kwargs["view"]
    assert service.requests[-1] == (4, None)
    assert view.scope_button.disabled and view.scope_button.label == "Kingdom"

    # With the server's kingdom known, the global panel can switch to it.
    ctx = interaction()
    await handler(service, kingdom=795)._handle_leaderboard(ctx, 4, None, "global")
    view = ctx.followup.send.call_args.kwargs["view"]
    assert service.requests[-1] == (4, None)
    assert view.scope_button.label == "Kingdom 795" and not view.scope_button.disabled

    ctx = interaction()
    await handler(service)._handle_leaderboard(ctx)
    ctx.followup.send.assert_not_awaited()
    asked = ctx.edit_original_response.call_args.kwargs["embed"]
    assert asked.title == "Which kingdom?" and "/settings" in asked.description


@pytest.mark.asyncio
async def test_a_board_that_cannot_be_shown_says_why_and_keeps_the_menu():
    service = FakeLeaderboardService(error="This board has no entries right now.")
    ctx = interaction()

    await handler(service)._handle_leaderboard(ctx, 26, 830, None)

    sent = ctx.followup.send.call_args.kwargs
    assert sent["files"] == []
    assert sent["embed"].description == "This board has no entries right now."
    view = sent["view"]
    assert view.previous_button.disabled and view.next_button.disabled
    assert len(view.board_select.options) == len(BOARDS)


@pytest.mark.asyncio
async def test_the_ranking_is_text_when_the_card_cannot_be_drawn(monkeypatch):
    async def no_card(*args, **kwargs):
        return None

    monkeypatch.setattr("handlers.leaderboard_handler.render_card", no_card)
    ctx = interaction()

    await handler()._handle_leaderboard(ctx, 3, 830, None)

    sent = ctx.followup.send.call_args.kwargs
    assert sent["files"] == []
    lines = sent["embed"].description.splitlines()
    assert lines[0] == "**#1** `[KOR]` Player 1 · 1K" and len(lines) == PAGE_SIZE


@pytest.mark.asyncio
async def test_only_the_requester_controls_the_panel_and_not_while_it_loads():
    view = LeaderboardView(handler(), 1, board=BOARDS_BY_TYPE[3], kingdom=830, global_scope=False)
    stranger = interaction()
    stranger.user.id = 2
    assert not await view.interaction_check(stranger)

    owner = interaction()
    view._loading = True
    assert not await view.interaction_check(owner)
    assert "loading" in owner.response.send_message.call_args.args[0].lower()
    view._loading = False
    assert await view.interaction_check(owner)


def test_rows_without_a_name_show_what_is_known():
    power, alliances = BOARDS_BY_TYPE[3], BOARDS_BY_TYPE[1]
    assert entry_name(power, BoardEntry(1, 77, 1.0, "1")) == "Player 77"
    assert entry_name(power, BoardEntry(1, None, 1.0, "1")) == "Unknown player"
    assert entry_name(alliances, BoardEntry(1, 83900004, 1.0, "1")) == "Alliance 83900004"
    assert entry_name(alliances, BoardEntry(1, 83900004, 1.0, "1", tag="KOR")) == "Alliance"
    assert entry_name(alliances, BoardEntry(1, 83900004, 1.0, "1", name="Serendipity", tag="KOR")) == "Serendipity"
