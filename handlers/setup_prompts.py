"""Checks on the channels the bot posts in: may it send, attach, and is it a lasting target."""

from __future__ import annotations

import discord


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
