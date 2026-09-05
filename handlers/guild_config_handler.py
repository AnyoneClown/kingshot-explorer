"""Discord UI for guild-wide bot configuration."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from handlers.ui import EmbedColors, OwnedView, build_status_embed
from services.guild_configuration_service import GuildConfigurationService

logger = logging.getLogger(__name__)


class GuildConfigView(OwnedView):
    """Ephemeral admin control panel for guild configuration."""

    def __init__(
        self,
        *,
        guild_id: int,
        author_id: int,
        guild_name: str,
        guild_config_service,
        timeout: float = 180.0,
    ):
        super().__init__(author_id, timeout=timeout)
        self._guild_id = guild_id
        self._guild_name = guild_name
        self._guild_config_service = guild_config_service

    async def build_embed(self) -> discord.Embed:
        guild_config = await self._guild_config_service.get_or_create_for_guild(self._guild_id)
        self._sync_voice_button(guild_config.use_voice_replies)
        self._sync_random_replies_button(guild_config.use_random_replies)
        voice_status = "Enabled" if guild_config.use_voice_replies else "Disabled"
        ai_replies_status = "Enabled" if guild_config.use_random_replies else "Disabled"
        return build_status_embed(
            title="Bot Configuration",
            description=(
                f"Server: **{self._guild_name}**\n"
                f"Voice replies: **{voice_status}**\n"
                f"AI replies: **{ai_replies_status}**\n"
                "Use the buttons below to update guild-wide bot behavior."
            ),
            color=EmbedColors.NEUTRAL,
            footer="Admin-only configuration panel · Reopen /configure when controls expire",
        )

    def _sync_voice_button(self, enabled: bool) -> None:
        self.toggle_voice_button.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        self.toggle_voice_button.label = f"Voice Replies: {'On' if enabled else 'Off'}"

    def _sync_random_replies_button(self, enabled: bool) -> None:
        self.toggle_random_replies_button.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
        self.toggle_random_replies_button.label = f"AI Replies: {'On' if enabled else 'Off'}"

    @discord.ui.button(label="Voice Replies", style=discord.ButtonStyle.secondary, row=0)
    async def toggle_voice_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        guild_config = await self._guild_config_service.get_or_create_for_guild(self._guild_id)
        updated = await self._guild_config_service.set_use_voice_replies_for_guild(
            self._guild_id,
            not guild_config.use_voice_replies,
        )
        self._sync_voice_button(updated.use_voice_replies)
        await interaction.edit_original_response(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="AI Replies", style=discord.ButtonStyle.secondary, row=0)
    async def toggle_random_replies_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.defer()
        guild_config = await self._guild_config_service.get_or_create_for_guild(self._guild_id)
        updated = await self._guild_config_service.set_use_random_replies_for_guild(
            self._guild_id,
            not guild_config.use_random_replies,
        )
        self._sync_random_replies_button(updated.use_random_replies)
        await interaction.edit_original_response(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, row=0)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.defer()
        await interaction.edit_original_response(embed=await self.build_embed(), view=self)


class GuildConfigHandler:
    """Admin-only guild configuration command handler."""

    def __init__(
        self,
        bot: commands.Bot,
        guild_config_service: GuildConfigurationService,
        admin_user_ids: set[int] | None = None,
    ):
        self._bot = bot
        self._guild_config_service = guild_config_service
        self._admin_user_ids = admin_user_ids or set()
        logger.info("GuildConfigHandler initialized")

    def register_commands(self):
        @self._bot.tree.command(name="configure", description="Open the guild-wide bot configuration panel")
        async def configure(interaction: discord.Interaction):
            await self._handle_configure(interaction)

    async def _handle_configure(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not self._is_bot_admin(interaction):
            await interaction.response.send_message(
                "Only configured bot admins can configure the bot.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        view = GuildConfigView(
            guild_id=interaction.guild.id,
            author_id=interaction.user.id,
            guild_name=interaction.guild.name,
            guild_config_service=self._guild_config_service,
        )
        view.message = await interaction.edit_original_response(
            embed=await view.build_embed(),
            view=view,
        )

    def _is_bot_admin(self, interaction: discord.Interaction) -> bool:
        return int(interaction.user.id) in self._admin_user_ids
