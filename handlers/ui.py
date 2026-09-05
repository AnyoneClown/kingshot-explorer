"""Shared Discord UI helpers."""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)


class OwnedView(discord.ui.View):
    """Controls belong to their requester and visibly expire."""

    def __init__(self, author_id: int, timeout: float = 180.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the command user can control this panel.", ephemeral=True
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                logger.debug("Could not disable expired controls", exc_info=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await send_ui_error(interaction, error)


async def send_ui_error(interaction: discord.Interaction, error: Exception) -> None:
    logger.error("Panel action failed", exc_info=(type(error), error, error.__traceback__))
    send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
    await send("That action could not be completed. Try again or reopen the command.", ephemeral=True)


class EmbedColors:
    """Consistent embed colors across commands."""

    SUCCESS = discord.Color.green()
    WARNING = discord.Color.orange()
    ERROR = discord.Color.red()
    INFO = discord.Color.blue()
    NEUTRAL = discord.Color.blurple()


def build_status_embed(
    *,
    title: str,
    description: str,
    color: discord.Color = EmbedColors.INFO,
    footer: str | None = None,
) -> discord.Embed:
    """Build a compact status embed with optional footer text."""
    embed = discord.Embed(title=title, description=description, color=color)
    if footer:
        embed.set_footer(text=footer)
    return embed


def status_value(ok: bool, success_text: str = "Online", failure_text: str = "Unavailable") -> str:
    """Format a boolean health state for embeds."""
    return f"OK - {success_text}" if ok else f"ISSUE - {failure_text}"
