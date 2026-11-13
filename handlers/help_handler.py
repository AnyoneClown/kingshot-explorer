"""``/help``: one private screen of what the bot does, with clickable commands.

The commands are Discord command mentions (``</gift:123>``): tapping one opens the
real command with Discord's own option hints, validation and autocomplete, so the
help screen needs no buttons or forms of its own.
"""

import asyncio
import logging
import time
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands

from handlers.permissions import AccessPolicy
from handlers.ui import EmbedColors

logger = logging.getLogger(__name__)

# (section title, manager-only, ((command, what it is for), ...)); a command inside a
# group is named with a space, like Discord shows it ("giftadmin panel").
SECTIONS = (
    (
        "🎁 Gift codes",
        False,
        (
            (
                "gift",
                "add your Governor ID and new codes are redeemed for you automatically; "
                "see your accounts, copy the active codes, send in a code the bot has not found yet",
            ),
        ),
    ),
    (
        "🔎 Players and kingdoms",
        False,
        (
            ("player", "a player's profile: stats, heroes and governor gear"),
            ("scout", "the top 5 Mystic Trial players of a kingdom"),
            ("leaderboard", "rankings: power, kills, alliances, trials"),
            ("kvk", "a kingdom's KvK history, or two kingdoms compared"),
            ("calendar", "the kingdom's events today, this week and next week"),
        ),
    ),
    (
        "⏰ Reminders",
        False,
        (("reminders", "this channel's reminders; server managers create, edit and cancel them there"),),
    ),
    (
        "🛠️ Server managers",
        True,
        (
            ("settings", "the server's kingdom, channels, timezone, manager role and AI replies"),
            ("giftadmin panel", "post the gift-code panel with buttons in a channel"),
            ("giftadmin import", "add a whole alliance to the gift-code list"),
            ("giftadmin roster", "the accounts of this server's kingdom: enable, disable, remove"),
            ("giftadmin redeem", "hand the bot a code, or run a known code again"),
        ),
    ),
    ("ℹ️ Bot", False, (("status", "is the bot healthy"),)),
)
# Commands that only make sense inside a server.
GUILD_ONLY_COMMANDS = {"gift", "reminders", "settings"}
KINGDOM_HINT = "Commands that take a kingdom use this server's kingdom when you leave it empty."
# After a failed lookup of the command IDs, wait this long before asking Discord again.
COMMAND_ID_RETRY_SECONDS = 600.0
COMMAND_ID_FETCH_TIMEOUT_SECONDS = 2.0


class HelpHandler:
    def __init__(
        self,
        bot: commands.Bot,
        admin_user_ids: set[int] | None = None,
        access_policy: AccessPolicy | None = None,
    ):
        self._bot = bot
        self._admin_user_ids = admin_user_ids if admin_user_ids is not None else set()
        self._access_policy = access_policy
        # Top-level command name -> Discord command ID, for clickable mentions.
        self._command_ids: dict[str, int] = {}
        self._command_ids_retry_at = 0.0

    @property
    def _access(self) -> AccessPolicy:
        # Without an injected policy, read the admin set live so changes apply immediately.
        return self._access_policy or AccessPolicy(self._admin_user_ids)

    # ------------------------------------------------------------ command IDs

    def remember_commands(self, synced: Iterable) -> None:
        """Keep the IDs of the synced commands (called after ``tree.sync()``)."""
        for command in synced or ():
            name, command_id = getattr(command, "name", None), getattr(command, "id", None)
            if name and command_id:
                self._command_ids[str(name)] = int(command_id)

    async def _ensure_command_ids(self) -> None:
        """Load the command IDs when the sync did not provide them; never raises."""
        if self._command_ids or time.monotonic() < self._command_ids_retry_at:
            return
        self._command_ids_retry_at = time.monotonic() + COMMAND_ID_RETRY_SECONDS
        fetch = getattr(getattr(self._bot, "tree", None), "fetch_commands", None)
        if not callable(fetch):
            return
        try:
            # Short: /help must answer within Discord's three seconds.
            self.remember_commands(await asyncio.wait_for(fetch(), COMMAND_ID_FETCH_TIMEOUT_SECONDS))
        except Exception:
            logger.info("Could not load command IDs for /help; commands are shown as text", exc_info=True)

    def mention(self, name: str) -> str:
        """A clickable mention of a command, or its name as text when the ID is unknown."""
        command_id = self._command_ids.get(name.split()[0])
        return f"</{name}:{command_id}>" if command_id else f"`/{name}`"

    # ----------------------------------------------------------------- content

    def find_command(self, name: str):
        """Resolve "giftadmin panel" style names: a top-level command or a subcommand of a group."""
        first, *rest = name.split()
        command = self._bot.tree.get_command(first)
        for part in rest:
            command = command.get_command(part) if isinstance(command, app_commands.Group) else None
        return None if isinstance(command, app_commands.Group) else command

    def build_embed(self, *, in_guild: bool, is_manager: bool) -> discord.Embed:
        embed = discord.Embed(
            title="What this bot does",
            description="Tap a command to run it.",
            color=EmbedColors.INFO,
        )
        for title, manager_only, entries in SECTIONS:
            if manager_only and not is_manager:
                continue
            lines = [
                f"{self.mention(name)} — {text}"
                for name, text in entries
                if self.find_command(name) is not None and (in_guild or name.split()[0] not in GUILD_ONLY_COMMANDS)
            ]
            if lines:
                embed.add_field(name=title, value="\n".join(lines)[:1024], inline=False)
        if self._bot.get_command("t") is not None:
            prefix = self._bot.command_prefix
            embed.add_field(
                name="🌐 Translation",
                value=(
                    "Right-click a message (long-press on mobile) → **Apps → Translate** to read it in your "
                    "Discord language; only you see the result.\n"
                    f"To translate for everyone, reply to a message with `{prefix}en` for English or "
                    f"`{prefix}t Spanish` for another language."
                ),
                inline=False,
            )
        embed.set_footer(text=KINGDOM_HINT)
        return embed

    # ---------------------------------------------------------------- command

    def register_commands(self) -> None:
        @self._bot.tree.command(name="help", description="What the bot does and the commands for it")
        async def help_command(interaction: discord.Interaction):
            await self._handle_help(interaction)

    async def _handle_help(self, interaction: discord.Interaction) -> None:
        in_guild = interaction.guild is not None
        is_manager = in_guild and await self._access.can_manage(interaction)
        await self._ensure_command_ids()
        await interaction.response.send_message(
            embed=self.build_embed(in_guild=in_guild, is_manager=is_manager), ephemeral=True
        )
