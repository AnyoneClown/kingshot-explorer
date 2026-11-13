import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from handlers.kvk_handler import KVKCompareView, KVKHandler
from handlers.ui import EmbedColors


def _interaction(guild_id=None):
    return SimpleNamespace(
        user=SimpleNamespace(id=1, name="Tester", discriminator="0"),
        guild=SimpleNamespace(id=guild_id) if guild_id else None,
        guild_id=guild_id,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock(), edit_message=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))),
        edit_original_response=AsyncMock(),
        delete_original_response=AsyncMock(),
    )


def _settings(kingdom):
    return SimpleNamespace(get_settings=AsyncMock(return_value=SimpleNamespace(kingdom_id=kingdom)))


def _history(count):
    return [
        {"season_id": count - index, "season_date": "2026-09-05", "opponent": 831, "castleResult": "win",
         "prepResult": "loss"}
        for index in range(count)
    ]


def _comparison(history=()):
    history = list(history)
    summary = {"history": history, "matchCount": len(history), "wins": len(history), "losses": 0, "winRate": 100,
               "prepWins": 0, "prepLosses": len(history)}
    return {"success": True, "data": {
        "kingdom_a": summary, "kingdom_b": summary, "score": {"830": 0, "831": 0},
        "head_to_head": {"available": True, "matches": history, "kingdom_a": summary, "kingdom_b": summary},
    }}


def test_history_is_one_card_without_controls():
    async def check():
        history = _history(13)
        stats = {"history": history, "latestMatch": history[0], "matchCount": 13, "wins": 0, "losses": 13, "winRate": 0}
        service = SimpleNamespace(get_kingdom_stats=AsyncMock(return_value={"success": True, "data": stats}))
        handler = KVKHandler(service, object(), dossier_service=SimpleNamespace())
        interaction = _interaction()
        await handler._handle_kvk(interaction, 830)

        service.get_kingdom_stats.assert_awaited_once_with(830)
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        sent = interaction.followup.send.call_args.kwargs
        # The record and the last ten seasons are on the card: no buttons, no paged text.
        assert "view" not in sent
        assert [file.filename for file in sent["files"]] == ["kvk-history.png"]
        assert sent["files"][0].fp.read(8) == b"\x89PNG\r\n\x1a\n"
        embed = sent["embed"]
        assert embed.color == EmbedColors.INFO
        assert embed.title == "⚔️ KVK History · Kingdom 830"
        assert embed.image.url == "attachment://kvk-history.png"
        assert "Latest: **KvK #13** vs **K831**" in embed.description
        assert not embed.fields
        assert embed.footer.text == "13 matches tracked · Source: Kingshot KVK"

    asyncio.run(check())


def test_history_without_a_card_is_text_with_the_last_ten_matches(monkeypatch):
    async def check():
        history = _history(13)
        stats = {"history": history, "latestMatch": history[0], "matchCount": 13, "wins": 0, "losses": 13, "winRate": 0}
        service = SimpleNamespace(get_kingdom_stats=AsyncMock(return_value={"success": True, "data": stats}))
        handler = KVKHandler(service, object())

        def broken(*args, **kwargs):
            raise RuntimeError("no fonts")

        monkeypatch.setattr("handlers.kvk_handler.kvk_card_renderer.render_kvk_history_card", broken)
        interaction = _interaction()
        await handler._handle_kvk(interaction, 830)
        sent = interaction.followup.send.call_args.kwargs
        assert sent["files"] == [] and "view" not in sent
        embed = sent["embed"]
        assert embed.image.url is None
        fields = {field.name: field.value for field in embed.fields}
        assert fields["Castle Record"] == "0-13"
        assert fields["Castle Win Rate"] == "0.00%"
        assert fields["Prep Record"] == "Unavailable" and fields["Prep Win Rate"] == "Unavailable"
        # Newest first, five matches a field, ten in all.
        assert fields["Last Matches"].startswith("#13 ") and fields["Last Matches"].count("Castle") == 5
        assert fields["Last Matches (continued)"].startswith("#8 ")
        assert "#3 " not in fields["Last Matches (continued)"]

        assert handler._win_rate({"wins": 0, "losses": 0, "winRate": 0}) == "No decided matches"
        empty = handler._build_history_embed(830, {"history": [], "matchCount": 0})
        assert "Last Matches" not in {field.name for field in empty.fields}
        assert empty.description == "No KVK matches found for this kingdom."
        assert handler._build_history_embed(830, {}).description == "Match history unavailable."
        oversized = handler._build_history_embed(830, {"history": [{"season_date": "x" * 2000}] * 20, "matchCount": 20})
        assert all(len(field.value) <= 1024 for field in oversized.fields) and len(oversized) <= 6000

    asyncio.run(check())


