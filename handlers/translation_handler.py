import logging
import random
import time
from io import BytesIO
from typing import Dict, List

import discord
from discord.ext import commands

from handlers.ui import EmbedColors, build_status_embed
from services.chatbot_service import IChatbotService
from services.guild_configuration_service import GuildConfigurationService
from services.interaction_tracking_service import InteractionTrackingService
from services.translation_service import ITranslationService
from services.voice_message_service import VoiceMessageService

logger = logging.getLogger(__name__)


class TranslationHandler:
    """Handles translation-related Discord commands and events."""

    def __init__(
        self,
        translation_service: ITranslationService,
        chatbot_service: IChatbotService,
        bot: commands.Bot,
        config=None,
        voice_message_service: VoiceMessageService | None = None,
        interaction_tracking_service: InteractionTrackingService | None = None,
        guild_configuration_service: GuildConfigurationService | None = None,
    ):
        """
        Initialize translation handler.

        Args:
            translation_service: Service for handling translations
            chatbot_service: Service for contextual conversational replies
            bot: Discord bot instance
            config: Bot configuration containing banned players list
        """
        self._translation_service = translation_service
        self._chatbot_service = chatbot_service
        self._bot = bot
        self._config = config
        self._voice_message_service = voice_message_service
        self._last_chat_reply_at: Dict[int, float] = {}
        self._interaction_tracking_service = interaction_tracking_service or InteractionTrackingService()
        self._guild_configuration_service = guild_configuration_service

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
            await ctx.reply(
                embed=build_status_embed(
                    title="Translation Blocked",
                    description="You are currently blocked from using translation commands.",
                    color=EmbedColors.ERROR,
                )
            )
            return

        if not ctx.message.reference:
            await ctx.reply(
                embed=build_status_embed(
                    title="Reply Required",
                    description="Reply to a message first, then use this command. Example: `!t spanish`.",
                    color=EmbedColors.WARNING,
                )
            )
            return

        try:
            original_message = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            text_to_translate = original_message.content

            if not text_to_translate:
                await ctx.reply(
                    embed=build_status_embed(
                        title="Empty Message",
                        description="The replied-to message does not contain text to translate.",
                        color=EmbedColors.WARNING,
                    )
                )
                return

            async with ctx.typing():
                result = await self._translation_service.translate_to_language(text_to_translate, target_language)

                if result:
                    translated_text = result.get("text")
                    if not translated_text:
                        await ctx.reply(
                            embed=build_status_embed(
                                title="Translation Failed",
                                description="The AI service responded, but no translated text was returned.",
                                color=EmbedColors.ERROR,
                            )
                        )
                        return

                    quoted_text = self._as_quote_block(self._truncate_for_discord(translated_text, 1500))
                    embed = build_status_embed(
                        title=f"Translated to {target_language}",
                        description=quoted_text,
                        color=EmbedColors.INFO,
                        footer="DS Translator",
                    )
                    await self._reply_with_optional_voice(
                        ctx.reply,
                        embed=embed,
                        guild_id=ctx.guild.id if ctx.guild else None,
                        voice_text=translated_text,
                        language_hint=target_language,
                        filename_stem="translation",
                    )

                    # Track in database
                    try:
                        await self._interaction_tracking_service.track_translation(
                            user_id=ctx.author.id,
                            original_text=text_to_translate,
                            translated_text=translated_text,
                            target_language=target_language,
                            source_language=result.get("language"),
                            translation_type="command",
                            guild_id=ctx.guild.id if ctx.guild else None,
                            channel_id=ctx.channel.id,
                            username=ctx.author.name,
                            discriminator=ctx.author.discriminator,
                            display_name=ctx.author.display_name,
                        )
                    except Exception as db_error:
                        logger.error(f"Database tracking error: {db_error}", exc_info=True)
                else:
                    await ctx.reply(
                        embed=build_status_embed(
                            title="Translation Failed",
                            description=(
                                f"I couldn't translate that to `{target_language}`. "
                                "Try a common language name like `Spanish`, `French`, or `English`."
                            ),
                            color=EmbedColors.ERROR,
                        )
                    )
        except Exception as e:
            logger.error(f"Error in translate command: {e}")
            await ctx.reply(
                embed=build_status_embed(
                    title="Translation Unavailable",
                    description="The translation request failed. Try again in a moment.",
                    color=EmbedColors.ERROR,
                )
            )

    async def _handle_translate_to_english(self, ctx):
        """Handle translation to English command."""
        # Check if user is banned from translation
        if self._config and ctx.author.id in self._config.banned_players:
            await ctx.reply(
                embed=build_status_embed(
                    title="Translation Blocked",
                    description="You are currently blocked from using translation commands.",
                    color=EmbedColors.ERROR,
                )
            )
            return

        if not ctx.message.reference:
            await ctx.reply(
                embed=build_status_embed(
                    title="Reply Required",
                    description="Reply to a message first, then use `!en`.",
                    color=EmbedColors.WARNING,
                )
            )
            return

        try:
            original_message = await ctx.channel.fetch_message(ctx.message.reference.message_id)
            text_to_translate = original_message.content

            if not text_to_translate:
                await ctx.reply(
                    embed=build_status_embed(
                        title="Empty Message",
                        description="The replied-to message does not contain text to translate.",
                        color=EmbedColors.WARNING,
                    )
                )
                return

            async with ctx.typing():
                result = await self._translation_service.translate_to_english(text_to_translate)

            if result and result.get("language").lower() not in ("english", "en"):
                translated_text = result.get("text")
                source_language = result.get("language")
                if not translated_text:
                    await ctx.reply(
                        embed=build_status_embed(
                            title="Translation Failed",
                            description="The AI service responded, but no translated text was returned.",
                            color=EmbedColors.ERROR,
                        )
                    )
                    return

                quoted_text = self._as_quote_block(self._truncate_for_discord(translated_text, 1500))
                embed = build_status_embed(
                    title=f"Translated from {source_language}",
                    description=quoted_text,
                    color=EmbedColors.INFO,
                    footer="DS Translator",
                )
                await self._reply_with_optional_voice(
                    ctx.reply,
                    embed=embed,
                    guild_id=ctx.guild.id if ctx.guild else None,
                    voice_text=translated_text,
                    language_hint=source_language,
                    filename_stem="translation",
                )

                # Track in database
                try:
                    await self._interaction_tracking_service.track_translation(
                        user_id=ctx.author.id,
                        original_text=text_to_translate,
                        translated_text=translated_text,
                        target_language="en",
                        source_language=source_language,
                        translation_type="command",
                        guild_id=ctx.guild.id if ctx.guild else None,
                        channel_id=ctx.channel.id,
                        username=ctx.author.name,
                        discriminator=ctx.author.discriminator,
                        display_name=ctx.author.display_name,
                    )
                except Exception as db_error:
                    logger.error(f"Database tracking error: {db_error}", exc_info=True)
            elif result:
                await ctx.reply(
                    embed=build_status_embed(
                        title="Already English",
                        description="The replied-to message already appears to be in English.",
                        color=EmbedColors.SUCCESS,
                    )
                )
            else:
                await ctx.reply(
                    embed=build_status_embed(
                        title="Translation Failed",
                        description="I couldn't translate that message. Try again with a clearer text message.",
                        color=EmbedColors.ERROR,
                    )
                )
        except Exception as e:
            logger.error(f"Error in !en command: {e}")
            await ctx.reply(
                embed=build_status_embed(
                    title="Translation Unavailable",
                    description="The translation request failed. Try again in a moment.",
                    color=EmbedColors.ERROR,
                )
            )

    async def _handle_command_error(self, ctx, error):
        """Handle errors for translation commands."""
        if isinstance(error, commands.MissingRole):
            await ctx.reply(
                embed=build_status_embed(
                    title="Missing Role",
                    description="You need the `Translator` role to use this command.",
                    color=EmbedColors.ERROR,
                )
            )
        else:
            logger.error(f"Unhandled error in command: {error}")
            await ctx.reply(
                embed=build_status_embed(
                    title="Command Failed",
                    description="An unexpected error occurred while processing the command.",
                    color=EmbedColors.ERROR,
                )
            )

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
            result = await self._translation_service.translate_to_english(message.content)
            translated_text = None
            source_language = None
            if result and result.get("language") != "English":
                translated_text = result.get("text")
                source_language = result.get("language")

            if translated_text:
                await self._reply_with_optional_voice(
                    message.reply,
                    content=self._as_quote_block(self._truncate_for_discord(translated_text, 1500)),
                    guild_id=message.guild.id if message.guild else None,
                    voice_text=translated_text,
                    language_hint=source_language,
                    filename_stem="translation",
                )

            # Track in database
            try:
                await self._interaction_tracking_service.track_translation(
                    user_id=message.author.id,
                    original_text=message.content,
                    translated_text=translated_text or "",
                    target_language="en",
                    source_language=source_language or "en",
                    translation_type="auto",
                    guild_id=message.guild.id if message.guild else None,
                    channel_id=message.channel.id,
                    username=message.author.name,
                    discriminator=message.author.discriminator,
                    display_name=message.author.display_name,
                )
            except Exception as db_error:
                logger.error(f"Database tracking error: {db_error}", exc_info=True)
        except Exception as e:
            logger.error(f"Auto-translation error: {e}", exc_info=True)
            # Don't send error messages for auto-translation to avoid spam

    async def _handle_contextual_chat(self, message: discord.Message):
        """Optionally reply to chat using recent message history as context."""
        if self._config and message.author.id in self._config.banned_players:
            return

        direct_trigger = await self._is_direct_mention_or_reply(message)
        if not message.content.strip() and not direct_trigger:
            return

        if not await self._should_attempt_reply(message, direct_trigger=direct_trigger):
            return

        try:
            history = await self._collect_conversation_context(message)
            reply_context = await self._collect_reply_context(message)
            async with message.channel.typing():
                reply_text = await self._chatbot_service.generate_contextual_reply(
                    self._content_for_ai(message),
                    history,
                    force_reply=direct_trigger,
                    reply_context=reply_context,
                )

            if not reply_text:
                logger.info(
                    "Contextual chat produced no reply for message %s in channel %s (direct_trigger=%s)",
                    message.id,
                    message.channel.id,
                    direct_trigger,
                )
                return

            await self._reply_with_optional_voice(
                message.reply,
                content=self._truncate_for_discord(
                    reply_text,
                    self._config.max_chat_response_chars if self._config else 500,
                ),
                mention_author=False,
                guild_id=message.guild.id if message.guild else None,
                voice_text=reply_text,
                filename_stem="chat-reply",
            )

            self._last_chat_reply_at[message.channel.id] = time.monotonic()
        except Exception as e:
            logger.error(f"Contextual chat error: {e}", exc_info=True)

    async def _reply_with_optional_voice(
        self,
        reply_callable,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        mention_author: bool | None = None,
        guild_id: int | None = None,
        voice_text: str | None = None,
        language_hint: str | None = None,
        filename_stem: str = "reply",
    ):
        reply_kwargs = {}
        if content is not None:
            reply_kwargs["content"] = content
        if embed is not None:
            reply_kwargs["embed"] = embed
        if mention_author is not None:
            reply_kwargs["mention_author"] = mention_author

        voice_file = await self._build_voice_file(
            voice_text,
            guild_id=guild_id,
            language_hint=language_hint,
            filename_stem=filename_stem,
        )
        if voice_file is not None:
            reply_kwargs["file"] = voice_file

        await reply_callable(**reply_kwargs)

    async def _build_voice_file(
        self,
        text: str | None,
        *,
        guild_id: int | None = None,
        language_hint: str | None = None,
        filename_stem: str = "reply",
    ) -> discord.File | None:
        if not self._voice_message_service or not text:
            return None

        if not await self._voice_replies_enabled_for_guild(guild_id):
            return None

        try:
            audio = await self._voice_message_service.generate_audio(
                text,
                language_hint=language_hint,
                filename_stem=filename_stem,
            )
        except Exception as exc:
            logger.error("Voice message generation failed: %s", exc, exc_info=True)
            return None

        if audio is None:
            return None

        return discord.File(BytesIO(audio.data), filename=audio.filename)

    async def _voice_replies_enabled_for_guild(self, guild_id: int | None) -> bool:
        default_enabled = getattr(self._config, "enable_voice_replies", True)
        if guild_id is None:
            return default_enabled

        if self._guild_configuration_service is not None:
            try:
                guild_config = await self._guild_configuration_service.get_or_create_for_guild(guild_id)
                if guild_config is not None:
                    return bool(guild_config.use_voice_replies)
            except Exception as exc:
                logger.error(
                    "Guild configuration lookup failed for guild %s: %s",
                    guild_id,
                    exc,
                    exc_info=True,
                )
                return default_enabled

        return default_enabled

    async def _should_attempt_reply(self, message: discord.Message, *, direct_trigger: bool | None = None) -> bool:
        """Return True if the bot should try generating a chat reply."""
        if direct_trigger is None:
            direct_trigger = self._has_direct_mention_or_resolved_reply(message)

        guild_id = message.guild.id if message.guild else None
        default_enabled = True if direct_trigger else bool(getattr(self._config, "random_reply_chance", 0) > 0)
        if not await self._ai_replies_enabled_for_guild(guild_id, default_enabled=default_enabled):
            return False

        if direct_trigger:
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

    async def _ai_replies_enabled_for_guild(self, guild_id: int | None, *, default_enabled: bool) -> bool:
        if guild_id is None:
            return default_enabled

        if self._guild_configuration_service is not None:
            try:
                guild_config = await self._guild_configuration_service.get_or_create_for_guild(guild_id)
                if guild_config is not None:
                    return bool(guild_config.use_random_replies)
            except Exception as exc:
                logger.error(
                    "Guild configuration lookup failed for AI replies in guild %s: %s",
                    guild_id,
                    exc,
                    exc_info=True,
                )
                return default_enabled

        return default_enabled

    def _has_direct_mention_or_resolved_reply(self, message: discord.Message) -> bool:
        """Detect direct triggers that do not require an API fetch."""
        if self._message_mentions_bot(message):
            return True

        reference = message.reference
        if not reference:
            return False

        resolved = getattr(reference, "resolved", None)
        return self._message_is_from_bot(resolved)

    async def _is_direct_mention_or_reply(self, message: discord.Message) -> bool:
        """Detect whether the message is directly aimed at the bot."""
        if self._has_direct_mention_or_resolved_reply(message):
            return True

        reference = message.reference
        if not reference:
            return False

        message_id = getattr(reference, "message_id", None)
        if not message_id:
            return False

        try:
            replied_to = await message.channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            logger.debug("Could not fetch referenced message %s for direct reply detection", message_id, exc_info=True)
            return False

        return self._message_is_from_bot(replied_to)

    def _message_mentions_bot(self, message: discord.Message) -> bool:
        bot_user = self._bot.user
        if not bot_user:
            return False

        bot_id = getattr(bot_user, "id", None)
        return any(mention == bot_user or getattr(mention, "id", None) == bot_id for mention in message.mentions)

    def _message_is_from_bot(self, message: object) -> bool:
        bot_user = self._bot.user
        if not message or not bot_user:
            return False

        author = getattr(message, "author", None)
        if not author:
            return False

        bot_id = getattr(bot_user, "id", None)
        return author == bot_user or getattr(author, "id", None) == bot_id

    @staticmethod
    def _content_for_ai(message: discord.Message) -> str:
        """Return content with Discord mention tokens resolved to readable names."""
        clean_content = getattr(message, "clean_content", None)
        if isinstance(clean_content, str):
            return clean_content.strip()
        return str(getattr(message, "content", "") or "").strip()

    async def _collect_conversation_context(self, message: discord.Message) -> List[Dict[str, object]]:
        """Collect recent messages above the current one for context."""
        history_limit = self._config.chat_history_limit if self._config else 25
        collected: List[Dict[str, object]] = []

        async for previous in message.channel.history(limit=history_limit, before=message, oldest_first=False):
            if previous.author.bot and previous.author != self._bot.user:
                continue

            content = self._content_for_ai(previous)
            if not content:
                continue

            collected.append(
                {
                    "author": previous.author.display_name,
                    "is_bot": previous.author == self._bot.user,
                    "timestamp": previous.created_at.isoformat(),
                    "content": content,
                }
            )

        collected.reverse()
        return collected

    async def _collect_reply_context(self, message: discord.Message) -> Dict[str, object] | None:
        """Collect the exact message this message replies to, if available."""
        reference = message.reference
        if not reference:
            return None

        resolved = getattr(reference, "resolved", None)
        replied_to = resolved if isinstance(resolved, discord.Message) else None

        if replied_to is None and reference.message_id:
            try:
                replied_to = await message.channel.fetch_message(reference.message_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                logger.debug("Could not fetch replied-to message %s", reference.message_id, exc_info=True)
                return None

        if replied_to is None:
            return None

        content = self._content_for_ai(replied_to)
        if not content:
            return None

        return {
            "author": replied_to.author.display_name,
            "is_bot": replied_to.author == self._bot.user,
            "timestamp": replied_to.created_at.isoformat(),
            "content": content,
        }

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
