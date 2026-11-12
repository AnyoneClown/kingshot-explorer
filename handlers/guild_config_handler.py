"""Discord UI for guild-wide bot configuration."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from handlers.calendar_handler import DAILY_POST_TIME
from handlers.permissions import AccessPolicy, manager_only_command, send_denied
from handlers.ui import EmbedColors, OwnedView, build_status_embed, send_ui_error
from services.guild_configuration_service import GuildConfigurationService
from services.kingshot_calendar_service import MAX_KINGDOM_NUMBER, CalendarError, KingdomNotTrackedError

logger = logging.getLogger(__name__)

_TEXT_CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news]


def _mention_channel(channel_id: int | None) -> str:
    return f"<#{channel_id}>" if channel_id else "Not set"


def _mention_role(role_id: int | None) -> str:
    return f"<@&{role_id}>" if role_id else "Not set"


class ServerKingdomModal(discord.ui.Modal, title="Server kingdom"):
    """Asks which kingdom this server plays in (one per server)."""

    kingdom = discord.ui.TextInput(
        label="Kingdom number",
        placeholder="The kingdom this server plays in, for example 795",
        min_length=1,
        max_length=5,
    )

    def __init__(self, view: "GuildConfigView", *, current: int | None = None, pending_channel_id: int | None = None):
        super().__init__(timeout=300)
        self._view = view
        self._pending_channel_id = pending_channel_id
        if current:
            self.kingdom.default = str(current)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._view.apply_kingdom(interaction, str(self.kingdom.value), self._pending_channel_id)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await send_ui_error(interaction, error)


class GuildConfigView(OwnedView):
    """Ephemeral manager control panel for guild configuration."""

    def __init__(
        self,
        *,
        guild_id: int,
        author_id: int,
        guild_name: str,
        guild_config_service,
        access_policy: AccessPolicy | None = None,
        guild: discord.Guild | None = None,
        calendar=None,
        timeout: float = 600.0,
    ):
        super().__init__(author_id, timeout=timeout)
        self._guild_id = guild_id
        self._guild_name = guild_name
        self._guild_config_service = guild_config_service
        self._access = access_policy or AccessPolicy()
        self._guild = guild
        # The calendar handler: checks a kingdom and posts the first daily calendar.
        self._calendar = calendar
        # As last shown; lets the kingdom controls open a modal without a database read.
        self._kingdom: int | None = None
        self._warnings: list[str] = []
        self._notes: list[str] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """The panel can stay open for a long time: re-check manager rights on every click."""
        if not await super().interaction_check(interaction):
            return False
        return await self._access.require_manager(interaction, "configure the bot")

    async def _settings(self):
        return await self._guild_config_service.get_or_create_for_guild(self._guild_id)

    def _channel_warning(self, channel_id: int | None, label: str, *, pictures: bool = False) -> str | None:
        """Warn when the bot can't post in a configured channel (or can't attach its pictures)."""
        if not channel_id or self._guild is None:
            return None
        channel = self._guild.get_channel(channel_id)
        me = getattr(self._guild, "me", None)
        if channel is None:
            return f"{label}: the channel no longer exists."
        if me is not None and hasattr(channel, "permissions_for"):
            permissions = channel.permissions_for(me)
            if not (permissions.view_channel and permissions.send_messages and permissions.embed_links):
                return f"{label}: I need View Channel, Send Messages and Embed Links in {channel.mention}."
            if pictures and not permissions.attach_files:
                return f"{label}: without Attach Files in {channel.mention} it is posted as text, not pictures."
        return None

    async def build_embed(self) -> discord.Embed:
        guild_config = await self._settings()
        self._sync_random_replies_button(guild_config.use_random_replies)
        ai_replies_status = "Enabled" if guild_config.use_random_replies else "Disabled"
        gift_channel = getattr(guild_config, "gift_code_channel_id", None)
        manager_role = getattr(guild_config, "manager_role_id", None)
        calendar_channel = getattr(guild_config, "calendar_channel_id", None)
        kingdom = getattr(guild_config, "kingdom_id", None)
        self._kingdom = kingdom
        self._sync_kingdom_button(kingdom)
        # Show what is saved inside the menus too, instead of empty placeholders.
        # Only values the menu can hold: a deleted channel or role, or a channel of
        # another type, as a default would make Discord reject the whole panel.
        self.gift_channel_select.default_values = self._defaults(
            self._selectable_channel(gift_channel), discord.SelectDefaultValueType.channel
        )
        self.manager_role_select.default_values = self._defaults(
            self._selectable_role(manager_role), discord.SelectDefaultValueType.role
        )
        self.calendar_channel_select.default_values = self._defaults(
            self._selectable_channel(calendar_channel), discord.SelectDefaultValueType.channel
        )

        lines = [
            f"Server: **{self._guild_name}**",
            f"Kingdom: {f'**{kingdom}**' if kingdom else 'Not set'}",
            f"AI replies: **{ai_replies_status}**",
            f"Gift-code announcements: {_mention_channel(gift_channel)}",
            f"Event calendar: {self._calendar_summary(calendar_channel, kingdom)}",
            f"Bot manager role: {_mention_role(manager_role)}",
            "",
            self._setup_progress(gift_channel, calendar_channel, kingdom),
            "Use the menus below to pick channels and the manager role. "
            "Clear a menu's selection to unset it.",
        ]
        warnings = [
            warning
            for warning in (
                self._channel_warning(gift_channel, "Gift-code announcements"),
                self._channel_warning(calendar_channel, "Event calendar", pictures=True),
            )
            if warning
        ] + self._warnings
        if self._notes:
            lines.extend(["", "✅ " + "\n✅ ".join(self._notes)])
        if warnings:
            lines.extend(["", "⚠️ " + "\n⚠️ ".join(warnings)])
        self._warnings = []
        self._notes = []
        return build_status_embed(
            title="Bot Configuration",
            description="\n".join(lines),
            color=EmbedColors.NEUTRAL,
            footer=(
                "Managers: Manage Server, the manager role, or bot operators · "
                "Members with the manager role may also need the commands enabled in "
                "Server Settings → Integrations"
            ),
        )

    @staticmethod
    def _calendar_summary(channel_id: int | None, kingdom: int | None) -> str:
        if channel_id and kingdom:
            return f"<#{channel_id}> · every day at {DAILY_POST_TIME:%H:%M} UTC"
        if channel_id:
            return f"<#{channel_id}> · not posted yet: press **Set kingdom**"
        return "Not set"

    def _selectable_channel(self, channel_id: int | None) -> int | None:
        if not channel_id:
            return None
        if self._guild is None:
            return channel_id
        channel = self._guild.get_channel(channel_id)
        return channel_id if getattr(channel, "type", None) in _TEXT_CHANNEL_TYPES else None

    def _selectable_role(self, role_id: int | None) -> int | None:
        if not role_id:
            return None
        if self._guild is None:
            return role_id
        return role_id if self._guild.get_role(role_id) is not None else None

    @staticmethod
    def _defaults(object_id: int | None, kind: discord.SelectDefaultValueType) -> list[discord.SelectDefaultValue]:
        return [discord.SelectDefaultValue(id=int(object_id), type=kind)] if object_id else []

    @staticmethod
    def _setup_progress(gift_channel: int | None, calendar_channel: int | None, kingdom: int | None) -> str:
        missing = []
        if not gift_channel:
            missing.append("a channel for gift-code announcements")
        if not kingdom:
            missing.append("the server's kingdom")
        if not calendar_channel:
            missing.append("a channel for the daily event calendar")
        if not missing:
            return "✅ Everything is set up."
        return f"Setup {3 - len(missing)}/3 — still to pick: {', '.join(missing)}."

    def _sync_random_replies_button(self, enabled: bool) -> None:
        self.toggle_random_replies_button.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        self.toggle_random_replies_button.label = f"AI Replies: {'On' if enabled else 'Off'}"

    def _sync_kingdom_button(self, kingdom: int | None) -> None:
        self.kingdom_button.label = f"Kingdom: {kingdom}" if kingdom else "Set kingdom"

    # Not named ``_refresh``: that is discord.py's own View method, called with the
    # message's components when a message carrying the view is edited.
    async def _show(self, interaction: discord.Interaction) -> None:
        await interaction.edit_original_response(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="AI Replies", style=discord.ButtonStyle.secondary, row=0)
    async def toggle_random_replies_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.defer()
        guild_config = await self._settings()
        updated = await self._guild_config_service.set_use_random_replies_for_guild(
            self._guild_id,
            not guild_config.use_random_replies,
        )
        self._sync_random_replies_button(updated.use_random_replies)
        await self._show(interaction)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, row=0)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.defer()
        await self._show(interaction)

    @discord.ui.button(label="Set kingdom", emoji="🏰", style=discord.ButtonStyle.secondary, row=0)
    async def kingdom_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.send_modal(ServerKingdomModal(self, current=self._kingdom))

    async def apply_kingdom(
        self, interaction: discord.Interaction, raw_kingdom: str, pending_channel_id: int | None = None
    ) -> None:
        """Save the server's kingdom from the modal (and the calendar channel that asked for it)."""
        if interaction.user.id != self.author_id or not await self._access.require_manager(
            interaction, "configure the bot"
        ):
            return
        value = raw_kingdom.strip().lstrip("#")
        if not value.isdecimal() or not 1 <= int(value) <= MAX_KINGDOM_NUMBER:
            await interaction.response.send_message(
                "Enter the kingdom number, for example `795`.", ephemeral=True
            )
            return
        kingdom = int(value)
        await interaction.response.defer()
        if self._calendar is not None:
            try:
                await self._calendar.verify_kingdom(kingdom)
            except KingdomNotTrackedError:
                await interaction.followup.send(
                    f"Kingdom **{kingdom}** is not on the kingshot.net calendar. Check the number; "
                    "very new kingdoms appear there a little later.",
                    ephemeral=True,
                )
                await self._show(interaction)
                return
            except CalendarError:
                self._warnings.append(
                    f"kingshot.net is not answering, so kingdom {kingdom} could not be checked. It is saved anyway."
                )
        changes = {"kingdom_id": kingdom, "calendar_last_posted_on": None}
        if pending_channel_id:
            changes["calendar_channel_id"] = pending_channel_id
        await self._guild_config_service.update_for_guild(self._guild_id, **changes)
        await self._post_calendar_now()
        await self._show(interaction)

    async def _post_calendar_now(self) -> None:
        """Post today's calendar once the daily post is fully set up, as a visible check."""
        if self._calendar is None:
            return
        guild_config = await self._settings()
        if not getattr(guild_config, "calendar_channel_id", None) or not getattr(
            guild_config, "kingdom_id", None
        ):
            return
        try:
            posted, message = await self._calendar.post_now(self._guild_id)
        except Exception:
            logger.warning("Could not post the calendar for guild %s", self._guild_id, exc_info=True)
            posted, message = False, "Could not post the calendar right now. I will retry shortly."
        (self._notes if posted else self._warnings).append(message)

    async def _update(self, interaction: discord.Interaction, **changes) -> None:
        await interaction.response.defer()
        await self._guild_config_service.update_for_guild(self._guild_id, **changes)
        await self._show(interaction)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=_TEXT_CHANNEL_TYPES,
        placeholder="Gift-code announcement channel",
        min_values=0,
        max_values=1,
        row=1,
    )
    async def gift_channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel_id = select.values[0].id if select.values else None
        await self._update(interaction, gift_code_channel_id=channel_id)

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Bot manager role",
        min_values=0,
        max_values=1,
        row=2,
    )
    async def manager_role_select(self, interaction: discord.Interaction, select: discord.ui.RoleSelect):
        # Holders of the manager role must not be able to hand it to another role:
        # only Manage Server / Administrator members and bot operators may change it.
        permissions = getattr(interaction.user, "guild_permissions", None)
        can_change = self._access.is_super_admin(interaction.user.id) or (
            getattr(permissions, "manage_guild", False) is True or getattr(permissions, "administrator", False) is True
        )
        if not can_change:
            await send_denied(interaction, "Only members with **Manage Server** can change the bot manager role.")
            return
        role = select.values[0] if select.values else None
        if role is not None and (getattr(role, "is_default", lambda: False)() or getattr(role, "managed", False)):
            self._warnings.append("Pick a regular role (not @everyone or a bot/integration role).")
            await interaction.response.defer()
            await self._show(interaction)
            return
        await self._update(interaction, manager_role_id=role.id if role is not None else None)

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=_TEXT_CHANNEL_TYPES,
        placeholder=f"Event calendar channel (daily post at {DAILY_POST_TIME:%H:%M} UTC)",
        min_values=0,
        max_values=1,
        row=3,
    )
    async def calendar_channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel_id = select.values[0].id if select.values else None
        if channel_id is None:
            await self._update(interaction, calendar_channel_id=None)
            return
        if not self._kingdom:
            # The post needs a kingdom: ask for it now and save both together.
            await interaction.response.send_modal(ServerKingdomModal(self, pending_channel_id=channel_id))
            return
        await interaction.response.defer()
        await self._guild_config_service.update_for_guild(
            self._guild_id, calendar_channel_id=channel_id, calendar_last_posted_on=None
        )
        await self._post_calendar_now()
        await self._show(interaction)


