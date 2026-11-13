"""The gift-code panel: buttons members can use without knowing any command.

A manager posts it permanently in a channel (``/giftadmin panel``), and ``/gift`` opens
the same panel privately for whoever runs it.

The view is persistent (no timeout, fixed ``custom_id`` values), so the buttons of a
panel posted weeks ago keep working after restarts once ``bot.add_view`` has
registered it.
"""

from __future__ import annotations

import discord

from handlers.ui import EmbedColors, send_ui_error

PANEL_ADD_ID = "giftpanel:add"
PANEL_MINE_ID = "giftpanel:mine"
PANEL_CODES_ID = "giftpanel:codes"
PANEL_SUBMIT_ID = "giftpanel:submit"


def build_panel_embed(max_self_registered: int) -> discord.Embed:
    lines = [
        "New gift codes are redeemed automatically for every account on the list. "
        "Rewards arrive in your in-game mail.",
        "",
        "**Add my ID** — put your Governor ID on the list"
        + (f" (up to {max_self_registered} accounts)." if max_self_registered > 0 else ".")
        + " The codes active right now are redeemed for it too.",
        "**My accounts** — see whether the latest codes reached your accounts; pause, resume or remove them.",
        "**Active codes** — codes that work right now, ready to copy.",
        "**Submit a code** — found a code the bot has not posted yet? Send it here; "
        "if it works, everyone on the list gets it.",
    ]
    embed = discord.Embed(title="🎁 Gift codes", description="\n".join(lines), color=EmbedColors.SUCCESS)
    embed.set_footer(text="Your Governor ID is in the game: tap your avatar, the ID is under your name.")
    return embed


class AddPlayerModal(discord.ui.Modal, title="Add my Governor ID"):
    player_ids = discord.ui.TextInput(
        label="Governor ID",
        placeholder="For example 123456789. Several IDs: separate with commas.",
        min_length=1,
        max_length=600,
    )

    def __init__(self, handler, after=None):
        super().__init__(timeout=300)
        self._handler = handler
        # Awaited after the IDs were handled, so the panel that opened the form can redraw.
        self._after = after

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._handler._handle_add_player_slash(interaction, str(self.player_ids.value))
        if self._after is not None:
            await self._after()

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await send_ui_error(interaction, error)


class SubmitCodeModal(discord.ui.Modal, title="Submit a gift code"):
    code = discord.ui.TextInput(
        label="Gift code",
        placeholder="Exactly as published, for example KINGSHOT2026",
        min_length=1,
        max_length=64,
    )

    def __init__(self, handler):
        super().__init__(timeout=300)
        self._handler = handler

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self._handler._handle_enter_code(interaction, str(self.code.value))

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await send_ui_error(interaction, error)


class GiftPanelView(discord.ui.View):
    """Buttons under the panel message; everyone may press them."""

    def __init__(self, handler):
        super().__init__(timeout=None)
        self._handler = handler

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return not await self._handler._reject_banned(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await send_ui_error(interaction, error)

    @discord.ui.button(label="Add my ID", style=discord.ButtonStyle.success, custom_id=PANEL_ADD_ID)
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddPlayerModal(self._handler))

    @discord.ui.button(label="My accounts", style=discord.ButtonStyle.primary, custom_id=PANEL_MINE_ID)
    async def mine_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handler._handle_my_accounts(interaction)

    @discord.ui.button(label="Active codes", style=discord.ButtonStyle.secondary, custom_id=PANEL_CODES_ID)
    async def codes_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handler._handle_active_codes(interaction)

    @discord.ui.button(label="Submit a code", style=discord.ButtonStyle.secondary, custom_id=PANEL_SUBMIT_ID)
    async def submit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SubmitCodeModal(self._handler))
