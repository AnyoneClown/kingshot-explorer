import contextlib
import logging
import random
import re
import time
from typing import Dict, List

import discord
from discord import app_commands
from discord.ext import commands

from handlers.ui import EmbedColors, build_status_embed
from services.chat_tools import ToolContext
from services.chatbot_service import IChatbotService
from services.guild_configuration_service import GuildConfigurationService
from services.interaction_tracking_service import InteractionTrackingService
from services.text_utils import split_for_discord, truncate_cleanly
from services.translation_service import (
    ITranslationService,
    TranslationUnavailableError,
    UnknownLanguageError,
    has_translatable_text,
    is_english_language,
    looks_clearly_english,
)

logger = logging.getLogger(__name__)

# Messages that are commands for some bot ("!rank", "/roll"), not text to translate.
_COMMAND_LIKE = re.compile(r"[!$/?.][A-Za-z]")

# Model output (translations, chat replies) must never ping anyone.
NO_MENTIONS = discord.AllowedMentions.none()

# Discord client languages -> the language the "Translate" message command targets.
LOCALE_LANGUAGES = {
    "en-US": "English", "en-GB": "English", "uk": "Ukrainian", "ru": "Russian", "de": "German",
    "fr": "French", "es-ES": "Spanish", "es-419": "Spanish", "pt-BR": "Portuguese", "it": "Italian",
    "pl": "Polish", "tr": "Turkish", "nl": "Dutch", "cs": "Czech", "ro": "Romanian", "hu": "Hungarian",
    "el": "Greek", "bg": "Bulgarian", "hr": "Croatian", "lt": "Lithuanian", "sv-SE": "Swedish",
    "da": "Danish", "fi": "Finnish", "no": "Norwegian", "ja": "Japanese", "ko": "Korean",
    "zh-CN": "Chinese (Simplified)", "zh-TW": "Chinese (Traditional)", "vi": "Vietnamese",
    "th": "Thai", "id": "Indonesian", "hi": "Hindi",
}
TRANSLATE_MENU_COOLDOWN_SECONDS = 5.0


def language_for_locale(locale) -> str:
    """Language name for a Discord locale (English when it is unknown)."""
    return LOCALE_LANGUAGES.get(str(getattr(locale, "value", locale) or ""), "English")


