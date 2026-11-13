from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from handlers.help_handler import SECTIONS, HelpHandler
from handlers.ui import OwnedView


def interaction(user_id=1, *, guild=True):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        guild=SimpleNamespace(id=10) if guild else None,
        response=SimpleNamespace(send_message=AsyncMock(), send_modal=AsyncMock(), edit_message=AsyncMock()),
        original_response=AsyncMock(),
    )


def make_bot(*, local=False):
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())

    @bot.tree.command(name="player")
    async def player(ctx: discord.Interaction, player_id: str):
        pass

    @bot.tree.command(name="kvk")
    async def kvk(ctx: discord.Interaction, kingdom: int | None = None):
        pass

    @bot.tree.command(name="status")
    async def status(ctx: discord.Interaction):
        pass

    if local:
        admin = app_commands.Group(name="giftadmin", description="Gift code management")

        @bot.tree.command(name="gift")
        async def gift(ctx: discord.Interaction):
            pass

        @admin.command(name="redeem")
        async def redeem(ctx: discord.Interaction, gift_code: str):
            pass

        @admin.command(name="import")
        async def import_alliance(ctx: discord.Interaction, kid: int, alliance: str):
            pass

        bot.tree.add_command(admin)

        @bot.tree.command(name="settings")
        async def settings(ctx: discord.Interaction):
            pass

        @bot.command(name="t")
        async def translate(ctx):
            pass

    return bot


def fields(embed):
    return {field.name: field.value for field in embed.fields}


def test_help_lists_only_registered_commands_and_keeps_manager_commands_for_managers():
    handler = HelpHandler(make_bot(), {1})
    shown = fields(handler.build_embed(in_guild=True, is_manager=True))
    # Nothing of a feature that is not registered, and no empty section.
    assert set(shown) == {"🔎 Players and kingdoms", "ℹ️ Bot"}
    assert "`/player`" in shown["🔎 Players and kingdoms"] and "`/kvk`" in shown["🔎 Players and kingdoms"]
    assert "scout" not in shown["🔎 Players and kingdoms"]

    handler = HelpHandler(make_bot(local=True), {1})
    member = fields(handler.build_embed(in_guild=True, is_manager=False))
    assert "🛠️ Server managers" not in member
    assert "`/gift`" in member["🎁 Gift codes"]
    assert "!en" in member["🌐 Translation"]
    manager = fields(handler.build_embed(in_guild=True, is_manager=True))["🛠️ Server managers"]
    assert "`/settings`" in manager and "`/giftadmin import`" in manager and "`/giftadmin redeem`" in manager
    assert "giftadmin panel" not in manager  # not registered on this bot


def test_help_hides_server_only_commands_in_direct_messages():
    handler = HelpHandler(make_bot(local=True), {1})
    shown = fields(handler.build_embed(in_guild=False, is_manager=False))
    assert "🎁 Gift codes" not in shown
    assert "🔎 Players and kingdoms" in shown


def test_commands_are_clickable_mentions_once_their_ids_are_known():
    handler = HelpHandler(make_bot(local=True), {1})
    assert handler.mention("gift") == "`/gift`"
    handler.remember_commands(
        [SimpleNamespace(name="gift", id=11), SimpleNamespace(name="giftadmin", id=22), SimpleNamespace(name="x", id=None)]
    )
    assert handler.mention("gift") == "</gift:11>"
    # A subcommand is mentioned with the ID of its group.
    assert handler.mention("giftadmin import") == "</giftadmin import:22>"
    assert handler.mention("player") == "`/player`"
    manager = fields(handler.build_embed(in_guild=True, is_manager=True))["🛠️ Server managers"]
    assert "</giftadmin import:22>" in manager


