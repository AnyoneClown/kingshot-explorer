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
        # The record is on the attached card; the embed keeps the match list.
        assert embed.image.url == "attachment://kvk-history.png"
        assert [field.name for field in embed.fields] == ["Match History"]
        sent_files = interaction.followup.send.call_args.kwargs["files"]
        assert [file.filename for file in sent_files] == ["kvk-history.png"]
        assert sent_files[0].fp.read(8) == b"\x89PNG\r\n\x1a\n"
        # Without a card the same numbers are text fields.
        fields = {field.name: field.value for field in handler._build_history_embed(830, stats).fields}
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


def _interaction():
    return SimpleNamespace(
        user=SimpleNamespace(id=1, name="Tester", discriminator="0"),
        guild=None,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))),
    )


def test_history_lines_use_clear_win_loss_markers_and_long_timeouts():
    handler = KVKHandler(SimpleNamespace(), object())
    line = handler._format_history_line(
        {"season_id": 13, "season_date": "2026-04-25", "opponent": 892, "side": "defender",
         "castleResult": "win", "prepResult": "loss"}
    )
    assert line.startswith("#13 · 2026-04-25 · vs **K892** · 🛡️ Defense")
    assert "Castle ✅ Win · Prep ❌ Loss" in line
    unknown = handler._format_history_line({"season_id": None, "castleResult": None})
    assert "#? " in unknown and "Castle ❔ Unknown" in unknown and "vs **K?**" in unknown
    assert "Castle ✅ Win" in handler._build_latest_match_summary(
        {"season_id": 13, "opponent": 892, "castleResult": "win", "prepResult": "loss"}
    )

    async def build_view():
        return KVKHistoryView(handler, 830, {"history": []}, 1)

    assert asyncio.run(build_view()).timeout == 900


def test_kvk_commands_use_ranges_and_validate_before_deferring():
    from discord import Intents
    from discord.ext import commands

    bot = commands.Bot(command_prefix="!", intents=Intents.none())
    KVKHandler(SimpleNamespace(), bot, dossier_service=SimpleNamespace()).register_commands()
    group = bot.tree.get_command("kvk")
    for name, parameters in (("history", ["kingdom"]), ("compare", ["kingdom_a", "kingdom_b"]),
                             ("prep", ["kingdom_a", "kingdom_b"])):
        command = group.get_command(name)
        for parameter in parameters:
            option = command.get_parameter(parameter)
            assert (option.min_value, option.max_value) == (1, 99999), (name, parameter)
    assert group.get_command("prep").checks  # per-user cooldown

    plain_bot = commands.Bot(command_prefix="!", intents=Intents.none())
    KVKHandler(SimpleNamespace(), plain_bot).register_commands()
    assert plain_bot.tree.get_command("kvk").get_command("prep") is None
    assert plain_bot.tree.get_command("kvk").get_command("history") is not None

    async def check():
        handler = KVKHandler(SimpleNamespace(get_kingdom_stats=AsyncMock(), compare_kingdoms=AsyncMock()), object(),
                             dossier_service=SimpleNamespace(build_dossier=AsyncMock()))
        cases = [
            (handler._handle_get_kvk_stats_slash, (0,)),
            (handler._handle_get_kvk_stats_slash, (100000,)),
            (handler._handle_compare_kvk_slash, (830, 830)),
            (handler._handle_compare_kvk_slash, (830, -1)),
            (handler._handle_kvk_prep, (830, 830)),
        ]
        for method, args in cases:
            interaction = _interaction()
            await method(interaction, *args)
            interaction.response.defer.assert_not_awaited()
            assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True
        handler._kvk_service.get_kingdom_stats.assert_not_awaited()
        handler._dossier_service.build_dossier.assert_not_awaited()

    asyncio.run(check())


