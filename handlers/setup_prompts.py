"""One-click channel setup offered right where a manager needs it."""

from __future__ import annotations

import logging

import discord

from handlers.permissions import AccessPolicy, interaction_guild_id, send_denied
from handlers.ui import EmbedColors, OwnedView, build_status_embed

logger = logging.getLogger(__name__)

# setting name -> (what is posted there, button label)
CHANNEL_SETTINGS = {
    "gift_code_channel_id": ("gift-code announcements", "Announce gift codes here"),
    "calendar_channel_id": ("daily event calendar posts", "Post the daily calendar here"),
}


def bot_send_permission(channel, permissions) -> bool:
    """The "may send here" bit: threads use Send Messages in Threads of their parent."""
    if isinstance(channel, discord.Thread):
        return bool(permissions.send_messages_in_threads)
    return bool(permissions.send_messages)


def bot_can_post(channel) -> bool:
    """Whether the bot can send embeds in ``channel`` (True when it cannot be checked)."""
    guild = getattr(channel, "guild", None)
    me = getattr(guild, "me", None)
    if me is None or not hasattr(channel, "permissions_for"):
        return True
    try:
        permissions = channel.permissions_for(me)
    except Exception:
        # For example a thread whose parent channel is not cached.
        return True
    return bool(permissions.view_channel and bot_send_permission(channel, permissions) and permissions.embed_links)


def bot_can_attach(channel) -> bool:
    """Whether the bot may upload files in ``channel`` (True when it cannot be checked)."""
    guild = getattr(channel, "guild", None)
    me = getattr(guild, "me", None)
    if me is None or not hasattr(channel, "permissions_for"):
        return True
    try:
        return bool(channel.permissions_for(me).attach_files)
    except Exception:
        return True


def is_announcement_target(channel) -> bool:
    """Whether a channel may be saved as a gift-code or calendar channel.

    Threads archive (announcements would then be dropped silently) and voice
    channels are not what ``/settings`` offers, so only text and news channels count.
    Objects that are not discord channels (test doubles) are accepted.
    """
    if isinstance(channel, discord.abc.GuildChannel) or isinstance(channel, discord.Thread):
        return isinstance(channel, discord.TextChannel)
    return True


class ChannelSetupView(OwnedView):
    """Sets one channel setting of a server to the channel the manager is in."""

    def __init__(
        self,
        *,
        author_id: int,
        guild_id: int,
        channel_id: int,
        setting: str,
        guild_config_service,
        access_policy: AccessPolicy,
        on_saved=None,
    ):
        super().__init__(author_id, timeout=600.0)
        self._on_saved = on_saved
        self._guild_id = guild_id
        self._channel_id = channel_id
        self._setting = setting
        self._guild_config_service = guild_config_service
        self._access = access_policy
        self.use_button.label = CHANNEL_SETTINGS[setting][1]

    @discord.ui.button(label="Use this channel", style=discord.ButtonStyle.success)
    async def use_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self._access.can_manage(interaction):
            await send_denied(interaction, "You need **Manage Server** or the bot manager role to change settings.")
            return
        await interaction.response.defer()
        await self._guild_config_service.update_for_guild(self._guild_id, **{self._setting: self._channel_id})
        what = CHANNEL_SETTINGS[self._setting][0]
        for item in self.children:
            item.disabled = True
        self.stop()
        await interaction.edit_original_response(
            embed=build_status_embed(
                title="✅ Channel saved",
                description=(
                    f"{what.capitalize()} will be posted in <#{self._channel_id}>. "
                    "Change it any time in `/settings`."
                ),
                color=EmbedColors.SUCCESS,
            ),
            view=self,
        )
        if self._on_saved is not None:
            try:
                await self._on_saved(self._channel_id)
            except Exception:
                logger.warning("Follow-up after saving %s failed", self._setting, exc_info=True)

    @discord.ui.button(label="Not now", style=discord.ButtonStyle.secondary)
    async def dismiss_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        self.stop()
        await interaction.response.edit_message(view=self)


async def offer_channel_setup(
    interaction: discord.Interaction,
    *,
    setting: str,
    guild_config_service,
    access_policy: AccessPolicy,
    on_saved=None,
) -> bool:
    """Offer to use the current channel when ``setting`` is not configured yet.

    Sends one private follow-up with a button; returns whether it was offered. Never
    raises: a failed offer must not break the command that triggered it.
    ``on_saved(channel_id)`` runs after the manager accepts.
    """
    try:
        guild_id = interaction_guild_id(interaction)
        channel = getattr(interaction, "channel", None)
        channel_id = getattr(channel, "id", None) or getattr(interaction, "channel_id", None)
        if guild_id is None or channel_id is None or guild_config_service is None:
            return False
        if not hasattr(guild_config_service, "update_for_guild"):
            return False
        settings = await guild_config_service.get_settings(guild_id)
        if getattr(settings, setting, None):
            return False
        if not is_announcement_target(channel):
            return False
        if not await access_policy.can_manage(interaction) or not bot_can_post(channel):
            return False
        what = CHANNEL_SETTINGS[setting][0]
        view = ChannelSetupView(
            author_id=interaction.user.id,
            guild_id=guild_id,
            channel_id=int(channel_id),
            setting=setting,
            guild_config_service=guild_config_service,
            access_policy=access_policy,
            on_saved=on_saved,
        )
        view.message = await interaction.followup.send(
            embed=build_status_embed(
                title="One more step",
                description=(
                    f"This server has no channel for {what} yet, so they are not posted anywhere. "
                    f"Post them in <#{channel_id}>?"
                ),
                color=EmbedColors.INFO,
                footer="You can pick another channel in /settings.",
            ),
            view=view,
            ephemeral=True,
            wait=True,
        )
        return True
    except Exception:
        logger.warning("Could not offer channel setup for %s", setting, exc_info=True)
        return False