class GuildConfigHandler:
    """Manager-only guild configuration command handler."""

    def __init__(
        self,
        bot: commands.Bot,
        guild_config_service: GuildConfigurationService,
        admin_user_ids: set[int] | None = None,
        access_policy: AccessPolicy | None = None,
        calendar_handler=None,
    ):
        self._bot = bot
        self._guild_config_service = guild_config_service
        self._calendar_handler = calendar_handler
        self._admin_user_ids = admin_user_ids or set()
        self._access = access_policy or AccessPolicy(
            self._admin_user_ids,
            guild_configuration_service=guild_config_service,
        )
        logger.info("GuildConfigHandler initialized")

    def register_commands(self):
        @self._bot.tree.command(name="settings", description="Open the server's bot settings: kingdom, channels, manager role, AI replies")
        @manager_only_command
        async def configure(interaction: discord.Interaction):
            await self._handle_configure(interaction)

    async def _handle_configure(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not await self._access.require_manager(interaction, "configure the bot"):
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        view = GuildConfigView(
            guild_id=interaction.guild.id,
            author_id=interaction.user.id,
            guild_name=interaction.guild.name,
            guild_config_service=self._guild_config_service,
            access_policy=self._access,
            guild=interaction.guild if isinstance(interaction.guild, discord.Guild) else None,
            calendar=self._calendar_handler,
        )
        view.message = await interaction.edit_original_response(
            embed=await view.build_embed(),
            view=view,
        )
