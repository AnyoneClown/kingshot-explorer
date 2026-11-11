from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from handlers.help_handler import GROUPS, HelpHandler, HelpView
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

    @bot.tree.command(name="player")
    async def player(ctx: discord.Interaction, player_id: str):
        calls.append(("player", player_id))

    @bot.tree.command(name="scout")
    async def scout(ctx: discord.Interaction, kingdom: int, limit: int = 5):
        calls.append(("scout", kingdom, limit))

    kvk = app_commands.Group(name="kvk", description="KvK")

    @kvk.command(name="history")
    async def kvk_history(ctx: discord.Interaction, kingdom: int):
        calls.append(("kvk history", kingdom))

    bot.tree.add_command(kvk)

    @bot.tree.command(name="status")
    async def status(ctx: discord.Interaction):
        calls.append(("status",))

    if local:
        gift = app_commands.Group(name="gift", description="Gift codes")
        admin = app_commands.Group(name="giftadmin", description="Gift code management")

        @gift.command(name="codes")
        async def gift_codes(ctx: discord.Interaction):
            calls.append(("gift codes",))

        @admin.command(name="redeem")
        async def redeem(ctx: discord.Interaction, gift_code: str):
            calls.append(("giftadmin redeem", gift_code))

        @admin.command(name="import")
        async def import_alliance(ctx: discord.Interaction, kid: int, alliance: str):
            calls.append(("giftadmin import", kid, alliance))

        bot.tree.add_command(gift)
        bot.tree.add_command(admin)

        @bot.tree.command(name="settings")
        async def settings(ctx: discord.Interaction):
            calls.append(("settings",))

        @bot.command(name="t")
        async def translate(ctx):
            pass

    return bot, calls


@pytest.mark.asyncio
async def test_calendar_help_opens_the_calendar_without_asking_for_a_kingdom():
    bot, calls = make_bot()

    @bot.tree.command(name="calendar")
    async def calendar(ctx: discord.Interaction, kingdom: int | None = None):
        calls.append(("calendar", kingdom))

    handler = HelpHandler(bot, {1})
    ctx = interaction(2)
    assert "Calendar" in handler.available_groups(ctx)
    assert "Alliance" not in handler.available_groups(ctx)
    await handler.open_command(ctx, name="calendar", label="Event calendar")
    assert calls == [("calendar", None)]


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
    assert [action[0] for action in member_groups["Gifts"][1]] == ["gift codes"]
    manager_gifts = [action[0] for action in handler.available_groups(interaction())["Gifts"][1]]
    assert manager_gifts == ["gift codes", "giftadmin import", "giftadmin redeem"]
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
    await handler.open_command(ctx, name="kvk history", label="Kingdom history")
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
    await handler.open_command(ctx, name="giftadmin redeem", label="Redeem code")
    modal = ctx.response.send_modal.call_args.args[0]
    modal.children[0]._value = "CODE"
    admins.clear()
    await modal.on_submit(ctx)
    assert not calls
    assert "not available" in ctx.response.send_message.call_args.args[0]


@pytest.mark.asyncio
async def test_import_alliance_guides_users_to_native_autocomplete():
    bot, calls = make_bot(local=True)
    handler = HelpHandler(bot, {1})
    ctx = interaction()
    await handler.open_command(ctx, name="giftadmin import", label="Import alliance")
    ctx.response.send_modal.assert_not_awaited()
    sent = ctx.response.send_message.call_args.kwargs
    assert sent["ephemeral"] is True
    assert "autocomplete" in sent["embed"].description
    assert "enter `830` for `kid`, then choose an alliance" in sent["embed"].description
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

    config = BotConfig(
        discord_token="test-token",
        database_url="postgresql+asyncpg://user:pass@localhost/test",
        nvidia_api_key="test-key",
    )
    app = TranslatorBot(config)
    names = {command.name for command in app.bot.tree.get_commands()}
    assert names == {
        "help", "status", "player", "scout", "kvk", "calendar", "gift", "giftadmin", "reminders", "remind",
        "settings", "Translate",
    }
    subcommands = {
        group: sorted(command.name for command in app.bot.tree.get_command(group).commands)
        for group in ("kvk", "gift", "giftadmin")
    }
    assert subcommands == {
        "kvk": ["compare", "history", "prep"],
        "gift": ["accounts", "add", "codes", "remove", "roster", "submit"],
        "giftadmin": ["import", "panel", "redeem"],
    }
    # Every action offered in /help resolves to a registered command.
    help_handler = HelpHandler(app.bot, {1})
    assert all(
        help_handler.find_command(action[0]) is not None
        for _, actions in GROUPS.values()
        for action in actions
    )
    assert help_handler.find_command("gift") is None and help_handler.find_command("gift nope") is None
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