def _dossier(**overrides):
    def ok(data):
        return {"ok": True, "data": data, "error": None}

    castle = {
        "owner_kid": 830, "foreign_owner": False, "king": {"name": "Nyan", "abbr": "TMN", "home_kid": 830,
                                                            "is_foreign": False},
        "recent_reigns": [{"name": "Zero", "abbr": "J4F", "home_kid": 556, "ts": 1762626159,
                           "is_cross_mode": True, "is_invader": True}],
        "top_kings": [{"name": "Nyan", "abbr": "TMN", "king_terms": 18, "high_king_terms": 3}],
        "kvk": {"victories": 5, "last_opponent_kid": 565, "last_ts": 1779549621}, "kvk_included": True,
    }
    rows = [{"rank": index, "name": f"Player {index}", "abbr": "TOP", "score": 1_230_000_000 // index}
            for index in range(1, 6)]
    history = {"matchCount": 3, "wins": 2, "losses": 1, "prepWins": 1, "prepLosses": 2, "currentStreak": "W2",
               "recent": [{"season_id": 13, "opponent": 892, "castleResult": "win", "prepResult": "loss"}]}
    intel = {
        "castle": ok(castle),
        "alliances": ok([{"abbr": "TOP", "name": "Top Alliance", "power": 12_300_000_000}]),
        "top_power": ok(rows), "top_kills": ok(rows), "kvk_history": ok(history),
    }
    intel_b = dict(intel, castle=ok(dict(castle, owner_kid=845, foreign_owner=True)))
    dossier = {
        "kingdom_a": intel, "kingdom_b": intel_b,
        "head_to_head": ok({"match_count": 1, "kingdom_a": {"wins": 0, "losses": 1, "prepWins": 1, "prepLosses": 0},
                            "recent": [{"season_id": 9, "opponent": 831, "castleResult": "loss", "prepResult": "win"}]}),
    }
    dossier.update(overrides)
    return dossier


def test_prep_dossier_embed_is_readable_and_tolerates_missing_sections():
    handler = KVKHandler(SimpleNamespace(), object())
    embed = handler._build_dossier_embed(830, 831, _dossier())
    fields = {field.name: field.value for field in embed.fields}

    assert embed.title == "🛡️ KvK Prep · K830 vs K831"
    assert "K831's castle is held by **K845**" in embed.description
    assert "Met 1× before · K830 castle 0-1" in embed.description
    assert "👑 King: **Nyan `[TMN]`** · K830" in fields["🏰 Kingdom 830 · Castle & throne"]
    assert "⚠️ Castle occupied by K845" in fields["🏰 Kingdom 831 · Castle & throne"]
    assert "Zero `[J4F]` · <t:1762626159:d> · ⚔️ KvK · 🚩 from K556" in fields["🏰 Kingdom 830 · Castle & throne"]
    assert fields["🤝 K830 top alliances"] == "1. `[TOP]` Top Alliance · 12.3B"
    assert fields["💪 K830 top power"].startswith("1. `[TOP]` Player 1 · 1.23B")
    assert "Castle ❌ Loss · Prep ✅ Win" in fields["📜 KvK record"]
    assert len(embed.fields) <= 25 and len(embed) <= 6000

    broken = {"ok": False, "data": None, "error": "game gateway error"}
    partial = _dossier(head_to_head=broken)
    partial["kingdom_a"] = dict(partial["kingdom_a"], castle=broken, alliances=broken, kvk_history=broken)
    partial["kingdom_b"] = dict(partial["kingdom_b"], top_kills={"ok": True, "data": [], "error": None})
    embed = handler._build_dossier_embed(830, 831, partial)
    fields = {field.name: field.value for field in embed.fields}
    assert fields["🏰 Kingdom 830 · Castle & throne"] == "Unavailable (game gateway error)"
    assert fields["🤝 K830 top alliances"] == "Unavailable (game gateway error)"
    assert fields["🗡️ K831 top kills"] == "No ranking returned"
    assert "Direct match history unavailable" in embed.description

    huge = _dossier()
    long_row = {"rank": 1, "name": "N" * 500, "abbr": "TOP", "score": 1}
    for intel in (huge["kingdom_a"], huge["kingdom_b"]):
        intel["top_power"] = {"ok": True, "data": [long_row] * 100, "error": None}
        intel["castle"]["data"]["recent_reigns"] = [{"name": "@everyone" + "x" * 500, "ts": 1}] * 50
    embed = handler._build_dossier_embed(830, 831, huge)
    assert len(embed) <= 6000 and all(len(field.value) <= 1024 for field in embed.fields)
    assert "@everyone" not in str(embed.fields[0].value)


def test_compare_panel_offers_public_prep_dossier():
    async def check():
        summary = {"history": [], "matchCount": 0, "wins": 0, "losses": 0}
        service = SimpleNamespace(compare_kingdoms=AsyncMock(return_value={"success": True, "data": {
            "kingdom_a": summary, "kingdom_b": summary, "score": {"830": 0, "831": 0},
            "head_to_head": {"available": True, "matches": [], "kingdom_a": summary, "kingdom_b": summary},
        }}))
        dossier_service = SimpleNamespace(build_dossier=AsyncMock(return_value=_dossier()))
        handler = KVKHandler(service, object(), dossier_service=dossier_service)
        interaction = _interaction()
        await handler._handle_compare_kvk_slash(interaction, 830, 831)
        view = interaction.followup.send.call_args.kwargs["view"]
        assert view.timeout == 900
        assert view.dossier_button is not None and view.dossier_button in view.children
        assert "Prep dossier posts a new message" in view.build_embed().footer.text

        click = _interaction()
        await view.dossier_button.callback(click)
        dossier_service.build_dossier.assert_awaited_once_with(830, 831)
        click.response.defer.assert_awaited_once_with(thinking=True)
        sent = click.followup.send.call_args
        first, second = sent.kwargs["embeds"]
        assert first.title == "🛡️ KvK Prep · K830 vs K831"
        # Rankings are two picture cards; castle and record stay as text.
        assert first.image.url == "attachment://kvk-prep-alliances.png"
        assert second.image.url == "attachment://kvk-prep-players.png"
        assert [file.filename for file in sent.kwargs["files"]] == ["kvk-prep-alliances.png", "kvk-prep-players.png"]
        assert not any("top alliances" in field.name or "top power" in field.name for field in first.fields)
        assert any("Castle & throne" in field.name for field in first.fields)
        assert second.footer.text.startswith("Cached up to 10 min") and not first.footer.text
        assert "ephemeral" not in sent.kwargs

        plain = KVKHandler(service, object())
        plain_interaction = _interaction()
        await plain._handle_compare_kvk_slash(plain_interaction, 830, 831)
        assert plain_interaction.followup.send.call_args.kwargs["view"].dossier_button is None

    asyncio.run(check())
