"""Ephemeral confirmation prompt for destructive panel actions."""

from __future__ import annotations

from typing import Awaitable, Callable

import discord

from handlers.ui import OwnedView, build_status_embed

ConfirmCallback = Callable[[discord.Interaction], Awaitable[None]]


class ConfirmView(OwnedView):
    """Two buttons owned by one user: run ``on_confirm`` or leave things unchanged.

    ``on_confirm`` receives the button interaction and must respond to it (for
    example ``interaction.response.defer()`` and ``edit_original_response``).
    """

    def __init__(
        self,
        author_id: int,
        *,
        on_confirm: ConfirmCallback,
        confirm_label: str,
        keep_label: str = "Keep",
        kept_title: str = "Nothing changed",
        kept_description: str = "No changes were made.",
        timeout: float = 120.0,
    ):
        super().__init__(author_id, timeout=timeout)
        self._on_confirm = on_confirm
        self._kept_title = kept_title
        self._kept_description = kept_description
        self.confirm_button.label = confirm_label
        self.keep_button.label = keep_label
        self.handled = False

    async def _already_handled(self, interaction: discord.Interaction) -> bool:
        if self.handled:
            await interaction.response.send_message("This confirmation was already used.", ephemeral=True)
            return True
        return False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._already_handled(interaction):
            return
        self.handled = True
        try:
            await self._on_confirm(interaction)
        except Exception:
            self.handled = False
            raise
        self.stop()

    @discord.ui.button(label="Keep", style=discord.ButtonStyle.secondary)
    async def keep_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self._already_handled(interaction):
            return
        self.handled = True
        self.stop()
        await interaction.response.edit_message(
            embed=build_status_embed(title=self._kept_title, description=self._kept_description),
            view=None,
        )
