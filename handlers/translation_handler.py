import logging
import random
import time
from typing import Dict, List

import discord
from discord.ext import commands

from db import get_db
from services.database_service import DatabaseService
from services.translation_service import ITranslationService

logger = logging.getLogger(__name__)


class TranslationHandler:
    """Handles translation-related Discord commands and events."""

    def __init__(self, translation_service: ITranslationService, bot: commands.Bot, config=None):
        """
        Initialize translation handler.

        Args:
            translation_service: Service for handling translations
            bot: Discord bot instance
            config: Bot configuration containing banned players list
        """
        self._translation_service = translation_service
        self._bot = bot
        self._config = config
        self._last_chat_reply_at: Dict[int, float] = {}

    def register_commands(self):
        """Register all translation commands with the bot."""

        @self._bot.command(name="t", aliases=["translate"])
        async def translate_command(ctx, *, target_language: str):
            """Translates a replied-to message into a specified language."""
            await self._handle_translate_to_language(ctx, target_language)

        @self._bot.command(name="en")
        async def en_command(ctx):
            """Translates a replied-to message into English."""
            await self._handle_translate_to_english(ctx)

        @en_command.error
        async def en_command_error(ctx, error):
            """Handles errors for the !en command."""
            await self._handle_command_error(ctx, error)

    def register_events(self):
        """Register message events for automatic translation."""

        @self._bot.event
        async def on_message(message):
            if message.author == self._bot.user or message.author.bot:
                return

            await self._bot.process_commands(message)

            # Stop if command or DM
            if message.content.startswith(self._bot.command_prefix) or not message.guild:
                return

            await self._handle_auto_translation(message)
            await self._handle_contextual_chat(message)

    async def _handle_translate_to_language(self, ctx, target_language: str):
        """Handle translation to a specific language command."""
        # Check if user is banned from translation
        if self._config and ctx.author.id in self._config.banned_players:
            await ctx.reply("⛔ You are currently blocked from using translation commands.")
            return

        if not ctx.message.reference:
            await ctx.reply("💬 Reply to a message first, then use this command (example: `!t spanish`).")
            return

        try:
            original_message = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            text_to_translate = original_message.content

            if not text_to_translate:
                await ctx.reply("⚠️ The replied-to message is empty.")
                return

            async with ctx.typing():
                result = self._translation_service.translate_to_language(text_to_translate, target_language)

            if result:
                translated_text = result.get("text")
                if not translated_text:
                    await ctx.reply("❌ Translation completed but no text was returned.")
                    return

                quoted_text = self._as_quote_block(self._truncate_for_discord(translated_text, 1500))
                await ctx.reply(f"🌐 **Translated to {target_language}**\n{quoted_text}")

                # Track in database
                try:
                    db = get_db()
                    async with db.session() as session:
                        await DatabaseService.get_or_create_user(
                            session,
                            ctx.author.id,
                            ctx.author.name,
                            ctx.author.discriminator,
                            ctx.author.display_name,
                        )
                        await DatabaseService.log_translation(
                            session,
                            user_id=ctx.author.id,
                            original_text=text_to_translate,
                            translated_text=translated_text,
                            target_language=target_language,
                            source_language=result.get("language"),
                            translation_type="command",
                            guild_id=ctx.guild.id if ctx.guild else None,
                            channel_id=ctx.channel.id,
                        )
                except Exception as db_error:
                    logger.error(f"Database tracking error: {db_error}", exc_info=True)
            else:
                await ctx.reply(f"❌ I couldn't translate that to {target_language}. Try a different language name.")
        except Exception as e:
            logger.error(f"Error in translate command: {e}")
            await ctx.reply("❌ An error occurred while processing your translation request.")

    async def _handle_translate_to_english(self, ctx):
        """Handle translation to English command."""
        # Check if user is banned from translation
        if self._config and ctx.author.id in self._config.banned_players:
            await ctx.reply("⛔ You are currently blocked from using translation commands.")
            return

        if not ctx.message.reference:
            await ctx.reply("💬 Reply to a message first, then use `!en`.")
            return

        try:
            original_message = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            text_to_translate = original_message.content

            if not text_to_translate:
                await ctx.reply("⚠️ The replied-to message is empty.")
                return

            result = self._translation_service.translate_to_english(text_to_translate)

            if result and result.get("language").lower() not in ("english", "en"):
                translated_text = result.get("text")
                source_language = result.get("language")
                if not translated_text:
                    await ctx.reply("❌ Translation completed but no text was returned.")
                    return

                quoted_text = self._as_quote_block(self._truncate_for_discord(translated_text, 1500))
                await ctx.reply(f"🌐 **Translated from {source_language}**\n{quoted_text}")

                # Track in database
                try:
                    db = get_db()
                    async with db.session() as session:
                        await DatabaseService.get_or_create_user(
                            session,
                            ctx.author.id,
                            ctx.author.name,
                            ctx.author.discriminator,
                            ctx.author.display_name,
                        )
                        await DatabaseService.log_translation(
                            session,
                            user_id=ctx.author.id,
                            original_text=text_to_translate,
                            translated_text=translated_text,
                            target_language="en",
                            source_language=source_language,
                            translation_type="command",
                            guild_id=ctx.guild.id if ctx.guild else None,
                            channel_id=ctx.channel.id,
                        )
                except Exception as db_error:
                    logger.error(f"Database tracking error: {db_error}", exc_info=True)
            elif result:
                await ctx.reply("✅ The message already appears to be in English.")
            else:
                await ctx.reply("❌ I couldn't translate that message.")
        except Exception as e:
            logger.error(f"Error in !en command: {e}")
            await ctx.reply("❌ An error occurred while translating.")

    async def _handle_command_error(self, ctx, error):
        """Handle errors for translation commands."""
        if isinstance(error, commands.MissingRole):
            await ctx.reply("⛔ You need the `Translator` role to use this command.")
        else:
            logger.error(f"Unhandled error in command: {error}")
            await ctx.reply("❌ An unexpected error occurred.")

    async def _handle_auto_translation(self, message: discord.Message):
        """Automatically translate messages from users with Translator role."""
        role_name = self._config.translator_role_name if self._config else "Translator"
        role = discord.utils.get(message.guild.roles, name=role_name)

        if not (role and role in message.author.roles):
            return

        # Check if user is banned from auto-translation
        if self._config and message.author.id in self._config.banned_players:
            logger.debug(f"User {message.author.id} is banned from auto-translation")
            return

        # Skip empty or command-like messages
        if not message.content or message.content.startswith(("!", "$", "/", "?")):
            return

        try:
            result = self._translation_service.translate_to_english(message.content)

            if result and result.get("language") != "English":
                translated_text = result.get("text")
                source_language = result.get("language")
                if translated_text:
                    await message.reply(self._as_quote_block(self._truncate_for_discord(translated_text, 1500)))

                # Track in database
                try:
                    db = get_db()
                    async with db.session() as session:
                        await DatabaseService.get_or_create_user(
                            session,
                            message.author.id,
                            message.author.name,
                            message.author.discriminator,
                            message.author.display_name,
                        )
                        await DatabaseService.log_translation(
                            session,
                            user_id=message.author.id,
                            original_text=message.content,
                            translated_text=translated_text,
                            target_language="en",
                            source_language=source_language,
                            translation_type="auto",
                            guild_id=message.guild.id if message.guild else None,
                            channel_id=message.channel.id,
                        )
                except Exception as db_error:
                    logger.error(f"Database tracking error: {db_error}", exc_info=True)
        except Exception as e:
            logger.error(f"Auto-translation error: {e}", exc_info=True)
            # Don't send error messages for auto-translation to avoid spam

    async def _handle_contextual_chat(self, message: discord.Message):
        """Optionally reply to chat using recent message history as context."""
        if not message.content.strip():
            return

        if self._config and message.author.id in self._config.banned_players:
            return

        if not self._should_attempt_reply(message):
            return

        try:
            history = await self._collect_conversation_context(message)
            async with message.channel.typing():
                reply_text = self._translation_service.generate_contextual_reply(message.content, history)

            if not reply_text:
                return

            await message.reply(
                self._truncate_for_discord(reply_text, self._config.max_chat_response_chars if self._config else 500),
                mention_author=False,
            )

            self._last_chat_reply_at[message.channel.id] = time.monotonic()
        except Exception as e:
            logger.error(f"Contextual chat error: {e}", exc_info=True)

    def _should_attempt_reply(self, message: discord.Message) -> bool:
        """Return True if the bot should try generating a chat reply."""
        if self._is_direct_mention_or_reply(message):
            return True

        if not self._config:
            return False

        if self._config.random_reply_chance <= 0:
            return False

        if len(message.content.strip()) < 4:
            return False

        last_reply_at = self._last_chat_reply_at.get(message.channel.id)
        if last_reply_at is not None:
            if time.monotonic() - last_reply_at < self._config.random_reply_cooldown_seconds:
                return False

        return random.random() < self._config.random_reply_chance

    def _is_direct_mention_or_reply(self, message: discord.Message) -> bool:
        """Detect whether the message is directly aimed at the bot."""
        if self._bot.user and self._bot.user in message.mentions:
            return True

        reference = message.reference
        if not reference:
            return False

        resolved = getattr(reference, "resolved", None)
        if isinstance(resolved, discord.Message) and self._bot.user:
            return resolved.author.id == self._bot.user.id

        return False

    async def _collect_conversation_context(self, message: discord.Message) -> List[Dict[str, str]]:
        """Collect recent messages above the current one for context."""
        history_limit = self._config.chat_history_limit if self._config else 12
        collected: List[Dict[str, str]] = []

        async for previous in message.channel.history(limit=history_limit, before=message, oldest_first=False):
            if previous.author.bot and previous.author != self._bot.user:
                continue

            content = (previous.content or "").strip()
            if not content:
                continue

            collected.append(
                {
                    "author": previous.author.display_name,
                    "content": content,
                }
            )

        collected.reverse()
        return collected

    @staticmethod
    def _truncate_for_discord(text: str, limit: int = 1500) -> str:
        """Truncate long text to avoid Discord message limits."""
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."

    @staticmethod
    def _as_quote_block(text: str) -> str:
        """Convert arbitrary text to Discord quote block formatting."""
        lines = text.splitlines() or [text]
        return "\n".join(f"> {line}" if line else ">" for line in lines)
