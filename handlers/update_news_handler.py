"""Posts the bot's update news in every server after a deploy.

What there is to say comes from ``services.update_news``. Once the bot is ready, each
server gets the notes it has not seen yet, in the channel its managers chose in
``/settings`` (the server's general channel when they chose none), written in the
server's chat languages. A server that switched update news off is caught up
silently, so switching it back on never replays old notes.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Optional, Sequence

import discord

from handlers.setup_prompts import bot_can_post
from handlers.ui import EmbedColors
from services.translation_languages import ENGLISH, Language, languages_from_setting
from services.update_news import UpdateNote, latest_id, load_update_news, pending_notes

logger = logging.getLogger(__name__)

NO_MENTIONS = discord.AllowedMentions.none()
# The command is in backticks for the translator, which leaves code spans alone; a
# footer shows no markdown, so they are taken out again before posting.
NEWS_FOOTER_SOURCE = "Update news from the bot · server managers can change the channel or turn it off in `/settings`"
NEWS_FOOTER = NEWS_FOOTER_SOURCE.replace("`", "")
GENERAL_CHANNEL_NAME = "general"
# Discord's limits: all embeds of one message together, and the parts of one embed.
MESSAGE_EMBED_CHARS = 5800
EMBED_TITLE_CHARS = 256
EMBED_TEXT_CHARS = 4096
EMBED_FOOTER_CHARS = 2048

_CODE_SPAN = re.compile(r"`[^`\n]+`")


def general_channel(guild):
    """Where a server reads news when its managers chose no channel.

    Discord's own system channel first, then a text channel called "general"; None
    when the bot can post in neither. Never a guess at some other channel.
    """
    candidates = [getattr(guild, "system_channel", None)]
    candidates += [
        channel
        for channel in getattr(guild, "text_channels", None) or ()
        if str(getattr(channel, "name", "")).casefold() == GENERAL_CHANNEL_NAME
    ]
    return next((channel for channel in candidates if channel is not None and bot_can_post(channel)), None)


def news_channel(guild, settings):
    """The channel update news goes to in ``guild`` now, or None when there is nowhere to post.

    A chosen channel that is gone or closed to the bot is not replaced by the general
    channel: the managers moved the news away from there on purpose.
    """
    chosen = getattr(settings, "update_news_channel_id", None)
    if not chosen:
        return general_channel(guild)
    channel = guild.get_channel(chosen)
    return channel if channel is not None and bot_can_post(channel) else None


def describe_destination(guild, settings) -> str:
    """Where update news goes, as the settings panel says it."""
    if not getattr(settings, "update_news_enabled", True):
        return "**Off**"
    chosen = getattr(settings, "update_news_channel_id", None)
    if chosen:
        return f"<#{chosen}>"
    if guild is None:
        return "the server's general channel"
    channel = general_channel(guild)
    if channel is None:
        return "no general channel I can post in · press **Update news** to pick a channel"
    return f"<#{channel.id}> · the general channel"


def keeps_code_spans(original: str, translated: str) -> bool:
    """Whether every `code` span of ``original`` (command names, button labels) survived translation as written."""
    return all(span in translated for span in _CODE_SPAN.findall(original))


class UpdateNewsHandler:
    """Gives every server the update notes it has not seen, once per start of the bot."""

    def __init__(
        self,
        bot,
        guild_configuration_service,
        translation_service=None,
        *,
        notes: Optional[Sequence[UpdateNote]] = None,
    ):
        self._bot = bot
        self._guild_config_service = guild_configuration_service
        self._translation_service = translation_service
        self._notes = tuple(notes) if notes is not None else None
        # (text, language code) -> its translation, or None when it could not be made.
        self._translations: dict[tuple[str, str], Optional[str]] = {}
        self._task: asyncio.Task | None = None

    @property
    def notes(self) -> tuple[UpdateNote, ...]:
        if self._notes is None:
            self._notes = load_update_news()
        return self._notes

    def start(self) -> None:
        """Post what is pending, in the background. Later calls (reconnects) do nothing."""
        if self._task is None:
            self._task = asyncio.create_task(self.post_pending())

    async def mark_seen(self, guild_id: int) -> None:
        """A server that just added the bot starts from today: it is not told the past."""
        newest = latest_id(self.notes)
        if newest is None:
            return
        try:
            await self._guild_config_service.update_for_guild(guild_id, update_news_last_id=newest)
        except Exception:
            logger.warning("Could not mark update news as seen for guild %s", guild_id, exc_info=True)

    async def post_pending(self) -> int:
        """Post the pending notes of every server; returns how many notes were posted."""
        notes = self.notes
        if not notes:
            return 0
        posted = 0
        for guild in list(self._bot.guilds):
            try:
                posted += await self._post_for_guild(guild, notes)
            except Exception:
                logger.exception("Update news failed for guild %s", getattr(guild, "id", "?"))
        if posted:
            logger.info("Posted %s update note(s)", posted)
        return posted

    async def _post_for_guild(self, guild, notes: Sequence[UpdateNote]) -> int:
        settings = await self._guild_config_service.get_settings(guild.id)
        seen = getattr(settings, "update_news_last_id", None)
        newest = notes[-1].id
        if seen == newest:
            return 0
        pending = pending_notes(notes, seen)
        if not pending or not getattr(settings, "update_news_enabled", True):
            # Nothing to say (the marked note was withdrawn) or news is off here: catch
            # up silently, so the next note, or switching news on, starts from now.
            await self._guild_config_service.claim_update_news(guild.id, seen, newest)
            return 0
        channel = news_channel(guild, settings)
        if channel is None:
            # The marker stays: the notes are posted after a start that finds a channel.
            logger.info("No channel for update news in guild %s", guild.id)
            return 0
        # Claimed before posting: a second instance of the bot must not post it too.
        if not await self._guild_config_service.claim_update_news(guild.id, seen, newest):
            return 0
        languages = languages_from_setting(getattr(settings, "translation_languages", None)) or (ENGLISH,)
        sent = 0
        try:
            for note in pending:
                for embeds in await self._messages(note, languages):
                    await channel.send(embeds=embeds, allowed_mentions=NO_MENTIONS)
                sent += 1
        except discord.HTTPException:
            logger.warning("Could not post update news in guild %s", guild.id, exc_info=True)
            # Give back what was not posted, for the next start.
            await self._guild_config_service.claim_update_news(guild.id, newest, pending[sent - 1].id if sent else seen)
        return sent

    async def _messages(self, note: UpdateNote, languages: Sequence[Language]) -> list[list[discord.Embed]]:
        """One embed per language, grouped into as few messages as Discord's size limit allows."""
        versions: list[tuple[Language, str, str, str]] = []
        for language in languages:
            title, text, footer = await self._localized(note, language)
            if not any(text == other[2] for other in versions):
                versions.append((language, title, text, footer))
        embeds = []
        for index, (language, title, text, footer) in enumerate(versions):
            mark = language.flag if len(versions) > 1 else "📰"
            embed = discord.Embed(
                title=f"{mark} {title}"[:EMBED_TITLE_CHARS], description=text[:EMBED_TEXT_CHARS], color=EmbedColors.INFO
            )
            if index == len(versions) - 1:
                embed.set_footer(text=footer[:EMBED_FOOTER_CHARS])
            embeds.append(embed)
        groups: list[list[discord.Embed]] = [[]]
        for embed in embeds:
            if groups[-1] and sum(len(item) for item in groups[-1]) + len(embed) > MESSAGE_EMBED_CHARS:
                groups.append([])
            groups[-1].append(embed)
        return groups

    async def _localized(self, note: UpdateNote, language: Language) -> tuple[str, str, str]:
        """Title, text and footer of ``note`` in ``language``; English for whatever cannot be translated."""
        if language.code == ENGLISH.code:
            return note.title, note.text, NEWS_FOOTER
        title, text, footer = [
            await self._translate(part, language) for part in (note.title, note.text, NEWS_FOOTER_SOURCE)
        ]
        if text is None:
            # Without its text a note is not translated at all: no mixed-language posts.
            return note.title, note.text, NEWS_FOOTER
        return title or note.title, text, footer.replace("`", "") if footer else NEWS_FOOTER

    async def _translate(self, text: str, language: Language) -> Optional[str]:
        key = (text, language.code)
        if key not in self._translations:
            self._translations[key] = await self._request_translation(text, language)
        return self._translations[key]

    async def _request_translation(self, text: str, language: Language) -> Optional[str]:
        if self._translation_service is None:
            return None
        try:
            result = await self._translation_service.translate_to_language(text, language.name)
        except Exception:
            logger.warning("Could not translate update news into %s", language.name, exc_info=True)
            return None
        translated = str((result or {}).get("text") or "").strip()
        if not translated or not keeps_code_spans(text, translated):
            logger.warning("Unusable %s translation of update news; posting it in English", language.name)
            return None
        return translated
