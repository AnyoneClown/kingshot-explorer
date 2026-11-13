"""Commands whose ``kingdom`` option falls back to the server's kingdom.

``/scout`` and ``/kvk`` post a public answer. With a kingdom in the command they
acknowledge publicly at once. Without one they first have to look the server's kingdom
up, and the answer to "this server has no kingdom" is for the caller only, so they
acknowledge privately, post the public answer as a follow-up and remove the private
receipt.
"""

from __future__ import annotations

import logging
from typing import Optional

import discord

from handlers.permissions import interaction_guild_id
from handlers.ui import EmbedColors, build_status_embed

logger = logging.getLogger(__name__)


async def server_kingdom(guild_configuration_service, guild_id: Optional[int]) -> Optional[int]:
    """The kingdom chosen in the server's ``/settings``, or None."""
    if guild_id is None or guild_configuration_service is None:
        return None
    try:
        settings = await guild_configuration_service.get_settings(guild_id)
    except Exception:
        logger.warning("Could not load settings for guild %s", guild_id, exc_info=True)
        return None
    return getattr(settings, "kingdom_id", None)


class PublicReply:
    """Sends a command's answer after a defer that was public or private."""

    def __init__(self, interaction: discord.Interaction, *, private: bool, receipt: str = "Ready."):
        self._interaction = interaction
        self.private = private
        self._receipt = receipt

    async def send(self, **kwargs):
        """Post the public answer and return its message."""
        interaction = self._interaction
        if not self.private:
            return await interaction.followup.send(**kwargs)
        # Complete the private defer first: the first follow-up would otherwise take
        # its place and stay private.
        await interaction.edit_original_response(content=self._receipt)
        message = await interaction.followup.send(**kwargs)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            logger.debug("Could not remove the private receipt", exc_info=True)
        return message

    async def fail(self, embed: discord.Embed) -> None:
        """Report a problem: in place of the private receipt, or as the public answer."""
        if self.private:
            # content=None also clears a receipt text that was already shown.
            await self._interaction.edit_original_response(content=None, embed=embed)
        else:
            await self._interaction.followup.send(embed=embed)


async def defer_for_kingdom(
    interaction: discord.Interaction,
    kingdom: Optional[int],
    guild_configuration_service,
    *,
    command: str,
    receipt: str,
) -> tuple[Optional[int], Optional[PublicReply]]:
    """Acknowledge the command and settle its kingdom.

    Returns ``(kingdom, reply)``. Without a kingdom in the command or in the server's
    settings, the caller is told privately how to give one and ``(None, None)`` is
    returned.
    """
    if kingdom is not None:
        await interaction.response.defer(thinking=True)
        return kingdom, PublicReply(interaction, private=False)

    guild_id = interaction_guild_id(interaction)
    # Acknowledge first: the settings lookup below may wait on the database.
    await interaction.response.defer(thinking=True, ephemeral=True)
    kingdom = await server_kingdom(guild_configuration_service, guild_id)
    if kingdom is None:
        hint = (
            " A server manager can set this server's kingdom in `/settings`, "
            f"then `/{command}` works without a number."
            if guild_id is not None
            else ""
        )
        await interaction.edit_original_response(
            embed=build_status_embed(
                title="Which kingdom?",
                description=f"Add the kingdom number: `/{command} kingdom:830`.{hint}",
                color=EmbedColors.INFO,
            ),
        )
        return None, None
    return int(kingdom), PublicReply(interaction, private=True, receipt=receipt)
