"""Discord UI for guild-wide bot configuration."""

from __future__ import annotations

import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from handlers.ui import EmbedColors, build_status_embed
from services.guild_configuration_service import GuildConfigurationService

logger = logging.getLogger(__name__)


class GuildConfigView(discord.ui.View):
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
        super().__init__(timeout=timeout)
        self._guild_id = guild_id
        self._author_id = author_id
        self._guild_name = guild_name
        self._guild_config_service = guild_config_service

    async def build_embed(self) -> discord.Embed:
        guild_config = await self._guild_config_service.get_or_create_for_guild(self._guild_id)
        self._sync_voice_button(guild_config.use_voice_replies)
        voice_status = "Enabled" if guild_config.use_voice_replies else "Disabled"
        return build_status_embed(
            title="Bot Configuration",
            description=(
                f"Server: **{self._guild_name}**\n"
                f"Voice replies: **{voice_status}**\n"
                "Use the buttons below to update guild-wide bot behavior."
            ),
            color=EmbedColors.NEUTRAL,
            footer="Admin-only configuration panel",
        )

    def _sync_voice_button(self, enabled: bool) -> None:
        self.toggle_voice_button.style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.danger
        self.toggle_voice_button.label = f"Voice Replies: {'On' if enabled else 'Off'}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self._author_id:
            await interaction.response.send_message("Only the command user can control this panel.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Voice Replies", style=discord.ButtonStyle.secondary, row=0)
    async def toggle_voice_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild_config = await self._guild_config_service.get_or_create_for_guild(self._guild_id)
        updated = await self._guild_config_service.set_use_voice_replies_for_guild(
            self._guild_id,
            not guild_config.use_voice_replies,
        )
        self._sync_voice_button(updated.use_voice_replies)
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.primary, row=0)
    async def refresh_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        del button
        await interaction.response.edit_message(embed=await self.build_embed(), view=self)


class GuildConfigHandler:
    """Admin-only guild configuration command handler."""

    def __init__(
        self,
        bot: commands.Bot,
        guild_config_service: GuildConfigurationService,
    ):
        self._bot = bot
        self._guild_config_service = guild_config_service
        logger.info("GuildConfigHandler initialized")

    def register_commands(self):
        @self._bot.tree.command(name="configure", description="Open the guild-wide bot configuration panel")
        async def configure(interaction: discord.Interaction):
            await self._handle_configure(interaction)

    async def _handle_configure(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only server administrators can configure the bot.",
                ephemeral=True,
            )
            return

        view = GuildConfigView(
            guild_id=interaction.guild.id,
            author_id=interaction.user.id,
            guild_name=interaction.guild.name,
            guild_config_service=self._guild_config_service,
        )
        await interaction.response.send_message(
            embed=await view.build_embed(),
            view=view,
            ephemeral=True,
        )
