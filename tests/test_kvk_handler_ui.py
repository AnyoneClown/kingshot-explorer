import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from handlers.kvk_handler import KVKHandler, KVKHistoryView
from handlers.ui import EmbedColors


def test_kvk_history_pagination_comparison_and_missing_values():
    async def check():
        history = [
            {"season_id": index, "season_date": "2026-09-05", "opponent": 831, "castleResult": "win", "prepResult": "loss"}
            for index in range(13)
        ]
        stats = {"history": history, "matchCount": 13, "wins": 0, "losses": 13, "winRate": 0}
        service = SimpleNamespace(get_kingdom_stats=AsyncMock(return_value={"success": True, "data": stats}))
        handler = KVKHandler(service, object())
        message = SimpleNamespace(edit=AsyncMock())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1, name="Tester", discriminator="0"),
            guild=None,
            response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock(return_value=message)),
        )
        await handler._handle_get_kvk_stats_slash(interaction, 830)
        view = interaction.followup.send.call_args.kwargs["view"]
        assert len(view.pages) == 3 and sum(map(len, view.pages)) == 13
        embed = view.build_embed()
        assert embed.color == EmbedColors.INFO
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Castle Record"] == "0-13"
        assert fields["Castle Win Rate"] == "0.00%"
        assert fields["Prep Record"] == "Unavailable" and fields["Prep Win Rate"] == "Unavailable"
        assert view.previous_button.disabled and not view.next_button.disabled
        await view.next_button.callback(interaction)
        await view.next_button.callback(interaction)
        assert view.next_button.disabled and not view.previous_button.disabled
        assert "#12 " in view.build_embed().fields[-1].value
        await view.previous_button.callback(interaction)
        assert view.current_page == 1
        await view.compare_button.callback(interaction)
        modal = interaction.response.send_modal.call_args.args[0]
        assert modal.timeout == 180
        stranger = SimpleNamespace(user=SimpleNamespace(id=2), response=SimpleNamespace(send_message=AsyncMock()))
        assert not await view.interaction_check(stranger)
        assert not await modal.interaction_check(stranger)
        handler._handle_compare_kvk_slash = AsyncMock()
        modal.opponent._value = "bad"
        await modal.on_submit(interaction)
        handler._handle_compare_kvk_slash.assert_not_awaited()
        modal.opponent._value = "830"
        await modal.on_submit(interaction)
        handler._handle_compare_kvk_slash.assert_not_awaited()
        modal.opponent._value = " 831 "
        await modal.on_submit(interaction)
        handler._handle_compare_kvk_slash.assert_awaited_once_with(interaction, 830, 831)
        assert handler._win_rate({"wins": 0, "losses": 0, "winRate": 0}) == "No decided matches"
        empty = KVKHistoryView(handler, 830, {"history": [], "matchCount": 0}, 1)
        assert empty.previous_button.disabled and empty.next_button.disabled
        assert "No matches" in empty.build_embed().fields[-1].value
        oversized = KVKHistoryView(handler, 830, {"history": [{"season_date": "x" * 2000}] * 20}, 1)
        assert len(oversized.build_embed().fields[-1].value) <= 1024
        assert len(oversized.build_embed()) <= 6000
        await view.on_timeout()
        assert all(item.disabled for item in view.children)
        message.edit.assert_awaited_once()

    asyncio.run(check())


def test_comparison_direct_history_has_all_matches():
    async def check():
        history = [{"season_id": index, "opponent": 831} for index in range(12)]
        summary = {"history": history, "matchCount": 12, "wins": 12, "losses": 0, "winRate": 100}
        service = SimpleNamespace(compare_kingdoms=AsyncMock(return_value={
            "success": True,
            "data": {
                "kingdom_a": summary,
                "kingdom_b": summary,
                "score": {"830": 0, "831": 0},
                "head_to_head": {"available": True, "matches": history, "kingdom_a": summary, "kingdom_b": summary},
            },
        }))
        handler = KVKHandler(service, object())
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=1),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await handler._handle_compare_kvk_slash(interaction, 830, 831)
        view = interaction.followup.send.call_args.kwargs["view"]
        assert sum(map(len, view.pages)) == 12
        assert view.build_embed().fields[-1].name == "Direct Match History"
        assert "Dead heat" in view.build_embed().description

    asyncio.run(check())