@pytest.mark.asyncio
async def test_help_is_one_private_message_without_controls():
    bot = make_bot(local=True)
    handler = HelpHandler(bot, {1})
    handler.register_commands()
    ctx = interaction(2)
    await bot.tree.get_command("help").callback(ctx)
    sent = ctx.response.send_message.call_args.kwargs
    assert sent["ephemeral"] is True
    assert "view" not in sent
    assert "🛠️ Server managers" not in fields(sent["embed"])
    assert len(sent["embed"]) < 6000

    ctx = interaction(1)
    await bot.tree.get_command("help").callback(ctx)
    assert "🛠️ Server managers" in fields(ctx.response.send_message.call_args.kwargs["embed"])


@pytest.mark.asyncio
async def test_help_loads_command_ids_itself_when_the_sync_did_not_and_survives_a_failure():
    bot = make_bot()
    handler = HelpHandler(bot, {1})
    bot.tree.fetch_commands = AsyncMock(side_effect=RuntimeError("discord is down"))
    await handler._ensure_command_ids()
    assert handler.mention("player") == "`/player`"
    # A failed lookup is not repeated on every /help.
    await handler._ensure_command_ids()
    assert bot.tree.fetch_commands.await_count == 1

    handler = HelpHandler(bot, {1})
    bot.tree.fetch_commands = AsyncMock(return_value=[SimpleNamespace(name="player", id=5)])
    await handler._ensure_command_ids()
    assert handler.mention("player") == "</player:5>"


@pytest.mark.asyncio
async def test_shared_controls_reject_non_owner():
    view = OwnedView(1)
    assert await view.interaction_check(interaction()) is True
    assert await view.interaction_check(interaction(2)) is False


@pytest.mark.asyncio
async def test_commands_are_registered_and_help_names_only_real_ones(monkeypatch):
    from config.bot_config import BotConfig
    from main import TranslatorBot

    config = BotConfig(
        discord_token="test-token",
        database_url="postgresql+asyncpg://user:pass@localhost/test",
        nvidia_api_key="test-key",
    )
    app = TranslatorBot(config)
    names = {command.name for command in app.bot.tree.get_commands()}
    assert names == {
        "help", "status", "player", "scout", "leaderboard", "kvk", "calendar", "gift", "giftadmin", "reminders",
        "settings", "Translate",
    }
    # Only the manager actions are a group; everything else is one command.
    groups = {
        command.name: sorted(sub.name for sub in command.commands)
        for command in app.bot.tree.get_commands()
        if isinstance(command, app_commands.Group)
    }
    assert groups == {"giftadmin": ["import", "panel", "redeem", "roster"]}
    # Every command named in /help resolves to a registered command.
    help_handler = HelpHandler(app.bot, {1})
    assert all(
        help_handler.find_command(name) is not None for _, _, entries in SECTIONS for name, _ in entries
    )
    assert help_handler.find_command("giftadmin") is None and help_handler.find_command("giftadmin nope") is None
    alliance_parameters = app.bot.tree.get_command("giftadmin").get_command("import").parameters
    assert [parameter.name for parameter in alliance_parameters] == ["kid", "alliance"]
    assert all(parameter.autocomplete for parameter in alliance_parameters)
    await app.bot.close()
    await app.db_manager.close()
    if app.nvidia_client:
        await app.nvidia_client.close()


@pytest.mark.asyncio
async def test_panel_edit_falls_back_to_bot_token_after_interaction_token_expires():
    from unittest.mock import Mock

    from handlers.ui import edit_panel_message

    expired = discord.HTTPException(Mock(status=401, reason="Unauthorized"), "Invalid Webhook Token")
    partial = SimpleNamespace(edit=AsyncMock(return_value="edited"))
    channel = SimpleNamespace(get_partial_message=Mock(return_value=partial))
    message = SimpleNamespace(
        id=42, channel=channel, flags=SimpleNamespace(ephemeral=False), edit=AsyncMock(side_effect=expired)
    )

    assert await edit_panel_message(message, view=None) == "edited"
    channel.get_partial_message.assert_called_once_with(42)

    message.flags.ephemeral = True
    with pytest.raises(discord.HTTPException):
        await edit_panel_message(message, view=None)