class TranslationHandler:
    """Handles translation-related Discord commands and events."""

    DEFAULT_CHAT_USER_COOLDOWN_SECONDS = 10.0
    EMBED_TEXT_LIMIT = 3800
    MESSAGE_CHUNK_LIMIT = 1800
    MAX_TRANSLATION_MESSAGES = 3

    def __init__(
        self,
        translation_service: ITranslationService,
        chatbot_service: IChatbotService,
        bot: commands.Bot,
        config=None,
        interaction_tracking_service: InteractionTrackingService | None = None,
        guild_configuration_service: GuildConfigurationService | None = None,
        access_policy=None,
    ):
        """
        Initialize translation handler.

        Args:
            translation_service: Service for handling translations
            chatbot_service: Service for contextual conversational replies
            bot: Discord bot instance
            config: Bot configuration (role name, chat limits, legacy banned list)
            access_policy: ``AccessPolicy``; banned users are ignored everywhere
        """
        self._translation_service = translation_service
        self._chatbot_service = chatbot_service
        self._bot = bot
        self._config = config
        self._last_chat_reply_at: Dict[int, float] = {}
        self._last_user_reply_at: Dict[int, float] = {}
        self._interaction_tracking_service = interaction_tracking_service or InteractionTrackingService()
        self._guild_configuration_service = guild_configuration_service
        self._access = access_policy

    def register_commands(self):
        """Register all translation commands with the bot."""

        @self._bot.command(name="t", aliases=["translate"])
        async def translate_command(ctx, *, target_language: str):
            """Translates a replied-to message into a specified language."""
            await self._handle_translate_to_language(ctx, target_language)

        @translate_command.error
        async def translate_command_error(ctx, error):
            """Handles errors for the !t command (e.g. a missing language)."""
            await self._handle_command_error(ctx, error)

        @self._bot.command(name="en")
        async def en_command(ctx):
            """Translates a replied-to message into English."""
            await self._handle_translate_to_english(ctx)

        @en_command.error
        async def en_command_error(ctx, error):
            """Handles errors for the !en command."""
            await self._handle_command_error(ctx, error)

        tree = getattr(self._bot, "tree", None)
        if tree is not None and hasattr(tree, "context_menu"):

            @tree.context_menu(name="Translate")
            @app_commands.checks.cooldown(1, TRANSLATE_MENU_COOLDOWN_SECONDS)
            async def translate_message(interaction: discord.Interaction, message: discord.Message):
                """Right-click a message → Apps → Translate: a private translation."""
                await self._handle_translate_message_menu(interaction, message)

    def register_events(self):
        """Register message events for automatic translation."""

        @self._bot.event
        async def on_message(message):
            if message.author == self._bot.user or message.author.bot:
                return

            # "X pinned a message", thread-created notices and joins are not chat: their
            # content is a name or empty, and they cannot be replied to.
            is_system = getattr(message, "is_system", None)
            if callable(is_system) and is_system() is True:
                return

            # Banned users are ignored entirely: no prefix commands, translation or chat.
            if self._is_banned(message.author.id):
                return

            await self._bot.process_commands(message)

            # Stop if command or DM
            if message.content.startswith(self._bot.command_prefix) or not message.guild:
                return

            await self._handle_auto_translation(message)
            await self._handle_contextual_chat(message)

    # ------------------------------------------------------------------ access

    def _is_banned(self, user_id) -> bool:
        if self._access is not None:
            try:
                if self._access.is_banned(user_id):
                    return True
            except Exception:
                logger.debug("Access policy ban check failed for %s", user_id, exc_info=True)
        banned = getattr(self._config, "banned_players", None) or ()
        return user_id in banned

    async def _reply_blocked(self, ctx) -> None:
        await ctx.reply(
            embed=build_status_embed(
                title="Translation Blocked",
                description="You are currently blocked from using translation commands.",
                color=EmbedColors.ERROR,
            )
        )

    @staticmethod
    def _message_text(message) -> str:
        """Message text; a forwarded message has empty content and keeps it in its snapshot."""
        content = getattr(message, "content", None) or ""
        if content.strip():
            return content
        snapshots = getattr(message, "message_snapshots", None)
        for snapshot in snapshots if isinstance(snapshots, (list, tuple)) else ():
            snapshot_content = getattr(snapshot, "content", None)
            if isinstance(snapshot_content, str) and snapshot_content.strip():
                return snapshot_content
        return content

    @staticmethod
    def _unavailable_embed() -> discord.Embed:
        return build_status_embed(
            title="Translation Unavailable",
            description="The translation service is unavailable right now. Try again in a moment.",
            color=EmbedColors.ERROR,
        )

    @staticmethod
    def _nothing_to_translate_embed() -> discord.Embed:
        return build_status_embed(
            title="Nothing to Translate",
            description="The replied-to message only contains emoji, mentions, links or numbers.",
            color=EmbedColors.WARNING,
        )

    async def _fetch_replied_text(self, ctx) -> str | None:
        """Return the replied-to message text, replying with an explanation when unusable."""
        try:
            original_message = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            await ctx.reply(
                embed=build_status_embed(
                    title="Message Unavailable",
                    description="I couldn't read the replied-to message. It may have been deleted.",
                    color=EmbedColors.WARNING,
                )
            )
            return None

        text_to_translate = self._message_text(original_message)
        if not text_to_translate:
            await ctx.reply(
                embed=build_status_embed(
                    title="Empty Message",
                    description="The replied-to message does not contain text to translate.",
                    color=EmbedColors.WARNING,
                )
            )
            return None
        if not has_translatable_text(text_to_translate):
            await ctx.reply(embed=self._nothing_to_translate_embed())
            return None
        return text_to_translate

    # ------------------------------------------------------------------ commands

    async def _handle_translate_message_menu(self, interaction: discord.Interaction, message: discord.Message):
        """Translate one message into the caller's Discord language, visible only to them."""
        language = language_for_locale(getattr(interaction, "locale", None))
        text = self._message_text(message)
        if not text.strip() or not has_translatable_text(text):
            await interaction.response.send_message(embed=self._nothing_to_translate_embed(), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await self._translation_service.translate_to_language(text, language)
        except TranslationUnavailableError:
            await interaction.followup.send(embed=self._unavailable_embed(), ephemeral=True)
            return
        except Exception as e:
            logger.error(f"Error in the Translate message command: {e}", exc_info=True)
            await interaction.followup.send(embed=self._unavailable_embed(), ephemeral=True)
            return

        translated_text = (result or {}).get("text")
        if not translated_text:
            await interaction.followup.send(embed=self._nothing_to_translate_embed(), ephemeral=True)
            return
        if self._same_text(translated_text, text):
            await interaction.followup.send(
                embed=build_status_embed(
                    title=f"Already {language}",
                    description=f"This message already appears to be in {language}.",
                    color=EmbedColors.SUCCESS,
                ),
                ephemeral=True,
            )
            return

        author_name = getattr(getattr(message, "author", None), "display_name", None)
        embed = build_status_embed(
            title=f"Translated to {language}",
            description=self._quote_for_embed(translated_text),
            color=EmbedColors.INFO,
            footer=(f"Message by {author_name} · " if author_name else "") + "Only you can see this",
        )
        extras = {}
        jump_url = getattr(message, "jump_url", None)
        if jump_url:
            view = discord.ui.View()
            view.add_item(discord.ui.Button(label="Original message", url=jump_url))
            extras["view"] = view
        await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=NO_MENTIONS, **extras)
        await self._track_translation(
            interaction.user,
            original_text=text,
            translated_text=translated_text,
            target_language=language,
            source_language=(result or {}).get("language"),
            translation_type="context_menu",
            guild=getattr(interaction, "guild", None),
            channel=getattr(interaction, "channel", None) or getattr(message, "channel", None),
        )

    async def _handle_translate_to_language(self, ctx, target_language: str):
        """Handle translation to a specific language command."""
        if self._is_banned(ctx.author.id):
            await self._reply_blocked(ctx)
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
            text_to_translate = await self._fetch_replied_text(ctx)
            if text_to_translate is None:
                return

            try:
                async with ctx.typing():
                    result = await self._translation_service.translate_to_language(text_to_translate, target_language)
            except UnknownLanguageError:
                await ctx.reply(
                    embed=build_status_embed(
                        title="Unknown Language",
                        description=(
                            f"I don't recognise `{truncate_cleanly(target_language, 40)}` as a language. "
                            "Try a common language name like `Spanish`, `French`, or `English`."
                        ),
                        color=EmbedColors.WARNING,
                    ),
                    allowed_mentions=NO_MENTIONS,
                )
                return
            except TranslationUnavailableError:
                await ctx.reply(embed=self._unavailable_embed())
                return

            translated_text = (result or {}).get("text")
            if not translated_text:
                await ctx.reply(embed=self._nothing_to_translate_embed())
                return

            embed = build_status_embed(
                title=f"Translated to {truncate_cleanly(target_language, 60)}",
                description=self._quote_for_embed(translated_text),
                color=EmbedColors.INFO,
            )
            await self._send_reply(
                ctx.reply,
                embed=embed,
            )

            await self._track_translation(
                ctx.author,
                original_text=text_to_translate,
                translated_text=translated_text,
                target_language=target_language,
                source_language=result.get("language"),
                translation_type="command",
                guild=ctx.guild,
                channel=ctx.channel,
            )
        except Exception as e:
            logger.error(f"Error in translate command: {e}", exc_info=True)
            await ctx.reply(embed=self._unavailable_embed())

    async def _handle_translate_to_english(self, ctx):
        """Handle translation to English command."""
        if self._is_banned(ctx.author.id):
            await self._reply_blocked(ctx)
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
            text_to_translate = await self._fetch_replied_text(ctx)
            if text_to_translate is None:
                return

            try:
                async with ctx.typing():
                    result = await self._translation_service.translate_to_english(text_to_translate)
            except TranslationUnavailableError:
                await ctx.reply(embed=self._unavailable_embed())
                return

            if not result or not result.get("text"):
                await ctx.reply(embed=self._nothing_to_translate_embed())
                return

            translated_text = result["text"]
            source_language = result.get("language")
            if is_english_language(source_language) or self._same_text(translated_text, text_to_translate):
                await ctx.reply(
                    embed=build_status_embed(
                        title="Already English",
                        description="The replied-to message already appears to be in English.",
                        color=EmbedColors.SUCCESS,
                    )
                )
                return

            embed = build_status_embed(
                title=f"Translated from {truncate_cleanly(str(source_language or 'unknown'), 60)}",
                description=self._quote_for_embed(translated_text),
                color=EmbedColors.INFO,
            )
            await self._send_reply(
                ctx.reply,
                embed=embed,
            )

            await self._track_translation(
                ctx.author,
                original_text=text_to_translate,
                translated_text=translated_text,
                target_language="en",
                source_language=source_language,
                translation_type="command",
                guild=ctx.guild,
                channel=ctx.channel,
            )
        except Exception as e:
            logger.error(f"Error in !en command: {e}", exc_info=True)
            await ctx.reply(embed=self._unavailable_embed())

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
        elif isinstance(error, commands.MissingRequiredArgument):
            prefix = getattr(ctx, "clean_prefix", None) or "!"
            await ctx.reply(
                embed=build_status_embed(
                    title="Language Required",
                    description=f"Reply to a message with `{prefix}t <language>`, for example `{prefix}t spanish`.",
                    color=EmbedColors.WARNING,
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

    # ------------------------------------------------------------------ auto translation

    def _has_translator_role(self, message: discord.Message) -> bool:
        role_name = self._config.translator_role_name if self._config else "Translator"
        guild = getattr(message, "guild", None)
        if guild is None:
            return False
        role = discord.utils.get(guild.roles, name=role_name)
        return bool(role and role in getattr(message.author, "roles", ()))

    async def _handle_auto_translation(self, message: discord.Message):
        """Automatically translate messages from users with Translator role."""
        content = message.content or ""
        # Skip empty or command-like messages ("!rank", "?help"); "?? 왜" and "$100 es
        # mucho" are ordinary chat.
        if not content.strip() or _COMMAND_LIKE.match(content):
            return

        if self._is_banned(message.author.id):
            logger.debug(f"User {message.author.id} is banned from auto-translation")
            return

        if not self._has_translator_role(message):
            return

        # Cheap local gates before paying for an LLM call.
        if not has_translatable_text(content) or looks_clearly_english(content):
            return

        try:
            result = await self._translation_service.translate_to_english(content)
        except TranslationUnavailableError as exc:
            logger.warning("Auto-translation skipped, service unavailable: %s", exc)
            return
        except Exception as e:
            logger.error(f"Auto-translation error: {e}", exc_info=True)
            return

        if not result:
            return
        translated_text = result.get("text")
        source_language = result.get("language")
        if (
            not translated_text
            or is_english_language(source_language)
            or self._same_text(translated_text, content)
        ):
            return

        try:
            chunks = self._quoted_chunks(translated_text)
            await self._send_reply(
                message.reply,
                content=chunks[0],
                mention_author=False,
            )
            for chunk in chunks[1:]:
                await message.channel.send(content=chunk, allowed_mentions=NO_MENTIONS)
        except Exception as e:
            logger.error(f"Auto-translation reply failed: {e}", exc_info=True)
            return

        await self._track_translation(
            message.author,
            original_text=content,
            translated_text=translated_text,
            target_language="en",
            source_language=source_language,
            translation_type="auto",
            guild=message.guild,
            channel=message.channel,
        )

    async def _track_translation(
        self,
        author,
        *,
        original_text: str,
        translated_text: str,
        target_language: str,
        source_language: str | None,
        translation_type: str,
        guild,
        channel,
    ) -> None:
        try:
            await self._interaction_tracking_service.track_translation(
                user_id=author.id,
                original_text=original_text,
                translated_text=translated_text,
                # The columns are VARCHAR(50); a chatty model-reported language name
                # must not lose the whole row.
                target_language=str(target_language)[:50],
                source_language=str(source_language)[:50] if source_language else None,
                translation_type=translation_type,
                guild_id=guild.id if guild else None,
                channel_id=getattr(channel, "id", None),
                username=author.name,
                discriminator=author.discriminator,
                display_name=author.display_name,
            )
        except Exception as db_error:
            logger.error(f"Database tracking error: {db_error}", exc_info=True)

    # ------------------------------------------------------------------ contextual chat

    async def _handle_contextual_chat(self, message: discord.Message):
        """Optionally reply to chat using recent message history as context."""
        if self._is_banned(message.author.id):
            return

        guild_id = message.guild.id if message.guild else None
        direct_trigger = self._has_direct_mention_or_resolved_reply(message)
        if not direct_trigger and self._has_unresolved_reference(message):
            # Only pay for the referenced-message fetch when replies are on for this server.
            if await self._ai_replies_enabled_for_guild(guild_id, default_enabled=True):
                direct_trigger = await self._is_direct_mention_or_reply(message)

        if not (message.content or "").strip() and not direct_trigger:
            return

        if not await self._should_attempt_reply(message, direct_trigger=direct_trigger):
            return

        if direct_trigger:
            # Random candidates are limited by the channel cooldown; they must not use up
            # the user's slot, or a real question right afterwards would be ignored.
            if not self._claim_user_reply_slot(message.author.id):
                logger.debug("Chat reply skipped for user %s: per-user cooldown", message.author.id)
                return
        else:
            # Claim the channel before generating: a second candidate arriving while the
            # model is still writing must not produce a second random reply.
            self._last_chat_reply_at[message.channel.id] = time.monotonic()

        try:
            history = await self._collect_conversation_context(message)
            reply_context = await self._collect_reply_context(message)
            tool_context = (
                ToolContext(guild_id=guild_id, channel_id=message.channel.id, user_id=message.author.id)
                if direct_trigger and guild_id
                else None
            )
            typing = message.channel.typing() if direct_trigger else contextlib.nullcontext()
            async with typing:
                reply_text = await self._chatbot_service.generate_contextual_reply(
                    self._content_for_ai(message),
                    history,
                    force_reply=direct_trigger,
                    reply_context=reply_context,
                    tool_context=tool_context,
                )

            if not reply_text:
                logger.info(
                    "Contextual chat produced no reply for message %s in channel %s (direct_trigger=%s)",
                    message.id,
                    message.channel.id,
                    direct_trigger,
                )
                return

            await self._send_reply(
                message.reply,
                content=truncate_cleanly(
                    str(reply_text),
                    self._config.max_chat_response_chars if self._config else 500,
                ),
                mention_author=False,
            )

            self._last_chat_reply_at[message.channel.id] = time.monotonic()
        except Exception as e:
            logger.error(f"Contextual chat error: {e}", exc_info=True)

    def _claim_user_reply_slot(self, user_id: int) -> bool:
        """Allow at most one AI reply attempt per user per cooldown window."""
        cooldown = getattr(self._config, "chat_user_cooldown_seconds", None)
        if cooldown is None:
            cooldown = self.DEFAULT_CHAT_USER_COOLDOWN_SECONDS
        cooldown = float(cooldown)
        if cooldown <= 0:
            return True

        now = time.monotonic()
        last = self._last_user_reply_at.get(user_id)
        if last is not None and now - last < cooldown:
            return False
        self._last_user_reply_at[user_id] = now
        if len(self._last_user_reply_at) > 1000:
            self._last_user_reply_at = {
                uid: stamp for uid, stamp in self._last_user_reply_at.items() if now - stamp < cooldown
            }
        return True

    # ------------------------------------------------------------------ replies

    async def _send_reply(
        self,
        reply_callable,
        *,
        content: str | None = None,
        embed: discord.Embed | None = None,
        mention_author: bool = False,
    ):
        """Send a reply that never pings anyone."""
        reply_kwargs = {"allowed_mentions": NO_MENTIONS, "mention_author": mention_author}
        if content is not None:
            reply_kwargs["content"] = content
        if embed is not None:
            reply_kwargs["embed"] = embed
        return await reply_callable(**reply_kwargs)

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

        content = (message.content or "").strip()
        if len(content) < 4:
            return False

        # Cheap heuristics: random replies need real words and some substance.
        if not has_translatable_text(content):
            return False
        if len(content.split()) < 3 and "?" not in content:
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
                # AI replies are opt-in per server: a database hiccup must not switch
                # them on where nobody enabled them.
                return False

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

    @staticmethod
    def _has_unresolved_reference(message: discord.Message) -> bool:
        """True when the message replies to something Discord did not include in the event."""
        reference = getattr(message, "reference", None)
        if not reference or not getattr(reference, "message_id", None):
            return False
        return getattr(reference, "resolved", None) is None

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
        if any(
            mention == bot_user or getattr(mention, "id", None) == bot_id
            for mention in getattr(message, "mentions", ())
        ):
            return True
        # Typing "@<bot name>" also offers the bot's own managed role; picking it still
        # addresses the bot, so it must not fall through to the random-reply path.
        role_ids = getattr(message, "raw_role_mentions", None) or ()
        if not role_ids:
            return False
        self_role = getattr(getattr(message, "guild", None), "self_role", None)
        return self_role is not None and getattr(self_role, "id", None) in role_ids

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

    # ------------------------------------------------------------------ formatting

    @staticmethod
    def _same_text(left: str | None, right: str | None) -> bool:
        def normalize(value: str | None) -> str:
            return " ".join(str(value or "").split()).casefold()

        return normalize(left) == normalize(right)

    @staticmethod
    def _truncate_for_discord(text: str, limit: int = 1500) -> str:
        """Truncate long text at a natural boundary to avoid Discord message limits."""
        return truncate_cleanly(text, limit)

    @classmethod
    def _quote_for_embed(cls, text: str, limit: int = 4000) -> str:
        quoted = cls._as_quote_block(truncate_cleanly(text, cls.EMBED_TEXT_LIMIT))
        return quoted if len(quoted) <= limit else truncate_cleanly(quoted, limit)

    @classmethod
    def _quote_for_message(cls, text: str, limit: int = 2000) -> str:
        quoted = cls._as_quote_block(text)
        return quoted if len(quoted) <= limit else truncate_cleanly(quoted, limit)

    def _quoted_chunks(self, text: str) -> list[str]:
        """Split a long translation into quoted messages that each fit Discord's limit.

        Quoting adds two characters per line, so a chunk of many short lines can pass
        2000 characters after it was split; the split is redone with a smaller limit
        instead of cutting lines out of the middle of the translation.
        """
        limit = self.MESSAGE_CHUNK_LIMIT
        while True:
            chunks = split_for_discord(text, limit, max_chunks=self.MAX_TRANSLATION_MESSAGES)
            quoted = [self._as_quote_block(chunk) for chunk in chunks]
            if all(len(chunk) <= 2000 for chunk in quoted) or limit <= 400:
                return [chunk if len(chunk) <= 2000 else truncate_cleanly(chunk, 2000) for chunk in quoted]
            limit -= 200

    @staticmethod
    def _as_quote_block(text: str) -> str:
        """Convert arbitrary text to Discord quote block formatting."""
        lines = text.splitlines() or [text]
        return "\n".join(f"> {line}" if line else ">" for line in lines)