def test_comparison_shows_the_direct_matchups_in_one_field():
    async def check():
        service = SimpleNamespace(compare_kingdoms=AsyncMock(return_value=_comparison(_history(12))))
        handler = KVKHandler(service, object())
        interaction = _interaction()
        await handler._handle_kvk(interaction, 830, 831)

        service.compare_kingdoms.assert_awaited_once_with(830, 831)
        sent = interaction.followup.send.call_args.kwargs
        # Without a dossier service there is nothing to press.
        assert "view" not in sent
        assert [file.filename for file in sent["files"]] == ["kvk-compare.png"]
        embed = sent["embed"]
        assert embed.title == "⚔️ KvK Compare: 830 vs 831" and "Dead heat" in embed.description
        assert embed.image.url == "attachment://kvk-compare.png"
        assert [field.name for field in embed.fields] == ["Direct Matchups"]
        direct = embed.fields[0].value
        assert direct.startswith("Matches: 12\nCastle: K830 12-0 | K831 12-0")
        assert direct.count("•") == 3 and "• #12 vs K831 · Castle ✅ Win · Prep ❌ Loss" in direct
        assert direct.endswith("… and 9 earlier")
        assert embed.footer.text == "Source: Kingshot KVK"

        assert handler._build_direct_matchups(830, 831, {"available": False, "matches": _history(2)}).startswith(
            "Head-to-head data unavailable"
        )
        none_yet = {"available": True, "matches": [], "kingdom_a": {"matchCount": 0}, "kingdom_b": {}}
        assert handler._build_direct_matchups(830, 831, none_yet) == "No direct KVK matchups found."

    asyncio.run(check())


def test_comparison_without_a_card_keeps_the_numbers_as_text(monkeypatch):
    async def check():
        service = SimpleNamespace(compare_kingdoms=AsyncMock(return_value=_comparison(_history(1))))
        handler = KVKHandler(service, object())

        def broken(*args, **kwargs):
            raise RuntimeError("no fonts")

        monkeypatch.setattr("handlers.kvk_handler.kvk_card_renderer.render_kvk_compare_card", broken)
        interaction = _interaction()
        await handler._handle_kvk(interaction, 830, 831)
        sent = interaction.followup.send.call_args.kwargs
        assert sent["files"] == []
        assert [field.name for field in sent["embed"].fields] == [
            "Kingdom 830", "Kingdom 831", "Overall Metrics", "Direct Matchups",
        ]

        service.compare_kingdoms = AsyncMock(return_value={"success": False})
        failed = _interaction()
        await handler._handle_kvk(failed, 830, 831)
        assert failed.followup.send.call_args.kwargs["embed"].title == "❌ Comparison Failed"

    asyncio.run(check())


def test_history_lines_use_clear_win_loss_markers():
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


def test_kvk_is_one_command_with_optional_kingdoms_validated_before_deferring():
    from discord import Intents, app_commands
    from discord.ext import commands

    bot = commands.Bot(command_prefix="!", intents=Intents.none())
    KVKHandler(SimpleNamespace(), bot, dossier_service=SimpleNamespace()).register_commands()
    command = bot.tree.get_command("kvk")
    assert isinstance(command, app_commands.Command)  # no history/compare/prep subcommands
    assert [parameter.name for parameter in command.parameters] == ["kingdom", "opponent"]
    for parameter in command.parameters:
        assert (parameter.min_value, parameter.max_value, parameter.required) == (1, 99999, False)

    async def check():
        handler = KVKHandler(SimpleNamespace(get_kingdom_stats=AsyncMock(), compare_kingdoms=AsyncMock()), object(),
                             dossier_service=SimpleNamespace(build_dossier=AsyncMock()))
        cases = [
            (handler._handle_kvk, (0,)),
            (handler._handle_kvk, (100000,)),
            (handler._handle_kvk, (830, 830)),
            (handler._handle_kvk, (830, -1)),
            (handler._handle_kvk, (None, 0)),
            (handler._handle_kvk_prep, (830, 830)),
        ]
        for method, args in cases:
            interaction = _interaction()
            await method(interaction, *args)
            interaction.response.defer.assert_not_awaited()
            assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True
        handler._kvk_service.get_kingdom_stats.assert_not_awaited()
        handler._kvk_service.compare_kingdoms.assert_not_awaited()
        handler._dossier_service.build_dossier.assert_not_awaited()

    asyncio.run(check())


