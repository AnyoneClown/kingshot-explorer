"""Discord UI for guild-wide bot configuration."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from handlers.permissions import AccessPolicy, manager_only_command, send_denied
from handlers.ui import EmbedColors, OwnedView, build_status_embed
from services.guild_configuration_service import GuildConfigurationService

logger = logging.getLogger(__name__)

_TEXT_CHANNEL_TYPES = [discord.ChannelType.text, discord.ChannelType.news]


def _mention_channel(channel_id: int | None) -> str:
    return f"<#{channel_id}>" if channel_id else "Not set"


def _mention_role(role_id: int | None) -> str:
    return f"<@&{role_id}>" if role_id else "Not set"


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
        timeout: float = 600.0,
    ):
        super().__init__(author_id, timeout=timeout)
        self._guild_id = guild_id
        self._guild_name = guild_name
        self._guild_config_service = guild_config_service
        self._access = access_policy or AccessPolicy()
        self._guild = guild
        self._warnings: list[str] = []

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """The panel can stay open for a long time: re-check manager rights on every click."""
        if not await super().interaction_check(interaction):
            return False
        return await self._access.require_manager(interaction, "configure the bot")

    async def _settings(self):
        return await self._guild_config_service.get_or_create_for_guild(self._guild_id)

    def _channel_warning(self, channel_id: int | None, label: str) -> str | None:
        """Warn when the bot can't post in a configured channel."""
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
        return None

    async def build_embed(self) -> discord.Embed:
        guild_config = await self._settings()
        self._sync_voice_button(guild_config.use_voice_replies)
        self._sync_random_replies_button(guild_config.use_random_replies)
        voice_status = "Enabled" if guild_config.use_voice_replies else "Disabled"
        ai_replies_status = "Enabled" if guild_config.use_random_replies else "Disabled"
        gift_channel = getattr(guild_config, "gift_code_channel_id", None)
        report_channel = getattr(guild_config, "alliance_report_channel_id", None)
        manager_role = getattr(guild_config, "manager_role_id", None)
        # Show what is saved inside the menus too, instead of empty placeholders.
        # Only values the menu can hold: a deleted channel or role, or a channel of
        # another type, as a default would make Discord reject the whole panel.
        self.gift_channel_select.default_values = self._defaults(
            self._selectable_channel(gift_channel), discord.SelectDefaultValueType.channel
        )
        self.report_channel_select.default_values = self._defaults(
            self._selectable_channel(report_channel), discord.SelectDefaultValueType.channel
        )
        self.manager_role_select.default_values = self._defaults(
            self._selectable_role(manager_role), discord.SelectDefaultValueType.role
        )

        lines = [
            f"Server: **{self._guild_name}**",
            f"Voice replies: **{voice_status}**",
            f"AI replies: **{ai_replies_status}**",
            f"Gift-code announcements: {_mention_channel(gift_channel)}",
            f"Alliance reports: {_mention_channel(report_channel)}",
            f"Bot manager role: {_mention_role(manager_role)}",
            "",
            self._setup_progress(gift_channel, report_channel),
            "Use the menus below to pick channels and the manager role. "
            "Clear a menu's selection to unset it.",
        ]
        warnings = [
            warning
            for warning in (
                self._channel_warning(gift_channel, "Gift-code announcements"),
                self._channel_warning(report_channel, "Alliance reports"),
            )
            if warning
        ] + self._warnings
        if warnings:
            lines.extend(["", "⚠️ " + "\n⚠️ ".join(warnings)])
        self._warnings = []
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

    def _setup_progress(self, gift_channel: int | None, report_channel: int | None) -> str:
        done = sum(bool(value) for value in (gift_channel, report_channel))
        if done == 2:
            return "✅ Channels are set up."
        missing = []
        if not gift_channel:
            missing.append("gift-code announcements")
        if not report_channel:
            missing.append("alliance reports")
        return f"Setup {done}/2 — no channel yet for: {', '.join(missing)}. Until then they are not posted."

    def _sync_voice_button(self, enabled: bool) -> None:
        self.toggle_voice_button.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        self.toggle_voice_button.label = f"Voice Replies: {'On' if enabled else 'Off'}"

    def _sync_random_replies_button(self, enabled: bool) -> None:
        self.toggle_random_replies_button.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        self.toggle_random_replies_button.label = f"AI Replies: {'On' if enabled else 'Off'}"

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.edit_original_response(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Voice Replies", style=discord.ButtonStyle.secondary, row=0)
    async def toggle_voice_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.defer()
        guild_config = await self._settings()
        updated = await self._guild_config_service.set_use_voice_replies_for_guild(
            self._guild_id,
            not guild_config.use_voice_replies,
        )
        self._sync_voice_button(updated.use_voice_replies)
        await self._refresh(interaction)

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
        await self._refresh(interaction)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, row=0)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.defer()
        await self._refresh(interaction)

    async def _update(self, interaction: discord.Interaction, **changes) -> None:
        await interaction.response.defer()
        await self._guild_config_service.update_for_guild(self._guild_id, **changes)
        await self._refresh(interaction)

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
        cls=discord.ui.ChannelSelect,
        channel_types=_TEXT_CHANNEL_TYPES,
        placeholder="Alliance report channel (daily roster changes)",
        min_values=0,
        max_values=1,
        row=2,
    )
    async def report_channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        channel_id = select.values[0].id if select.values else None
        await self._update(interaction, alliance_report_channel_id=channel_id)

    @discord.ui.select(
        cls=discord.ui.RoleSelect,
        placeholder="Bot manager role",
        min_values=0,
        max_values=1,
        row=3,
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
            await self._refresh(interaction)
            return
        await self._update(interaction, manager_role_id=role.id if role is not None else None)


class GuildConfigHandler:
    """Manager-only guild configuration command handler."""

    def __init__(
        self,
        bot: commands.Bot,
        guild_config_service: GuildConfigurationService,
        admin_user_ids: set[int] | None = None,
        access_policy: AccessPolicy | None = None,
    ):
        self._bot = bot
        self._guild_config_service = guild_config_service
        self._admin_user_ids = admin_user_ids or set()
        self._access = access_policy or AccessPolicy(
            self._admin_user_ids,
            guild_configuration_service=guild_config_service,
        )
        logger.info("GuildConfigHandler initialized")

    def register_commands(self):
        @self._bot.tree.command(name="configure", description="Open the server's bot settings: channels, manager role, replies")
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
        )
        view.message = await interaction.edit_original_response(
            embed=await view.build_embed(),
            view=view,
        )
