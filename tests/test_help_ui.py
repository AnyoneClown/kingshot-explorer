from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

from handlers.help_handler import HelpHandler, HelpView
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
    calls = []

    @bot.tree.command(name="stats")
    async def stats(ctx: discord.Interaction, player_id: str):
        calls.append(("stats", player_id))

    @bot.tree.command(name="scout")
    async def scout(ctx: discord.Interaction, kingdom_number: int, limit: int = 5):
        calls.append(("scout", kingdom_number, limit))

    @bot.tree.command(name="kvk")
    async def kvk(ctx: discord.Interaction, kingdom_number: int):
        calls.append(("kvk", kingdom_number))

    @bot.tree.command(name="status")
    async def status(ctx: discord.Interaction):
        calls.append(("status",))

    if local:
        @bot.tree.command(name="giftcodes")
        async def giftcodes(ctx: discord.Interaction):
            calls.append(("giftcodes",))

        @bot.tree.command(name="redeem")
        async def redeem(ctx: discord.Interaction, gift_code: str):
            calls.append(("redeem", gift_code))

        @bot.tree.command(name="addalliance")
        async def addalliance(ctx: discord.Interaction, kid: int, alliance: str):
            calls.append(("addalliance", kid, alliance))

        @bot.tree.command(name="configure")
        async def configure(ctx: discord.Interaction):
            calls.append(("configure",))

        @bot.command(name="t")
        async def translate(ctx):
            pass

    return bot, calls


@pytest.mark.asyncio
async def test_power_trends_help_is_available_in_servers_and_opens_existing_report():
    bot, calls = make_bot()

    @bot.tree.command(name="alliance")
    async def alliance(ctx: discord.Interaction, kid: int | None = None, alliance: str | None = None):
        calls.append(("alliance", kid, alliance))

    handler = HelpHandler(bot, {1})
    ctx = interaction(2)
    assert "Alliance" in handler.available_groups(ctx)
    assert "Alliance" not in handler.available_groups(interaction(guild=False))
    await handler.open_command(ctx, name="alliance", label="Power trends")
    assert calls == [("alliance", None, None)]


@pytest.mark.asyncio
async def test_help_only_shows_registered_features_and_available_admin_actions():
    bot, _ = make_bot()
    handler = HelpHandler(bot, {1})
    assert set(handler.available_groups(interaction())) == {"Players", "KVK"}

    bot, _ = make_bot(local=True)
    handler = HelpHandler(bot, {1})
    member_groups = handler.available_groups(interaction(2))
    assert "Translation" in member_groups
    assert "Settings" not in member_groups
    assert all(action[0] != "redeem" for action in member_groups["Gifts"][1])
    assert "Settings" in handler.available_groups(interaction())
    assert "Gifts" not in handler.available_groups(interaction(guild=False))


@pytest.mark.asyncio
async def test_help_button_opens_modal_and_preserves_command_types_and_defaults():
    bot, calls = make_bot()
    handler = HelpHandler(bot)
    ctx = interaction()
    view = HelpView(handler, ctx)
    button = next(item for item in view.children if getattr(item, "label", "") == "Scout kingdom")
    await button.callback(ctx)
    modal = ctx.response.send_modal.call_args.args[0]
    assert len(modal.children) == 1
    modal.children[0]._value = "830"
    await modal.on_submit(ctx)
    assert calls == [("scout", 830, 5)]


@pytest.mark.asyncio
async def test_form_rejects_invalid_numbers_and_other_users_without_running_command():
    bot, calls = make_bot()
    handler = HelpHandler(bot)
    ctx = interaction()
    await handler.open_command(ctx, name="kvk", label="Kingdom history")
    modal = ctx.response.send_modal.call_args.args[0]
    modal.children[0]._value = "not a number"
    await modal.on_submit(ctx)
    assert "whole number" in ctx.response.send_message.call_args.args[0]
    modal.children[0]._value = "830"
    other = interaction(2)
    await modal.on_submit(other)
    other.response.send_message.assert_awaited_once()
    assert not calls


@pytest.mark.asyncio
async def test_admin_access_is_rechecked_when_form_is_submitted():
    bot, calls = make_bot(local=True)
    admins = {1}
    handler = HelpHandler(bot, admins)
    ctx = interaction()
    await handler.open_command(ctx, name="redeem", label="Redeem code")
    modal = ctx.response.send_modal.call_args.args[0]
    modal.children[0]._value = "CODE"
    admins.clear()
    await modal.on_submit(ctx)
    assert not calls
    assert "not available" in ctx.response.send_message.call_args.args[0]


@pytest.mark.asyncio
async def test_import_alliance_guides_users_to_native_autocomplete():
    bot, calls = make_bot(local=True)
    handler = HelpHandler(bot)
    ctx = interaction()
    await handler.open_command(ctx, name="addalliance", label="Import alliance")
    ctx.response.send_modal.assert_not_awaited()
    sent = ctx.response.send_message.call_args.kwargs
    assert sent["ephemeral"] is True
    assert "autocomplete" in sent["embed"].description
    assert "/addalliance kid:830 alliance:FKA" in sent["embed"].description
    assert "aid:" not in sent["embed"].description
    assert not calls


@pytest.mark.asyncio
async def test_help_is_private_and_expired_controls_are_disabled():
    bot, _ = make_bot()
    handler = HelpHandler(bot)
    handler.register_commands()
    ctx = interaction()
    await bot.tree.get_command("help").callback(ctx)
    sent = ctx.response.send_message.call_args.kwargs
    assert sent["ephemeral"] is True
    view = sent["view"]
    assert await view.interaction_check(interaction(2)) is False
    await view.on_timeout()
    assert all(item.disabled for item in view.children)
    view.message.edit.assert_awaited_once_with(view=view)
    assert len(view.build_embed()) < 6000


@pytest.mark.asyncio
async def test_shared_controls_reject_non_owner():
    view = OwnedView(1)
    assert await view.interaction_check(interaction()) is True
    assert await view.interaction_check(interaction(2)) is False


@pytest.mark.asyncio
async def test_help_is_registered_with_all_workflows_and_native_alliance_autocomplete(monkeypatch):
    from config.bot_config import BotConfig
    from main import TranslatorBot

    monkeypatch.setattr("main.ChatNVIDIA", lambda **kwargs: SimpleNamespace())
    config = BotConfig(
        discord_token="test-token",
        database_url="postgresql+asyncpg://user:pass@localhost/test",
        nvidia_api_key="test-key",
    )
    app = TranslatorBot(config)
    names = {command.name for command in app.bot.tree.get_commands()}
    assert {"help", "status", "stats", "scout", "kvk", "kvk_compare"} <= names
    assert {"events", "schedule", "configure", "giftcodes", "redeem", "addalliance"} <= names
    alliance_parameters = app.bot.tree.get_command("addalliance").parameters
    assert [parameter.name for parameter in alliance_parameters] == ["kid", "alliance"]
    assert all(parameter.autocomplete for parameter in alliance_parameters)
    await app.bot.close()
    await app.db_manager.close()
    if app.nvidia_client:
        await app.nvidia_client.close()