def test_kvk_uses_the_servers_kingdom_when_none_is_named():
    async def check():
        stats = {"history": [], "matchCount": 0, "wins": 0, "losses": 0}
        service = SimpleNamespace(
            get_kingdom_stats=AsyncMock(return_value={"success": True, "data": stats}),
            compare_kingdoms=AsyncMock(return_value=_comparison()),
        )
        handler = KVKHandler(service, object(), guild_configuration_service=_settings(795))

        # /kvk: the history of the server's kingdom, posted publicly after a private lookup.
        interaction = _interaction(guild_id=10)
        await handler._handle_kvk(interaction)
        interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        service.get_kingdom_stats.assert_awaited_once_with(795)
        sent = interaction.followup.send.call_args.kwargs
        assert sent["embed"].title == "⚔️ KVK History · Kingdom 795" and "ephemeral" not in sent
        interaction.edit_original_response.assert_awaited_once_with(content="KvK report ready.")
        interaction.delete_original_response.assert_awaited_once()

        # /kvk opponent:831: the server's kingdom against the opponent.
        versus = _interaction(guild_id=10)
        await handler._handle_kvk(versus, None, 831)
        service.compare_kingdoms.assert_awaited_once_with(795, 831)
        assert versus.followup.send.call_args.kwargs["embed"].title == "⚔️ KvK Compare: 795 vs 831"

        # The opponent is the server's own kingdom: said privately, nothing is posted.
        same = _interaction(guild_id=10)
        await handler._handle_kvk(same, None, 795)
        embed = same.edit_original_response.call_args.kwargs["embed"]
        assert embed.title == "⚠️ Duplicate Kingdom" and "This server's kingdom is 795" in embed.description
        same.followup.send.assert_not_awaited()
        assert service.compare_kingdoms.await_count == 1

        # No kingdom anywhere: a private hint.
        for handler, guild_id, hinted in (
            (KVKHandler(service, object(), guild_configuration_service=_settings(None)), 10, True),
            (KVKHandler(service, object()), None, False),
        ):
            asked = _interaction(guild_id=guild_id)
            await handler._handle_kvk(asked, None, 831)
            asked.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
            embed = asked.edit_original_response.call_args.kwargs["embed"]
            assert embed.title == "Which kingdom?" and "`/kvk kingdom:830`" in embed.description
            assert ("`/settings`" in embed.description) is hinted
            asked.followup.send.assert_not_awaited()
        assert service.compare_kingdoms.await_count == 1

        # A failed lookup after the private acknowledgement stays private.
        service.get_kingdom_stats = AsyncMock(return_value={"success": False, "message": "down"})
        handler = KVKHandler(service, object(), guild_configuration_service=_settings(795))
        failed = _interaction(guild_id=10)
        await handler._handle_kvk(failed)
        assert failed.edit_original_response.call_args.kwargs["embed"].title == "❌ Could Not Fetch Kingdom 795"
        failed.followup.send.assert_not_awaited()

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


def test_comparison_offers_the_public_prep_dossier_as_its_only_button():
    async def check():
        service = SimpleNamespace(compare_kingdoms=AsyncMock(return_value=_comparison()))
        dossier_service = SimpleNamespace(build_dossier=AsyncMock(return_value=_dossier()))
        handler = KVKHandler(service, object(), dossier_service=dossier_service)
        interaction = _interaction()
        message = interaction.followup.send.return_value
        await handler._handle_kvk(interaction, 830, 831)
        sent = interaction.followup.send.call_args.kwargs
        view = sent["view"]
        assert isinstance(view, KVKCompareView) and view.timeout == 900
        assert [item.label for item in view.children] == ["Prep dossier"]
        assert view.message is message
        assert "Prep dossier posts a new message" in sent["embed"].footer.text

        click = _interaction()
        await view.dossier_button.callback(click)
        dossier_service.build_dossier.assert_awaited_once_with(830, 831)
        click.response.defer.assert_awaited_once_with(thinking=True)
        posted = click.followup.send.call_args
        first, second = posted.kwargs["embeds"]
        assert first.title == "🛡️ KvK Prep · K830 vs K831"
        # Rankings are two picture cards; castle and record stay as text.
        assert first.image.url == "attachment://kvk-prep-alliances.png"
        assert second.image.url == "attachment://kvk-prep-players.png"
        assert [file.filename for file in posted.kwargs["files"]] == ["kvk-prep-alliances.png", "kvk-prep-players.png"]
        assert not any("top alliances" in field.name or "top power" in field.name for field in first.fields)
        assert any("Castle & throne" in field.name for field in first.fields)
        assert second.footer.text.startswith("Cached up to 10 min") and not first.footer.text
        assert "ephemeral" not in posted.kwargs

        # Two dossiers a minute per user: the third click is refused privately.
        await view.dossier_button.callback(_interaction())
        limited = _interaction()
        await view.dossier_button.callback(limited)
        assert dossier_service.build_dossier.await_count == 2
        assert "Slow down" in limited.response.send_message.call_args.args[0]
        assert limited.response.send_message.call_args.kwargs["ephemeral"] is True

        # The button belongs to whoever ran the command, and visibly expires.
        stranger = SimpleNamespace(user=SimpleNamespace(id=2), response=SimpleNamespace(send_message=AsyncMock()))
        assert not await view.interaction_check(stranger)
        await view.on_timeout()
        assert all(item.disabled for item in view.children)
        message.edit.assert_awaited_once()

    asyncio.run(check())
