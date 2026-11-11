"""Regression tests for the translation / contextual-chat audit fixes."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest
from discord.ext import commands

from handlers.translation_handler import TranslationHandler
from tests.test_translation_handler_auto import (
    FakeChannel,
    FakeChatbot,
    FakeContext,
    FakeMessage,
    FakeTranslation,
    make_handler,
)

BOT_ROLE_ID = 999


def _role_mention_message(content, role_id):
    message = FakeMessage(content, roles=())
    message.raw_role_mentions = [role_id]
    message.guild.self_role = SimpleNamespace(id=BOT_ROLE_ID)
    return message


def test_mentioning_the_bots_own_role_is_a_direct_trigger():
    chatbot = FakeChatbot("Power is up.")
    handler, _ = make_handler(chatbot=chatbot)

    # "@AI Clown" picked as the bot's managed role from Discord's autocomplete.
    message = _role_mention_message("@AI Clown how strong is our alliance?", BOT_ROLE_ID)
    asyncio.run(handler._handle_contextual_chat(message))

    assert chatbot.calls and chatbot.calls[0][1]["force_reply"] is True
    assert message.replies[0]["content"] == "Power is up."

    # Any other role mention is ordinary chat (random replies are off here).
    other = _role_mention_message("@Officers rally at 20:00 please", 123)
    other.author.id = 11
    asyncio.run(handler._handle_contextual_chat(other))
    assert len(chatbot.calls) == 1 and other.replies == []


def test_translate_command_without_a_language_explains_usage():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    handler = TranslationHandler(FakeTranslation(), SimpleNamespace(), bot)
    handler.register_commands()
    command = bot.get_command("t")
    assert command.has_error_handler()

    ctx = SimpleNamespace(clean_prefix="!", reply=AsyncMock())
    error = commands.MissingRequiredArgument(command.clean_params["target_language"])
    asyncio.run(handler._handle_command_error(ctx, error))

    embed = ctx.reply.call_args.kwargs["embed"]
    assert embed.title == "Language Required"
    assert "`!t <language>`" in embed.description


def test_forwarded_messages_are_translated_from_their_snapshot():
    async def check():
        translation = FakeTranslation({"text": "Hello everyone"})
        handler, _ = make_handler(translation)
        forwarded = SimpleNamespace(
            content="", message_snapshots=[SimpleNamespace(content="Hola a todos")],
            jump_url=None, author=None, channel=SimpleNamespace(id=789),
        )
        interaction = SimpleNamespace(
            locale=discord.Locale.american_english,
            user=SimpleNamespace(id=10, name="user", discriminator="0", display_name="User"),
            guild=SimpleNamespace(id=456), channel=SimpleNamespace(id=789),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await handler._handle_translate_message_menu(interaction, forwarded)
        assert translation.calls == [("Hola a todos", "English")]
        assert interaction.followup.send.call_args.kwargs["embed"].title == "Translated to English"

        # !t replying to a forwarded message reads the same text.
        ctx = FakeContext("")
        ctx.channel = FakeChannel(referenced=forwarded)
        await handler._handle_translate_to_language(ctx, "English")
        assert translation.calls[-1] == ("Hola a todos", "English")
        assert ctx.replies[0]["embed"].title == "Translated to English"

    asyncio.run(check())


@pytest.mark.asyncio
async def test_commands_with_their_own_error_handler_get_a_single_reply():
    from config.bot_config import BotConfig
    from main import TranslatorBot

    app = TranslatorBot(
        BotConfig(
            discord_token="test-token",
            database_url="postgresql+asyncpg://user:pass@localhost/test",
            nvidia_api_key="test-key",
        )
    )
    try:
        command = app.bot.get_command("t")
        ctx = SimpleNamespace(
            command=command, author="user", clean_prefix="!", reply=AsyncMock(), send=AsyncMock()
        )
        error = commands.MissingRequiredArgument(command.clean_params["target_language"])
        # What Command.dispatch_error does: the command's own handler, then the global event.
        await command.on_error(ctx, error)
        await app.bot.on_command_error(ctx, error)

        assert ctx.reply.await_count == 1
        assert ctx.reply.call_args.kwargs["embed"].title == "Language Required"
        ctx.send.assert_not_awaited()
    finally:
        await app.bot.close()
        await app.db_manager.close()
        if app.nvidia_client:
            await app.nvidia_client.close()
