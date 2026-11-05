"""Shared access policy for bot commands and panels.

Three levels are used across the bot:

* **Bot operators** (``ADMIN_USER_IDS``) can run global actions that affect every
  server: the shared knowledge base, unassigned legacy players, global player
  toggles.
* **Server managers** can manage the bot inside one server: members with the
  Discord *Manage Server* or *Administrator* permission, members holding the
  manager role chosen in ``/configure``, and bot operators.
* Everyone else can use member features.

Slash commands that only managers should see are additionally hidden with
``manager_only_command`` (``default_permissions(manage_guild=True)``). Server
owners can still expose them to other roles in *Server Settings → Integrations*.
The runtime check below always runs, so hiding is a convenience, not the guard.
"""

from __future__ import annotations

import logging
from typing import Iterable

import discord
from discord import app_commands

from handlers.ui import EmbedColors

logger = logging.getLogger(__name__)

# Hide manager commands from members by default (runtime checks still apply).
manager_only_command = app_commands.default_permissions(manage_guild=True)
# Hide operator-only commands from everyone except server administrators.
operator_only_command = app_commands.default_permissions(administrator=True)


def interaction_guild_id(interaction) -> int | None:
    """Guild ID of an interaction (tolerates lightweight test doubles)."""
    guild_id = getattr(interaction, "guild_id", None)
    if guild_id is None:
        guild_id = getattr(getattr(interaction, "guild", None), "id", None)
    return guild_id


def _has_permission(member, name: str) -> bool:
    permissions = getattr(member, "guild_permissions", None)
    return getattr(permissions, name, False) is True


def build_denied_embed(description: str) -> discord.Embed:
    return discord.Embed(title="🔒 Not allowed", description=description, color=EmbedColors.WARNING)


async def send_denied(interaction: discord.Interaction, description: str) -> None:
    """Send the standard ephemeral permission denial, whatever the response state."""
    embed = build_denied_embed(description)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        logger.debug("Could not deliver permission denial", exc_info=True)


class AccessPolicy:
    """Decides who may run manager and operator actions."""

    def __init__(
        self,
        super_admin_ids: Iterable[int] | None = None,
        *,
        guild_configuration_service=None,
        banned_user_ids: Iterable[int] | None = None,
    ):
        self._super_admin_ids = {int(user_id) for user_id in (super_admin_ids or ())}
        self._guild_configuration_service = guild_configuration_service
        self._banned_user_ids = {int(user_id) for user_id in (banned_user_ids or ())}

    @property
    def super_admin_ids(self) -> frozenset[int]:
        return frozenset(self._super_admin_ids)

    def is_super_admin(self, user_id: int | None) -> bool:
        return user_id is not None and int(user_id) in self._super_admin_ids

    def is_banned(self, user_id: int | None) -> bool:
        return user_id is not None and int(user_id) in self._banned_user_ids

    async def manager_role_id(self, guild_id: int | None) -> int | None:
        if guild_id is None or self._guild_configuration_service is None:
            return None
        try:
            service = self._guild_configuration_service
            loader = getattr(service, "get_settings", None) or service.get_or_create_for_guild
            settings = await loader(guild_id)
        except Exception:
            logger.warning("Could not load manager role for guild %s", guild_id, exc_info=True)
            return None
        return getattr(settings, "manager_role_id", None)

    async def is_guild_manager(self, member, guild_id: int | None) -> bool:
        """Return True when ``member`` may manage the bot in ``guild_id``."""
        if member is None:
            return False
        if self.is_super_admin(getattr(member, "id", None)):
            return True
        if guild_id is None:
            return False
        if _has_permission(member, "administrator") or _has_permission(member, "manage_guild"):
            return True
        role_id = await self.manager_role_id(guild_id)
        if role_id is None:
            return False
        member_role_ids = {getattr(role, "id", None) for role in getattr(member, "roles", None) or ()}
        return role_id in member_role_ids

    async def can_manage(self, interaction: discord.Interaction) -> bool:
        return await self.is_guild_manager(interaction.user, interaction_guild_id(interaction))

    async def require_manager(self, interaction: discord.Interaction, action: str = "do this") -> bool:
        """Return True if allowed; otherwise send the standard denial and return False."""
        if interaction_guild_id(interaction) is None and not self.is_super_admin(interaction.user.id):
            await send_denied(interaction, "Use this in a server.")
            return False
        if await self.can_manage(interaction):
            return True
        await send_denied(
            interaction,
            f"You need **Manage Server** or the bot manager role to {action}.",
        )
        return False

    async def require_super_admin(self, interaction: discord.Interaction, action: str = "do this") -> bool:
        if self.is_super_admin(interaction.user.id):
            return True
        await send_denied(interaction, f"Only the bot operator can {action}.")
        return False
