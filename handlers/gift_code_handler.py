"""Gift-code commands: per-server rosters, redemption jobs and code polling.

Orchestration that is not Discord UI (bulk redemption, the retry policy, result
persistence, the per-code job guard) lives in ``services.gift_redemption_service``.
"""

import asyncio
import logging
import re
import socket
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config.bot_config import BotConfig
from handlers.gift_panel import GiftPanelView, build_panel_embed
from handlers.permissions import AccessPolicy, interaction_guild_id, manager_only_command, send_denied
from handlers.setup_prompts import bot_can_post, is_announcement_target, offer_channel_setup
from handlers.ui import EmbedColors, OwnedView, build_status_embed, send_ui_error
from services.gift_code_service import IGiftCodeService
from services.gift_redemption_service import (
    ALREADY_REDEEMED_ERROR_CODES,
    JOB_AUTO,
    JOB_MANUAL,
    STATUS_ALREADY_REDEEMED,
    STATUS_API_REJECTED,
    STATUS_INVALID_ID,
    STATUS_LABELS,
    STATUS_ORDER,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
    STOP_SHUTDOWN,
    CodeJobGuard,
    GiftRedemptionService,
    RedemptionJob,
    RosterHealthReport,
    count_statuses,
    format_status_counts,
    has_success,
    is_global_code_rejection,
    is_retryable_result,
)
from services.interaction_tracking_service import InteractionTrackingService
from services.kingshot_api import HOSTNAME as GIFT_CODE_SITE, MIN_REQUEST_INTERVAL_SECONDS
from services.kingshot_data_service import KingshotDataService
from services.mightpulse_service import (
    MightPulseAllianceDirectory,
    MightPulseService,
    MightPulseUnavailableError,
)
from services.player_info_service import IPlayerInfoService, PlayerLookupUnavailable
from services.player_registry_service import PlayerRegistryService, RegistrationOutcome

logger = logging.getLogger(__name__)

EMBED_FIELD_LIMIT = 1024
# Lists in multi-field embeds stay well below the 6000-character embed total.
EMBED_LIST_LIMIT = 500
# Value of the placeholder autocomplete choice shown while alliances still load.
ALLIANCE_LOADING_VALUE = "__loading__"
MAX_IDS_PER_ADDPLAYER = 50
GIFT_CODE_PATTERN = re.compile(r"^[^\s`@<>]{1,64}$")
# A probe that fails with one of these still proves the code itself exists.
CODE_VALID_ERROR_CODES = frozenset({"CASTLE_LEVEL_TOO_LOW", "SPEND_REQUIREMENT_NOT_MET", "VIP_REQUIREMENT_NOT_MET"})
JOB_PROBE = "probe"
ACCOUNT_ERROR_HINTS = {
    "KINGDOM_MISMATCH": "kingdom does not match",
    "INVALID_PLAYER_ID": "Governor ID not found",
    "INVALID_USER_ID": "Governor ID not found",
    "CASTLE_LEVEL_TOO_LOW": "Town Center level too low",
    "SPEND_REQUIREMENT_NOT_MET": "spending requirement not met",
    "VIP_REQUIREMENT_NOT_MET": "VIP requirement not met",
}
REDEEM_HOW_TO = (
    f"To claim a code yourself, enter your Governor ID and the code at <{GIFT_CODE_SITE}>; "
    "rewards arrive in your in-game mail."
)


def bounded_join(
    items: Iterable[Any],
    *,
    separator: str = "\n",
    limit: int = EMBED_FIELD_LIMIT,
    empty: str = "None",
) -> str:
    """Join ``items`` within ``limit`` characters, ending with "…and N more" if cut."""
    values = [str(item) for item in items]
    if not values:
        return empty
    joined = ""
    for index, value in enumerate(values):
        left = len(values) - index - 1
        reserve = len(f"{separator}…and {left} more") if left else 0
        if not joined and len(value) + reserve > limit:
            value = value[: max(1, limit - reserve - 1)] + "…"
        candidate = value if not joined else f"{joined}{separator}{value}"
        if len(candidate) + reserve > limit:
            return f"{joined}{separator}…and {len(values) - index} more"
        joined = candidate
    return joined


def progress_bar(done: int, total: int, width: int = 12) -> str:
    ratio = 0.0 if total <= 0 else max(0.0, min(1.0, done / total))
    filled = round(ratio * width)
    return f"{'▰' * filled}{'▱' * (width - filled)} {int(ratio * 100)}%"


def _player_label(player_id: str, name: Optional[str]) -> str:
    safe_name = discord.utils.escape_markdown(str(name)[:60]) if name else None
    return f"`{player_id}`" + (f" ({safe_name})" if safe_name else "")


class PlayerSearchModal(discord.ui.Modal, title="Find a player"):
    query = discord.ui.TextInput(label="Player name or ID", required=False, max_length=100)

    def __init__(self, view: "PlayerListPaginationView"):
        super().__init__(timeout=180)
        self.player_view = view
        self.query.default = view.query

    async def on_submit(self, interaction: discord.Interaction):
        view = self.player_view
        if not await view.interaction_check(interaction):
            return
        if view.is_finished():
            await interaction.response.send_message("This player list has expired. Run /listplayers again.", ephemeral=True)
            return
        view.query = str(self.query).strip()
        view.current_page = 0
        view.refresh()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await send_ui_error(interaction, error)


class PlayerListPaginationView(OwnedView):
    """Search, filter and manage this server's gift-code roster in one message."""

    PAGE_SIZE = 10

    def __init__(
        self,
        handler: "GiftCodeHandler",
        players: list[Any],
        author_id: int,
        *,
        guild_id: int | None = None,
        can_manage: bool = False,
        is_operator: bool = False,
    ):
        super().__init__(author_id)
        self.handler = handler
        self.roster = list(players)
        self.unassigned: list[Any] | None = None
        self.guild_id = guild_id
        self.can_manage = can_manage
        self.is_operator = is_operator
        self.query = ""
        self.status = "all"
        self.current_page = 0
        self.selected_index = 0
        self.selected_player = None
        if is_operator:
            self.status_select.append_option(
                discord.SelectOption(
                    label="Unassigned (legacy)",
                    value="unassigned",
                    description="Profiles not registered to any server",
                )
            )
        if not can_manage:
            self.remove_item(self.health_button)
        if not is_operator:
            self.remove_item(self.assign_button)
        self.refresh()

    @property
    def players(self) -> list[Any]:
        return (self.unassigned or []) if self.status == "unassigned" else self.roster

    def page_entries(self) -> list[Any]:
        start = self.current_page * self.PAGE_SIZE
        return self.filtered_players[start : start + self.PAGE_SIZE]

    def may_toggle(self, entry: Any) -> bool:
        if self.status == "unassigned":
            return self.is_operator
        return self.can_manage or getattr(entry, "added_by_user_id", None) == self.author_id

    def refresh(self, *, keep_selection: bool = False) -> None:
        query = self.query.casefold()
        self.filtered_players = [
            player for player in self.players
            if (self.status in {"all", "unassigned"} or bool(player.enabled) == (self.status == "enabled"))
            and (query in str(player.player_id).casefold()
                 or query in str(player.player_name or "").casefold())
        ]
        self.page_count = max(1, (len(self.filtered_players) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.current_page = min(max(0, self.current_page), self.page_count - 1)
        page = self.page_entries()
        if not keep_selection:
            self.selected_index = 0
        self.selected_index = min(self.selected_index, max(0, len(page) - 1))
        self.selected_player = page[self.selected_index] if page else None
        self.player_select.options = [
            discord.SelectOption(
                label=f"{player.player_name or 'Unnamed player'} · {player.player_id}"[:100],
                value=str(index), default=index == self.selected_index,
            ) for index, player in enumerate(page)
        ] or [discord.SelectOption(label="No matching players", value="none")]
        self.player_select.disabled = not page
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == self.page_count - 1
        self.clear_button.disabled = not self.query
        self.view_player.disabled = not page or self.handler._bot.tree.get_command("stats") is None
        for option in self.status_select.options:
            option.default = option.value == self.status
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        entry = self.selected_player
        if entry is None:
            self.toggle_button.label = "Enable / Disable"
            self.toggle_button.style = discord.ButtonStyle.secondary
            self.toggle_button.disabled = True
        else:
            self.toggle_button.label = "Disable" if entry.enabled else "Enable"
            self.toggle_button.style = discord.ButtonStyle.secondary if entry.enabled else discord.ButtonStyle.success
            self.toggle_button.disabled = not self.may_toggle(entry)
        self.assign_button.disabled = not (
            self.status == "unassigned" and any(player.enabled for player in self.unassigned or [])
        )

    def build_embed(self) -> discord.Embed:
        source = self.players
        enabled = sum(bool(player.enabled) for player in source)
        lines = self.handler._build_player_lines(self.page_entries())
        if self.status == "unassigned":
            title = "📋 Unassigned Player Profiles"
            description = (
                f"**{len(self.filtered_players)} matches** · {len(source)} unassigned · "
                f"{enabled} enabled · {len(source) - enabled} disabled\n"
                "Legacy profiles from before per-server rosters. Enabled ones still get "
                "auto-redemption until they are assigned to a server."
            )
        else:
            title = "📋 Server Gift-Code Roster"
            description = (
                f"**{len(self.filtered_players)} matches** · {len(source)} total · "
                f"{enabled} enabled · {len(source) - enabled} disabled"
            )
        description += f"\nFilter: **{self.status.title()}**"
        if self.query:
            description += f" · Search: {discord.utils.escape_markdown(self.query)}"
        embed = discord.Embed(
            title=title, color=EmbedColors.INFO,
            description=description + "\n\n" + ("\n".join(lines) or "No players match. Clear the search or change the filter."),
        )
        footer = f"Page {self.current_page + 1}/{self.page_count} · Select a player, then View player or Enable/Disable"
        if self.status != "unassigned":
            footer += f" · Roster {len(self.roster)}/{self.handler._max_players_per_guild}"
        embed.set_footer(text=footer)
        return embed

    @discord.ui.select(placeholder="Select a player", row=0)
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        index = int(select.values[0])
        self.selected_index = index
        self.selected_player = self.filtered_players[self.current_page * self.PAGE_SIZE + index]
        for option in select.options:
            option.default = option.value == select.values[0]
        self._refresh_actions()
        await interaction.response.edit_message(view=self)

    @discord.ui.select(placeholder="Redemption status", row=1, options=[
        discord.SelectOption(label="All players", value="all"),
        discord.SelectOption(label="Enabled", value="enabled"),
        discord.SelectOption(label="Disabled", value="disabled"),
    ])
    async def status_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.status = select.values[0]
        if self.status == "unassigned" and self.unassigned is None:
            self.unassigned = await self.handler._player_registry_service.list_unassigned_players()
        self.current_page = 0
        self.refresh()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=2)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self.refresh()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=2)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self.refresh()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Search", style=discord.ButtonStyle.secondary, row=2)
    async def search_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PlayerSearchModal(self))

    @discord.ui.button(label="Clear search", style=discord.ButtonStyle.secondary, row=2)
    async def clear_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.query = ""
        self.current_page = 0
        self.refresh()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="View player", style=discord.ButtonStyle.primary, row=2)
    async def view_player(self, interaction: discord.Interaction, button: discord.ui.Button):
        command = self.handler._bot.tree.get_command("stats")
        if command is None or self.selected_player is None:
            await interaction.response.send_message("Player statistics are unavailable in this bot profile.", ephemeral=True)
            return
        await command.callback(interaction, player_id=str(self.selected_player.player_id))

    @discord.ui.button(label="Enable / Disable", style=discord.ButtonStyle.secondary, row=3)
    async def toggle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handler._toggle_roster_entry(interaction, self)

    @discord.ui.button(label="Roster health", style=discord.ButtonStyle.secondary, row=3)
    async def health_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handler._send_roster_health(interaction, self)

    @discord.ui.button(label="Assign unassigned here", style=discord.ButtonStyle.danger, row=3)
    async def assign_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handler._assign_unassigned_players(interaction, self)


class RemovePlayerConfirmView(OwnedView):
    """Private confirm step before a roster entry is removed."""

    def __init__(self, handler: "GiftCodeHandler", author_id: int, guild_id: int, entry: Any):
        super().__init__(author_id, timeout=120)
        self.handler = handler
        self.guild_id = guild_id
        self.entry = entry

    @discord.ui.button(label="Remove from this server", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handler._confirm_remove_player(interaction, self)

    @discord.ui.button(label="Keep", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=build_status_embed(
                title="Nothing Changed",
                description=f"{_player_label(self.entry.player_id, self.entry.player_name)} stays in this server's roster.",
                color=EmbedColors.INFO,
            ),
            view=None,
        )


class RosterHealthView(OwnedView):
    """Offers to disable players that keep failing as invalid accounts."""

    def __init__(self, handler: "GiftCodeHandler", author_id: int, guild_id: int, failing: list[Any]):
        super().__init__(author_id)
        self.handler = handler
        self.guild_id = guild_id
        self.failing = failing
        self.disable_button.label = f"Disable these ({len(failing)})"

    @discord.ui.button(label="Disable these", style=discord.ButtonStyle.danger)
    async def disable_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handler._disable_failing_players(interaction, self)


class GiftCodesView(OwnedView):
    PAGE_SIZE = 10

    def __init__(self, handler: "GiftCodeHandler", codes: list[dict], author_id: int, can_redeem: bool):
        super().__init__(author_id)
        self.handler = handler
        self.codes = codes
        self.current_page = 0
        self.selected_index = 0
        self.can_redeem = can_redeem
        if not can_redeem:
            # Members cannot start jobs, so they get no code picker either.
            self.remove_item(self.code_select)
            self.remove_item(self.redeem_button)
        self.refresh()

    @property
    def page_count(self) -> int:
        return max(1, (len(self.codes) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def page_codes(self) -> list[dict]:
        start = self.current_page * self.PAGE_SIZE
        return self.codes[start:start + self.PAGE_SIZE]

    def refresh(self):
        start = self.current_page * self.PAGE_SIZE
        self.selected_index = start
        self.code_select.options = [
            discord.SelectOption(label=str(code["code"])[:100], value=str(index), default=index == start)
            for index, code in enumerate(self.codes[start:start + self.PAGE_SIZE], start)
        ]
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = start + self.PAGE_SIZE >= len(self.codes)

    def build_content(self) -> str:
        """Codes as plain inline-code lines, easy to copy on mobile."""
        return "\n".join(f"`{str(code['code']).replace('`', '')[:100]}`" for code in self.page_codes())

    def build_embed(self) -> discord.Embed:
        description = REDEEM_HOW_TO
        if self.can_redeem:
            description += "\nPick a code below to redeem it for this server's roster."
        embed = discord.Embed(title="🎁 Active Gift Codes", color=EmbedColors.INFO, description=description)
        for code in self.page_codes():
            expiry = "Expiration unavailable"
            if code.get("expiresAt"):
                try:
                    dt = datetime.fromisoformat(code["expiresAt"].replace("Z", "+00:00"))
                    expiry = f"Expires <t:{int(dt.timestamp())}:R>"
                except (ValueError, TypeError, AttributeError):
                    pass
            embed.add_field(name=str(code["code"])[:256], value=expiry, inline=False)
        embed.set_footer(text=f"Page {self.current_page + 1}/{self.page_count} · Auto-redemption runs when new codes appear")
        return embed

    async def _show_page(self, interaction: discord.Interaction) -> None:
        self.refresh()
        await interaction.response.edit_message(content=self.build_content(), embed=self.build_embed(), view=self)

    @discord.ui.select(placeholder="Select a gift code", row=0)
    async def code_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_index = int(select.values[0])
        for option in select.options:
            option.default = option.value == select.values[0]
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=1)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(0, self.current_page - 1)
        await self._show_page(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = min(self.page_count - 1, self.current_page + 1)
        await self._show_page(interaction)

    @discord.ui.button(label="Redeem selected code", style=discord.ButtonStyle.primary, row=1)
    async def redeem_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handler._handle_redeem_gift_code_slash(interaction, str(self.codes[self.selected_index]["code"]))


class RedemptionProgressView(discord.ui.View):
    """Cancel control on a running manual job (requester or server managers)."""

    def __init__(self, handler: "GiftCodeHandler", job: RedemptionJob):
        super().__init__(timeout=None)
        self.handler = handler
        self.job = job
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        user_id = interaction.user.id
        if user_id == self.job.requester_id:
            return True
        access = self.handler._access
        if self.job.all_servers:
            allowed = access.is_super_admin(user_id)
        else:
            allowed = await access.is_guild_manager(interaction.user, self.job.guild_id)
        if not allowed:
            await send_denied(interaction, "Only the person who started this job or a server manager can cancel it.")
        return allowed

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await send_ui_error(interaction, error)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.job.request_stop(user_id=interaction.user.id):
            await interaction.response.send_message("This job is already stopping.", ephemeral=True)
            return
        button.disabled = True
        button.label = "Cancelling…"
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            "Stopping after the players in progress finish. Results so far are saved.", ephemeral=True
        )


class RedemptionResultsView(OwnedView):
    PAGE_SIZE = 4

    def __init__(
        self,
        summary: discord.Embed,
        issues: list[dict],
        author_id: int,
        *,
        access_policy: AccessPolicy | None = None,
        timeout: float = 180.0,
        all_servers: bool = False,
    ):
        super().__init__(author_id, timeout=timeout)
        self._access = access_policy
        self._all_servers = all_servers
        self.summary = summary
        self.issues = issues
        self.current_page = 0
        self.expanded = False
        self.refresh()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """The requester and server managers may browse the failures of a job."""
        if interaction.user.id == self.author_id:
            return True
        if self._access is not None:
            # An all-servers run lists players of every server: operators only.
            if self._all_servers:
                allowed = self._access.is_super_admin(interaction.user.id)
            else:
                allowed = await self._access.can_manage(interaction)
            if allowed:
                return True
        await interaction.response.send_message(
            "Only the person who started this job or a server manager can open its details.", ephemeral=True
        )
        return False

    def refresh(self):
        self.details_button.label = "Back to summary" if self.expanded else f"Details ({len(self.issues)})"
        self.prev_button.disabled = not self.expanded or self.current_page == 0
        self.next_button.disabled = not self.expanded or (self.current_page + 1) * self.PAGE_SIZE >= len(self.issues)

    def build_embed(self) -> discord.Embed:
        if not self.expanded:
            return self.summary
        embed = discord.Embed(title="Redemption · Failure and skipped details", color=EmbedColors.WARNING,
                              description="Player errors, rejected requests, and players skipped without a request.")
        start = self.current_page * self.PAGE_SIZE
        for result in self.issues[start:start + self.PAGE_SIZE]:
            status = STATUS_LABELS.get(result.get("status_category"), STATUS_LABELS[STATUS_API_REJECTED])
            name = str(result.get("player_name") or "Unnamed player")[:80]
            player_id = str(result.get("player_id") or "Unavailable")
            embed.add_field(name=f"{status} · {name} · {player_id}"[:256],
                            value=str(result.get("message") or "Details unavailable")[:1024], inline=False)
        embed.set_footer(text=f"Page {self.current_page + 1}/{(len(self.issues) + self.PAGE_SIZE - 1) // self.PAGE_SIZE}")
        return embed

    @discord.ui.button(label="Details", style=discord.ButtonStyle.secondary)
    async def details_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.expanded = not self.expanded
        self.refresh()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(0, self.current_page - 1)
        self.refresh()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = min((len(self.issues) - 1) // self.PAGE_SIZE, self.current_page + 1)
        self.refresh()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)


class GiftCodeHandler:
    """Handles gift code redemption Discord commands."""

    STATUS_SUCCESS = STATUS_SUCCESS
    STATUS_ALREADY_REDEEMED = STATUS_ALREADY_REDEEMED
    STATUS_API_REJECTED = STATUS_API_REJECTED
    STATUS_INVALID_ID = STATUS_INVALID_ID
    STATUS_SKIPPED = STATUS_SKIPPED
    ALLIANCE_CACHE_TTL_SECONDS = 10 * 60
    ALLIANCE_AUTOCOMPLETE_WAIT_SECONDS = 2.4
    ALLIANCE_AUTOCOMPLETE_MAX_CHOICES = 15
    ALLIANCE_KID_PREWARM_DEBOUNCE_SECONDS = 0.35
    ALLIANCE_CHOICE_NAME_MAX_LENGTH = 100
    ALLIANCE_COMPLETION_RETRY_SECONDS = 30.0
    PROGRESS_UPDATE_INTERVAL_SECONDS = 5.0
    SHUTDOWN_GRACE_SECONDS = 8.0
    AUTO_RETRY_AFTER_CRASH_SECONDS = 10 * 60
    # Passes that end with temporary failures (outage, rate limit) keep the code pending
    # and retry the unsettled players later, up to this many passes per code.
    AUTO_RETRY_AFTER_TRANSIENT_SECONDS = 15 * 60
    MAX_AUTO_TRANSIENT_PASSES = 6
    # A code without an expiry date is abandoned after this long if the gift-code
    # server never lets a pass through.
    MAX_AUTO_PENDING_HOURS = 72
    # How long the Details button of a finished manual run keeps working.
    RESULT_DETAILS_TIMEOUT_SECONDS = 6 * 3600.0
    ROSTER_HEALTH_CODES = 5
    # A running automatic redemption keeps one announcement per channel up to date.
    # It is posted only while enough of the run is left for the news to matter.
    LIVE_ANNOUNCE_INTERVAL_SECONDS = 60.0
    LIVE_ANNOUNCE_MIN_REMAINING_SECONDS = 60.0
    # Accounts that failed as invalid on this many of the latest codes are paused.
    AUTO_DISABLE_CODES = 5
    AUTO_DISABLE_MIN_FAILURES = 3
    # Member code submissions: each one costs a request to the gift-code server.
    SUBMIT_USER_COOLDOWN_SECONDS = 60.0
    SUBMIT_USER_MAX_PER_HOUR = 5
    SUBMIT_GUILD_MAX_PER_HOUR = 10
    SUBMIT_MAX_PER_HOUR = 40
    MY_ACCOUNTS_CODES = 3
    MY_ACCOUNTS_LISTED = 10

    def __init__(
        self,
        gift_code_service: IGiftCodeService,
        player_info_service: IPlayerInfoService,
        bot: commands.Bot,
        config: BotConfig,
        interaction_tracking_service: InteractionTrackingService | None = None,
        player_registry_service: PlayerRegistryService | None = None,
        kingshot_data_service: KingshotDataService | None = None,
        guild_configuration_service=None,
        access_policy: AccessPolicy | None = None,
        mightpulse_service: MightPulseService | None = None,
        alliance_directory_service: MightPulseAllianceDirectory | None = None,
    ):
        self._gift_code_service = gift_code_service
        self._player_info_service = player_info_service
        self._bot = bot
        self._config = config
        self._tracking_service = interaction_tracking_service or InteractionTrackingService()
        self._player_registry_service = player_registry_service or PlayerRegistryService()
        self._kingshot_data_service = kingshot_data_service
        self._guild_configuration_service = guild_configuration_service
        self._access = access_policy or AccessPolicy(
            getattr(config, "admin_user_ids", None) or set(),
            guild_configuration_service=guild_configuration_service,
        )
        self._redemption = GiftRedemptionService(
            gift_code_service,
            self._tracking_service,
            kingshot_data_service,
        )
        # One running job per gift code, shared by automatic and manual runs.
        self._jobs = CodeJobGuard()
        self._job_start_lock = asyncio.Lock()
        self._closing = False
        self._tracked_bot_user_id: int | None = None
        self._auto_retry_after: dict[str, float] = {}
        self._auto_transient_passes: dict[str, int] = {}
        # Summaries already posted for a code that is still retrying, by channel ID, so
        # later passes edit them instead of posting a second summary.
        self._auto_announcements: dict[str, dict[int, Any]] = {}
        # Codes whose stored announcement messages were already loaded from the database.
        self._announcements_loaded: set[str] = set()
        # Runs pending codes, so the one-minute discovery loop never waits for a run.
        self._pending_worker: asyncio.Task | None = None
        self._submit_cooldowns: dict[int, float] = {}
        self._submit_times: list[tuple[float, int, int | None]] = []
        self._persistent_views_registered = False
        self._mightpulse_service = mightpulse_service
        self._alliance_directory_service = alliance_directory_service
        self._polling_loop = None
        self._poll_backoff_until: datetime | None = None
        self._poll_backoff_delta = timedelta(minutes=2)
        self._alliance_cache: Dict[
            int,
            tuple[float, List[Dict[str, Any]], bool],
        ] = {}
        self._alliance_refresh_tasks: Dict[
            int,
            asyncio.Task[Optional[List[Dict[str, Any]]]],
        ] = {}
        self._alliance_completion_tasks: Dict[
            int,
            asyncio.Task[Optional[List[Dict[str, Any]]]],
        ] = {}
        self._alliance_completion_retry_after: Dict[int, float] = {}
        self._alliance_prewarm_tasks: Dict[
            int,
            tuple[int, asyncio.Task[None]],
        ] = {}
        self._register_cleanup()
        logger.info("GiftCodeHandler initialized")

    # ------------------------------------------------------------ lifecycle

    def _register_cleanup(self) -> None:
        add_cleanup = getattr(self._bot, "add_cleanup", None)
        if not callable(add_cleanup):
            return
        close_client = getattr(self._gift_code_service, "close", None)
        if callable(close_client):
            add_cleanup("gift-code API client", close_client)
        # Registered last, so it runs first: stop jobs before their clients close.
        add_cleanup("gift-code redemption jobs", self.shutdown)

    async def shutdown(self) -> None:
        """Stop polling and redemption jobs; finished results are already saved.

        Running jobs get ``SHUTDOWN_GRACE_SECONDS`` to finish the players in flight,
        then they are cancelled. Automatic runs stay ``pending`` and resume after the
        restart; manual progress cards are marked as stopped.
        """
        self._closing = True
        await self._jobs.shutdown(grace_seconds=self.SHUTDOWN_GRACE_SECONDS)
        if self._polling_loop is not None and self._polling_loop.is_running():
            self._polling_loop.cancel()
        worker = self._pending_worker
        if worker is not None and not worker.done():
            worker.cancel()
            await asyncio.wait({worker}, timeout=2.0)
        for _, task in list(self._alliance_prewarm_tasks.values()):
            task.cancel()

    @property
    def _max_players_per_guild(self) -> int:
        return int(getattr(self._config, "max_players_per_guild", None) or 300)

    @property
    def _max_self_registered(self) -> int:
        value = getattr(self._config, "max_self_registered_players", None)
        return 3 if value is None else int(value)

    def _can_poll(self) -> bool:
        return self._poll_backoff_until is None or datetime.now(timezone.utc) >= self._poll_backoff_until

    def _mark_poll_backoff(self, error: Exception) -> None:
        self._poll_backoff_until = datetime.now(timezone.utc) + self._poll_backoff_delta
        logger.warning("Gift code polling will retry after %s because of networking error: %s", self._poll_backoff_delta, error)

    def register_commands(self):
        """Register all gift code commands with the bot."""

        @self._bot.tree.command(name="redeem", description="Redeem a gift code for this server's enabled roster")
        @app_commands.guild_only()
        @manager_only_command
        @app_commands.describe(
            gift_code="The gift code to redeem (e.g., KINGSHOTXMAS)",
            all_servers="Bot operators only: redeem for every server's players",
        )
        async def redeem_gift_code(interaction: discord.Interaction, gift_code: str, all_servers: bool = False):
            """Redeem a gift code for this server's roster."""
            await self._handle_redeem_gift_code_slash(interaction, gift_code, all_servers=all_servers)

        @self._bot.tree.command(name="addplayer", description="Add Governor IDs to this server's gift-code roster")
        @app_commands.guild_only()
        @app_commands.describe(player_ids="The player ID(s) to add, comma-separated")
        async def add_player(interaction: discord.Interaction, player_ids: str):
            """Add players to this server's gift-code roster."""
            await self._handle_add_player_slash(interaction, player_ids)

        @self._bot.tree.command(name="addalliance", description="Import an alliance into this server's gift-code roster")
        @app_commands.guild_only()
        @manager_only_command
        @app_commands.describe(
            kid="Kingdom ID",
            alliance="Top-15 alliance by power (may take a moment to load)",
        )
        async def add_alliance(
            interaction: discord.Interaction,
            kid: int,
            alliance: str,
        ):
            """Add an alliance selected from the kingdom's autocomplete results."""
            await self._handle_add_alliance_choice_slash(interaction, kid, alliance)

        @add_alliance.autocomplete("alliance")
        async def alliance_autocomplete(
            interaction: discord.Interaction,
            current: str,
        ) -> List[app_commands.Choice[str]]:
            """Offer alliances from the kingdom option without exposing their IDs."""
            kid = getattr(interaction.namespace, "kid", None)
            return await self._get_alliance_autocomplete_choices(kid, current)

        @add_alliance.autocomplete("kid")
        async def kid_autocomplete(
            interaction: discord.Interaction,
            current: int,
        ) -> List[app_commands.Choice[int]]:
            """Accept a positive kingdom ID while warming its alliance cache."""
            return await self._get_kid_autocomplete_choices(interaction, current)

        @self._bot.tree.command(name="removeplayer", description="Remove a player from this server's gift-code roster")
        @app_commands.guild_only()
        @app_commands.describe(player_id="The player ID to remove")
        async def remove_player(interaction: discord.Interaction, player_id: str):
            """Remove a player from this server's roster."""
            await self._handle_remove_player_slash(interaction, player_id)

        @self._bot.tree.command(name="listplayers", description="Browse and manage this server's gift-code roster")
        @app_commands.guild_only()
        async def list_players(interaction: discord.Interaction):
            """List this server's roster."""
            await self._handle_list_players_slash(interaction)

        @self._bot.tree.command(name="playerlist", description="Alias for /listplayers")
        @app_commands.guild_only()
        async def player_list_alias(interaction: discord.Interaction):
            """Alias command for listing this server's roster."""
            await self._handle_list_players_slash(interaction)

        @self._bot.tree.command(name="giftcodes", description="List available gift codes")
        async def list_giftcodes(interaction: discord.Interaction):
            """List available gift codes from the API."""
            await self._handle_list_gift_codes_slash(interaction)

        @self._bot.tree.command(name="myaccounts", description="Your accounts on this server's gift-code list")
        @app_commands.guild_only()
        async def my_accounts(interaction: discord.Interaction):
            """Show the caller's own roster entries and their latest results."""
            await self._handle_my_accounts(interaction)

        @self._bot.tree.command(name="submitcode", description="Send in a gift code the bot has not found yet")
        @app_commands.guild_only()
        @app_commands.describe(code="The gift code, exactly as published")
        async def submit_code(interaction: discord.Interaction, code: str):
            """Validate a member-submitted code and queue it for everyone."""
            await self._handle_submit_code(interaction, code)

        @self._bot.tree.command(name="giftpanel", description="Post the gift-code panel with buttons in this channel")
        @app_commands.guild_only()
        @manager_only_command
        async def gift_panel(interaction: discord.Interaction):
            """Post the permanent gift-code panel."""
            await self._handle_gift_panel(interaction)

    def register_persistent_views(self) -> None:
        """Make the buttons of gift-code panels posted earlier work again (call once, on ready)."""
        add_view = getattr(self._bot, "add_view", None)
        if self._persistent_views_registered or not callable(add_view):
            return
        add_view(GiftPanelView(self))
        self._persistent_views_registered = True

    # -------------------------------------------------------------- polling

    def start_polling_task(self):
        """Start the background task that checks for new gift codes."""
        if self._polling_loop and self._polling_loop.is_running():
            logger.info("Gift code polling task already running; skipping duplicate start")
            return

        @tasks.loop(minutes=1)
        async def poll_gift_codes():
            """Discover new codes every minute; redemption runs in its own task.

            A run takes two seconds per player, so awaiting it here would stop
            discovery (and the start of the next code) for the whole run.
            """
            await self._discover_gift_codes()
            self._ensure_pending_worker()

        # Wait until bot is fully ready before running the loop
        @poll_gift_codes.before_loop
        async def before_polling():
            logger.info("Waiting for bot to be ready before starting gift code polling...")
            await self._bot.wait_until_ready()

        self._polling_loop = poll_gift_codes
        poll_gift_codes.start()

    def is_polling_running(self) -> bool:
        """Return whether the gift-code polling loop is currently active."""
        return bool(self._polling_loop and self._polling_loop.is_running())

    async def _poll_gift_codes_once(self) -> None:
        """Discover codes, then run every pending one to completion (one full cycle)."""
        if self._closing:
            return
        await self._discover_gift_codes()
        # Pending codes are in the database, so they resume even when the
        # upstream list is temporarily unavailable.
        try:
            await self._process_pending_codes()
        except Exception:
            logger.exception("Error while processing pending gift codes")

    async def _discover_gift_codes(self) -> None:
        """Record codes from the upstream list; never raises (it runs inside the loop)."""
        if self._closing:
            return
        if not self._can_poll():
            logger.debug("Skipping gift-code polling due temporary network error backoff window")
            return
        try:
            response = await self._gift_code_service.get_available_gift_codes()
            if response.get("success"):
                new_codes = await self._record_discovered_codes(response.get("data") or [])
                if new_codes:
                    logger.info("Found %s new gift codes: %s", len(new_codes), ", ".join(new_codes))
            else:
                logger.warning("Failed to poll gift codes: %s", response.get("message"))
        except socket.gaierror as e:
            self._mark_poll_backoff(e)
            logger.error("Error in poll_gift_codes background task: %s", e, exc_info=True)
        except Exception:
            logger.exception("Error in poll_gift_codes background task")

    def _ensure_pending_worker(self) -> None:
        """Start the task that redeems pending codes unless it is already running."""
        if self._closing:
            return
        worker = self._pending_worker
        if worker is not None and not worker.done():
            return
        self._pending_worker = asyncio.create_task(self._run_pending_worker(), name="gift-code-pending-worker")

    async def _run_pending_worker(self) -> None:
        try:
            await self._process_pending_codes()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error while processing pending gift codes")

    async def _record_discovered_codes(self, rows: Iterable[Any]) -> list[str]:
        """Store codes from the upstream list; new ones start as pending auto-redemption."""
        new_codes: list[str] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            code_id = row.get("id")
            code_str = row.get("code")
            if not code_id or not code_str:
                continue
            try:
                created_at_api = row.get("createdAt")
                expires_at = row.get("expiresAt")
                dt_created = (
                    datetime.fromisoformat(created_at_api.replace("Z", "+00:00"))
                    if created_at_api
                    else datetime.now(timezone.utc)
                )
                dt_expires = datetime.fromisoformat(expires_at.replace("Z", "+00:00")) if expires_at else None
                is_new, _ = await self._gift_code_service.add_or_update_gift_code(
                    code_id=code_id,
                    code=code_str,
                    created_at_api=dt_created,
                    expires_at=dt_expires,
                )
            except (ValueError, AttributeError) as e:
                logger.error("Error parsing dates for gift code %s: %s", code_str, e)
                continue
            except Exception:
                logger.exception("Could not store gift code %s", code_str)
                continue
            if is_new:
                new_codes.append(str(code_str))
        return new_codes

    async def _process_pending_codes(self) -> None:
        """Run or resume the automatic redemption of every pending, unexpired code."""
        pending_codes = await self._gift_code_service.list_pending_gift_codes()
        for code_row in pending_codes:
            if self._closing:
                return
            code = str(code_row.code)
            expires_at = getattr(code_row, "expires_at", None)
            if expires_at is not None:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= datetime.now(timezone.utc):
                    logger.info("Gift code '%s' expired before auto-redemption finished; marking it completed", code)
                    await self._gift_code_service.mark_gift_code_completed(code_row.id)
                    await self._finish_expired_code(code_row)
                    continue
            retry_after = self._auto_retry_after.get(CodeJobGuard.key(code))
            if retry_after is not None and time.monotonic() < retry_after:
                continue
            if self._jobs.get(code) is not None:
                logger.info("Gift code '%s' is being redeemed by another job; auto-redemption resumes later", code)
                continue
            await self._run_auto_redemption(code_row)

    def _auto_pending_too_old(self, code_row: Any) -> bool:
        """Whether a still-pending code was discovered more than MAX_AUTO_PENDING_HOURS ago."""
        discovered_at = getattr(code_row, "discovered_at", None)
        if not isinstance(discovered_at, datetime):
            return False
        if discovered_at.tzinfo is None:
            discovered_at = discovered_at.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - discovered_at > timedelta(hours=self.MAX_AUTO_PENDING_HOURS)

    async def _finish_expired_code(self, code_row: Any) -> None:
        """Turn the announcements of a code that expired mid-retry into a final summary.

        Without this they would say "retried automatically" (or "redeeming") forever.
        """
        code = str(code_row.code)
        key = CodeJobGuard.key(code)
        try:
            if await self._load_announcements(code):
                targets = await self._player_registry_service.get_auto_redeem_targets()
                recorded = await self._redemption.load_recorded_outcomes(code)
                actor_user_id = await self._ensure_bot_user()
                results = [
                    self._redemption.recorded_outcome_result(player, recorded[str(player.player_id)], actor_user_id)
                    for player in targets
                    if str(player.player_id) in recorded
                ]
                await self._announce_auto_redemption(
                    code, results, final=True, note="The code expired before the remaining players could be retried."
                )
        except Exception:
            logger.exception("Could not finalize the announcements of expired code '%s'", code)
        finally:
            self._auto_announcements.pop(key, None)
            self._announcements_loaded.discard(key)
            self._auto_retry_after.pop(key, None)
            self._auto_transient_passes.pop(key, None)

    async def _ensure_bot_user(self) -> int:
        """The bot logs automatic attempts as itself (``users`` foreign key)."""
        bot_user = getattr(self._bot, "user", None)
        bot_user_id = int(bot_user.id) if bot_user else 0
        if self._tracked_bot_user_id != bot_user_id:
            await self._tracking_service.track_user(
                session=None,
                user_id=bot_user_id,
                username=bot_user.name if bot_user else "System Bot",
                discriminator=getattr(bot_user, "discriminator", "0000") if bot_user else "0000",
                display_name=getattr(bot_user, "display_name", "System Bot") if bot_user else "System Bot",
            )
            self._tracked_bot_user_id = bot_user_id
        return bot_user_id

    async def _run_auto_redemption(self, code_row: Any) -> bool:
        """Redeem one pending code for every auto-redeem target; True when completed."""
        code = str(code_row.code)
        job = RedemptionJob(gift_code=code, kind=JOB_AUTO)
        if self._jobs.claim(job) is not None:
            return False
        attached = False
        try:
            actor_user_id = await self._ensure_bot_user()
            targets = await self._player_registry_service.get_auto_redeem_targets(
                self_registered_max=self._max_self_registered
            )
            job.total = len(targets)
            expires_at = getattr(code_row, "expires_at", None)
            if not targets:
                logger.info("No enabled players to auto-redeem '%s' for; marking it completed", code)
                await self._gift_code_service.mark_gift_code_completed(code_row.id)
                return True
            if job.stop_requested:
                return False

            logger.info("Auto-redeeming code '%s' for %s players", code, len(targets))
            task = asyncio.create_task(
                self._redemption.run_bulk(
                    gift_code=code,
                    players=targets,
                    actor_user_id=actor_user_id,
                    guild_id=None,
                    channel_id=None,
                    progress=self._auto_progress(code, targets, expires_at, job),
                    stop_event=job.stop_event,
                    use_recorded_outcomes=True,
                    stop_on_transient_probe=True,
                ),
                name=f"auto-gift-code-{code}",
            )
            self._jobs.attach(job, task)
            attached = True
            await asyncio.wait({task})
            if task.cancelled() or job.stop_requested:
                logger.warning(
                    "Auto-redemption of '%s' stopped before finishing; it stays pending and resumes on the next poll",
                    code,
                )
                return False
            error = task.exception()
            if error is not None:
                self._auto_retry_after[job.key] = time.monotonic() + self.AUTO_RETRY_AFTER_CRASH_SECONDS
                logger.error(
                    "Auto-redemption of '%s' crashed; it stays pending and is retried in %s s",
                    code,
                    self.AUTO_RETRY_AFTER_CRASH_SECONDS,
                    exc_info=(type(error), error, error.__traceback__),
                )
                return False

            results = task.result()
            transient = [result for result in results if is_retryable_result(result)]
            globally_rejected = any(is_global_code_rejection(result) for result in results)
            # A pass that ended early (the gift-code server was down or rate limiting)
            # leaves players without any result. Such a pass says nothing about them, so
            # it does not count toward the pass limit: the code stays pending until it
            # expires (or, without an expiry, for MAX_AUTO_PENDING_HOURS).
            unreached = max(0, len(targets) - len(results))
            # The same goes for a pass in which nobody was settled at all (for example
            # a single remaining player during an outage): it proves nothing.
            settled_any = any(
                result.get("redemption_attempted") and not is_retryable_result(result) for result in results
            )
            outage_pass = unreached > 0 or (bool(transient) and not settled_any)
            passes = self._auto_transient_passes.get(job.key, 0) + (0 if outage_pass else 1)
            if outage_pass:
                # A code with an expiry date is ended by its expiry (see
                # _process_pending_codes); only codes without one need a time limit.
                keep_pending = expires_at is not None or not self._auto_pending_too_old(code_row)
            else:
                keep_pending = bool(transient) and passes < self.MAX_AUTO_TRANSIENT_PASSES
            keep_pending = keep_pending and not globally_rejected
            if keep_pending:
                # Temporary failures are not "settled", so the next pass retries exactly
                # those players (and anyone not reached); don't complete the code yet.
                self._auto_transient_passes[job.key] = passes
                self._auto_retry_after[job.key] = time.monotonic() + self.AUTO_RETRY_AFTER_TRANSIENT_SECONDS
                logger.warning(
                    "Auto-redemption of '%s' pass %s/%s ended with %s temporary failures and %s players "
                    "not reached; it stays pending and retries in %s s",
                    code,
                    passes,
                    self.MAX_AUTO_TRANSIENT_PASSES,
                    len(transient),
                    unreached,
                    self.AUTO_RETRY_AFTER_TRANSIENT_SECONDS,
                )
                await self._announce_auto_redemption(code, results, final=False, expires_at=expires_at)
                return False
            if outage_pass and not globally_rejected:
                logger.error(
                    "Giving up on gift code '%s': %s players were never reached after %s hours of "
                    "temporary failures",
                    code,
                    unreached,
                    self.MAX_AUTO_PENDING_HOURS,
                )
            counts = count_statuses(results)
            logger.info(
                "Auto-redemption of '%s' completed: %s",
                code,
                ", ".join(f"{status}={counts[status]}" for status in STATUS_ORDER),
            )
            # Announce before marking the code completed: if the bot stops in between,
            # the code is still pending, so the next start (which skips every recorded
            # player) writes the final summary instead of leaving "Redeeming…" forever.
            await self._announce_auto_redemption(code, results, final=True, expires_at=expires_at)
            await self._gift_code_service.mark_gift_code_completed(code_row.id)
            self._auto_retry_after.pop(job.key, None)
            self._auto_transient_passes.pop(job.key, None)
            await self._disable_broken_accounts(code, targets)
            return True
        finally:
            if not attached:
                self._jobs.release(job)

    def _auto_progress(self, gift_code: str, targets: Sequence[Any], expires_at: Any, job: RedemptionJob):
        """Progress callback of an automatic run: logs, and keeps a live announcement current.

        The announcement appears as soon as the code has worked for someone (so it is
        a real code) and enough of the run is left; it is then edited about once a
        minute and finally replaced by the summary.
        """
        total = len(targets)
        state: dict[str, Any] = {"logged": 0, "announced_at": None, "guilds": None}

        async def progress(results: list[dict], phase: str) -> None:
            processed = len(results)
            if processed - state["logged"] >= 50 or (processed == total and state["logged"] != total):
                state["logged"] = processed
                logger.info("Auto-redemption '%s': %s/%s players processed (%s)", gift_code, processed, total, phase)
            remaining_seconds = (total - processed) * MIN_REQUEST_INTERVAL_SECONDS
            if remaining_seconds < self.LIVE_ANNOUNCE_MIN_REMAINING_SECONDS or not has_success(results):
                return
            now = time.monotonic()
            announced_at = state["announced_at"]
            if announced_at is not None and now - announced_at < self.LIVE_ANNOUNCE_INTERVAL_SECONDS:
                return
            state["announced_at"] = now
            try:
                if state["guilds"] is None:
                    state["guilds"] = await self._player_registry_service.get_enabled_guilds_for_players(
                        [str(player.player_id) for player in targets]
                    )
                await self._announce_auto_progress(
                    gift_code, targets, list(results), state["guilds"], expires_at=expires_at, started_at=job.started_at
                )
            except Exception:
                logger.exception("Could not update the live announcement for '%s'", gift_code)

        return progress

    async def _disable_broken_accounts(self, gift_code: str, targets: Sequence[Any]) -> list[Any]:
        """Pause accounts that failed as invalid on the latest codes and tell their servers."""
        if not callable(getattr(self._gift_code_service, "get_recent_gift_codes", None)):
            return []
        try:
            failing = await self._redemption.find_broken_players(
                targets,
                current_code=gift_code,
                code_limit=self.AUTO_DISABLE_CODES,
                min_failures=self.AUTO_DISABLE_MIN_FAILURES,
            )
            if not failing:
                return []
            player_ids = [player.player_id for player in failing]
            guilds_by_player = await self._player_registry_service.get_enabled_guilds_for_players(player_ids)
            entries, profiles = await self._player_registry_service.disable_broken_players(player_ids)
            logger.warning(
                "Paused %s account(s) after repeated invalid-account failures (%s roster entries, "
                "%s unassigned profiles): %s",
                len(failing),
                entries,
                profiles,
                ", ".join(player_ids),
            )
        except Exception:
            logger.exception("Could not check for repeatedly failing accounts after '%s'", gift_code)
            return []
        try:
            await self._notify_disabled_accounts(failing, guilds_by_player)
        except Exception:
            logger.exception("Could not report the paused accounts after '%s'", gift_code)
        return failing

    def _build_disabled_accounts_embed(self, failing: Sequence[Any], *, scope: str) -> discord.Embed:
        lines = [
            f"{discord.utils.escape_markdown(str(player.player_name or 'Unnamed player'))[:60]} · "
            f"`{player.player_id}` — {ACCOUNT_ERROR_HINTS.get(player.error_code, 'invalid account')} "
            f"({player.failed_codes} codes)"
            for player in failing
        ]
        embed = discord.Embed(
            title=f"⏸️ Paused {len(failing)} Account(s) That Keep Failing",
            description=(
                f"These accounts {scope} failed as invalid on the latest gift codes and never received one, "
                "so the bot stopped redeeming for them:\n\n" + bounded_join(lines, limit=3000)
            ),
            color=EmbedColors.WARNING,
        )
        embed.add_field(
            name="How to fix",
            value=(
                "Check the Governor ID in game (a kingdom transfer or a typo are the usual causes). "
                "Replace a wrong ID with `/removeplayer` and `/addplayer`, or switch the account back on "
                "in `/listplayers` once it works."
            ),
            inline=False,
        )
        return embed

    async def _notify_disabled_accounts(self, failing: Sequence[Any], guilds_by_player: dict[str, set[int]]) -> None:
        """Tell each server's gift-code channel about its paused accounts, and DM the operators."""
        if self._guild_configuration_service is not None:
            for settings in await self._guild_configuration_service.list_all():
                channel = self._guild_gift_channel(settings)
                mine = [
                    player for player in failing
                    if settings.guild_id in guilds_by_player.get(str(player.player_id), ())
                ]
                if channel is None or not mine:
                    continue
                try:
                    await channel.send(embed=self._build_disabled_accounts_embed(mine, scope="on this server's list"))
                except discord.HTTPException:
                    logger.warning("Could not report paused accounts to guild %s", settings.guild_id, exc_info=True)
        embed = self._build_disabled_accounts_embed(failing, scope="(all servers and unassigned profiles)")
        for admin_id in sorted(self._access.super_admin_ids):
            try:
                get_user = getattr(self._bot, "get_user", None)
                user = get_user(admin_id) if callable(get_user) else None
                if user is None:
                    fetch_user = getattr(self._bot, "fetch_user", None)
                    if not callable(fetch_user):
                        continue
                    user = await fetch_user(admin_id)
                await user.send(embed=embed)
            except discord.HTTPException:
                logger.info("Could not DM operator %s about paused accounts", admin_id)

    # -------------------------------------------------------- announcements

    @staticmethod
    def _code_field(gift_code: str, expires_at: Any = None) -> str:
        """The code as copyable text, with its expiry when one is known and in the future."""
        value = f"`{gift_code}`"
        if isinstance(expires_at, datetime):
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at > datetime.now(timezone.utc):
                value += f" · expires <t:{int(expires_at.timestamp())}:R>"
        return value

    def _build_auto_redemption_embed(
        self,
        gift_code: str,
        total_players: int,
        results: list[dict[str, Any]],
        *,
        scope: str = "every registered player",
        final: bool = True,
        expires_at: Any = None,
        note: str | None = None,
    ) -> discord.Embed:
        global_code_error = next((result for result in results if is_global_code_rejection(result)), None)
        retrying = 0 if final else sum(1 for result in results if is_retryable_result(result))
        if global_code_error is None and retrying:
            title = "🎁 New Gift Code Redeemed!"
            description = (
                f"Auto-redemption reached {scope}. {retrying} player(s) hit temporary gift-code server "
                "errors and are retried automatically; this message is updated when it finishes."
            )
            color = EmbedColors.SUCCESS
        elif global_code_error is not None:
            title = "⚠️ Gift Code Partly Redeemed"
            description = (
                f"Auto-redemption reached {scope} until the code stopped working: "
                f"{global_code_error.get('message') or 'the gift code was rejected.'}"
            )
            color = EmbedColors.WARNING
        elif not final:
            # The pass ended early (temporary gift-code server errors); players without
            # a result are still to come, so nothing here may say "finished".
            title = "🎁 New Gift Code Redeemed!" if has_success(results) else "🎁 New Gift Code — Redeeming…"
            description = (
                f"Auto-redemption for {scope} was interrupted by temporary gift-code server errors. "
                "It continues automatically; this message is updated when it finishes."
            )
            color = EmbedColors.SUCCESS if has_success(results) else EmbedColors.INFO
        elif not has_success(results):
            title = "ℹ️ Gift Code Finished Without New Rewards"
            description = f"Auto-redemption finished for {scope}; nobody received a new reward."
            color = EmbedColors.INFO
        else:
            title = "🎁 New Gift Code Redeemed!"
            description = f"Auto-redemption finished for {scope}."
            color = EmbedColors.SUCCESS
        if note:
            description = f"{description}\n{note}"
        embed = discord.Embed(title=title, description=description[:4096], color=color)
        embed.add_field(name="Gift Code", value=self._code_field(gift_code, expires_at), inline=False)
        embed.add_field(
            name="Results",
            value=f"{format_status_counts(results)}\n**👥 Total players:** {total_players}",
            inline=False,
        )
        embed.set_footer(text="Check in-game mail for successfully redeemed codes!")
        return embed

    def _build_auto_progress_embed(
        self,
        gift_code: str,
        total_players: int,
        results: list[dict[str, Any]],
        *,
        scope: str,
        finish: int | None,
        expires_at: Any = None,
    ) -> discord.Embed:
        """The announcement shown while an automatic run is still going."""
        lines = [f"Redeeming for {scope}: **{len(results)}/{total_players}** players processed."]
        if finish is not None:
            lines.append(f"The whole run should finish <t:{finish}:R>.")
        embed = discord.Embed(
            title="🎁 New Gift Code — Redeeming…", description="\n".join(lines), color=EmbedColors.INFO
        )
        embed.add_field(name="Gift Code", value=self._code_field(gift_code, expires_at), inline=False)
        embed.add_field(
            name="Results so far",
            value=format_status_counts(results, hide_zero=True) or "Waiting for this roster's turn.",
            inline=False,
        )
        embed.add_field(name="Don't want to wait?", value=REDEEM_HOW_TO, inline=False)
        embed.set_footer(text="This message is updated during the run and becomes the summary.")
        return embed

    def _guild_gift_channel(self, settings: Any) -> Any:
        """The server's configured gift-code channel, or None when it is unusable."""
        channel_id = getattr(settings, "gift_code_channel_id", None)
        if not channel_id:
            return None
        channel = self._bot.get_channel(channel_id)
        channel_guild_id = getattr(getattr(channel, "guild", None), "id", None)
        if channel is None or channel_guild_id != settings.guild_id or not hasattr(channel, "send"):
            logger.warning("Gift-code channel %s of guild %s is unavailable", channel_id, settings.guild_id)
            return None
        return channel

    async def _guild_gift_settings(self) -> list[Any]:
        if self._guild_configuration_service is None:
            return []
        return [
            settings
            for settings in await self._guild_configuration_service.list_all()
            if getattr(settings, "gift_code_channel_id", None)
        ]

    def _legacy_announcement_channels(self, guild_settings: Sequence[Any]) -> list[tuple[int, Any]]:
        """``AUTO_REDEEM_CHANNELS`` that are not also a server's own gift-code channel.

        A channel that a server picked in ``/configure`` gets that server's summary;
        posting the global one there too would make the two overwrite each other.
        """
        configured = {int(settings.gift_code_channel_id) for settings in guild_settings}
        channels: list[tuple[int, Any]] = []
        for channel_id in getattr(self._config, "auto_redeem_channels", None) or ():
            if int(channel_id) in configured:
                continue
            channel = self._bot.get_channel(channel_id)
            if channel and isinstance(channel, discord.TextChannel):
                channels.append((channel_id, channel))
            else:
                logger.warning("Configured auto-redeem channel %s not found or is not a text channel", channel_id)
        return channels

    async def _announce_auto_progress(
        self,
        gift_code: str,
        targets: Sequence[Any],
        results: list[dict[str, Any]],
        guilds_by_player: dict[str, set[int]],
        *,
        expires_at: Any = None,
        started_at: float | None = None,
    ) -> None:
        """Post or refresh the "redeeming now" announcement in every announcement channel."""
        finish = self._estimate_finish(len(targets), results, started_at)
        guild_settings = await self._guild_gift_settings()
        for channel_id, channel in self._legacy_announcement_channels(guild_settings):
            embed = self._build_auto_progress_embed(
                gift_code, len(targets), results, scope="every registered player", finish=finish,
                expires_at=expires_at,
            )
            try:
                await self._post_or_update_announcement(gift_code, channel_id, channel, embed)
            except Exception as e:
                logger.error("Failed to post live gift code announcement to channel %s: %s", channel_id, e)
        for settings in guild_settings:
            guild_targets = sum(
                1 for player in targets if settings.guild_id in guilds_by_player.get(str(player.player_id), ())
            )
            if not guild_targets:
                continue
            channel = self._guild_gift_channel(settings)
            if channel is None:
                continue
            subset = [
                result for result in results
                if settings.guild_id in guilds_by_player.get(str(result.get("player_id")), ())
            ]
            embed = self._build_auto_progress_embed(
                gift_code, guild_targets, subset, scope="this server's roster", finish=finish, expires_at=expires_at
            )
            try:
                await self._post_or_update_announcement(gift_code, settings.gift_code_channel_id, channel, embed)
            except Exception as e:
                logger.error("Failed to post live gift code announcement to guild %s: %s", settings.guild_id, e)

    async def _announce_auto_redemption(
        self,
        gift_code: str,
        results: list[dict[str, Any]],
        *,
        final: bool = True,
        expires_at: Any = None,
        note: str | None = None,
    ) -> None:
        """Global summary for legacy channels, per-server summaries for configured servers.

        A summary is posted where it contains a new success, and wherever a live
        announcement of this code already stands (it must not stay "redeeming"). A
        pass that leaves the code pending (``final=False``) posts the summary right
        away; later passes of the same code edit that message instead of posting
        another one. Message IDs are stored, so this also holds across restarts.
        """
        try:
            await self._send_auto_redemption_announcement(
                gift_code=gift_code,
                total_players=len(results),
                results=results,
                final=final,
                expires_at=expires_at,
                note=note,
            )
        except Exception:
            logger.exception("Could not post the global gift-code announcement for '%s'", gift_code)
        try:
            await self._announce_to_guild_channels(
                gift_code, results, final=final, expires_at=expires_at, note=note
            )
        except Exception:
            logger.exception("Could not post per-server gift-code announcements for '%s'", gift_code)
        finally:
            if final:
                key = CodeJobGuard.key(gift_code)
                self._auto_announcements.pop(key, None)
                self._announcements_loaded.discard(key)

    async def _load_announcements(self, gift_code: str) -> dict[int, Any]:
        """Messages already posted for this code by channel ID (memory, then the database)."""
        key = CodeJobGuard.key(gift_code)
        posted = self._auto_announcements.setdefault(key, {})
        if key in self._announcements_loaded:
            return posted
        self._announcements_loaded.add(key)
        loader = getattr(self._gift_code_service, "get_announcements", None)
        if not callable(loader):
            return posted
        try:
            stored = await loader(gift_code)
        except Exception:
            logger.warning("Could not load stored announcements for '%s'", gift_code, exc_info=True)
            return posted
        for channel_id, message_id in stored.items():
            if channel_id in posted:
                continue
            channel = self._bot.get_channel(channel_id)
            get_partial_message = getattr(channel, "get_partial_message", None)
            if callable(get_partial_message):
                posted[channel_id] = get_partial_message(message_id)
        return posted

    async def _post_or_update_announcement(
        self, gift_code: str, channel_id: int, channel: Any, embed: discord.Embed
    ) -> str:
        """Edit this code's earlier summary in the channel, or post one; returns what happened."""
        posted = await self._load_announcements(gift_code)
        previous = posted.get(channel_id)
        if previous is not None:
            try:
                await previous.edit(embed=embed)
                return "updated"
            except discord.HTTPException:
                # The first summary was deleted (or cannot be edited any more): post the
                # update as a new message rather than losing it.
                posted.pop(channel_id, None)
        message = await channel.send(embed=embed)
        posted[channel_id] = message
        message_id = getattr(message, "id", None)
        saver = getattr(self._gift_code_service, "save_announcement", None)
        if isinstance(message_id, int) and callable(saver):
            try:
                await saver(gift_code, channel_id, message_id)
            except Exception:
                logger.warning("Could not store the announcement message of '%s'", gift_code, exc_info=True)
        return "posted"

    async def _send_auto_redemption_announcement(
        self,
        *,
        gift_code: str,
        total_players: int,
        results: list[dict[str, Any]],
        final: bool = True,
        expires_at: Any = None,
        note: str | None = None,
    ) -> None:
        """Post the global summary to the legacy ``AUTO_REDEEM_CHANNELS``."""
        channel_ids = getattr(self._config, "auto_redeem_channels", None) or set()
        if not channel_ids:
            return
        existing = await self._load_announcements(gift_code)
        if not has_success(results) and not existing:
            return

        embed = self._build_auto_redemption_embed(
            gift_code, total_players, results, final=final, expires_at=expires_at, note=note
        )
        for channel_id, channel in self._legacy_announcement_channels(await self._guild_gift_settings()):
            if not has_success(results) and channel_id not in existing:
                continue
            try:
                action = await self._post_or_update_announcement(gift_code, channel_id, channel, embed)
                logger.info("Announced gift code %s in channel %s (%s)", gift_code, channel_id, action)
            except Exception as e:
                logger.error("Failed to send gift code announcement to channel %s: %s", channel_id, e)

    async def _announce_to_guild_channels(
        self,
        gift_code: str,
        results: list[dict[str, Any]],
        *,
        final: bool = True,
        expires_at: Any = None,
        note: str | None = None,
    ) -> None:
        """Post each configured server a summary of its own roster's results."""
        settings_list = await self._guild_gift_settings()
        if not settings_list:
            return
        existing = await self._load_announcements(gift_code)
        if not has_success(results) and not existing:
            return
        guilds_by_player = await self._player_registry_service.get_enabled_guilds_for_players(
            [str(result["player_id"]) for result in results if result.get("player_id") is not None]
        )
        for settings in settings_list:
            subset = [
                result for result in results
                if settings.guild_id in guilds_by_player.get(str(result.get("player_id")), ())
            ]
            if not has_success(subset) and settings.gift_code_channel_id not in existing:
                continue
            channel = self._guild_gift_channel(settings)
            if channel is None:
                continue
            embed = self._build_auto_redemption_embed(
                gift_code, len(subset), subset, scope="this server's roster", final=final, expires_at=expires_at,
                note=note,
            )
            try:
                action = await self._post_or_update_announcement(
                    gift_code, settings.gift_code_channel_id, channel, embed
                )
                logger.info("Announced gift code %s to guild %s (%s)", gift_code, settings.guild_id, action)
            except Exception as e:
                logger.error("Failed to announce gift code %s to guild %s: %s", gift_code, settings.guild_id, e)

    # ------------------------------------------------------------ /giftcodes

    async def _handle_list_gift_codes_slash(self, interaction: discord.Interaction, *, ephemeral: bool = False):
        """Handle listing available gift codes (privately when opened from the panel)."""
        private = {"ephemeral": True} if ephemeral else {}
        await interaction.response.defer(thinking=True, **private)

        try:
            response = await self._gift_code_service.get_available_gift_codes()

            if not response.get("success"):
                embed = self._build_status_embed(
                    title="❌ Could Not Fetch Gift Codes",
                    description="The gift code list is unavailable right now. Try again in a few minutes.",
                    color=EmbedColors.ERROR,
                )
                embed.add_field(name="Details", value=str(response.get("message", "Unknown error"))[:1024], inline=False)
                await interaction.followup.send(embed=embed, **private)
                return

            codes = [code for code in response.get("data", []) if isinstance(code, dict) and code.get("code")]

            if not codes:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="📋 No Active Gift Codes",
                        description="No currently active gift codes were found.",
                        color=EmbedColors.INFO,
                    ),
                    **private,
                )
                return

            can_redeem = (
                not ephemeral
                and interaction_guild_id(interaction) is not None
                and await self._access.can_manage(interaction)
            )
            view = GiftCodesView(self, codes, interaction.user.id, can_redeem)
            view.message = await interaction.followup.send(
                content=view.build_content(), embed=view.build_embed(), view=view, wait=True, **private
            )

        except Exception as e:
            logger.error(f"Error listing gift codes: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Unexpected Error",
                    description="Could not show the gift codes. Try again in a minute.",
                    color=EmbedColors.ERROR,
                ),
                **private,
            )

    # ------------------------------------------------- panel, my accounts, submit

    async def _reject_banned(self, interaction: discord.Interaction) -> bool:
        """Buttons bypass the command tree's ban check, so panels repeat it; True when rejected."""
        if not self._access.is_banned(interaction.user.id):
            return False
        await send_denied(interaction, "You can't use this bot.")
        return True

    async def _handle_gift_panel(self, interaction: discord.Interaction) -> None:
        """Post the permanent panel; an unset gift-code channel becomes this channel."""
        if not await self._access.require_manager(interaction, action="post the gift-code panel"):
            return
        guild_id = interaction_guild_id(interaction)
        channel = getattr(interaction, "channel", None)
        if guild_id is None or channel is None or not hasattr(channel, "send"):
            await send_denied(interaction, "Use /giftpanel in a server text channel.")
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            await channel.send(embed=build_panel_embed(self._max_self_registered), view=GiftPanelView(self))
        except discord.HTTPException:
            logger.info("Could not post the gift-code panel in channel %s", getattr(channel, "id", None), exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Could Not Post the Panel",
                    description="I need **View Channel**, **Send Messages** and **Embed Links** in this channel.",
                    color=EmbedColors.ERROR,
                ),
                ephemeral=True,
            )
            return
        note = "The panel is posted. Pin it so members can always find it."
        service = self._guild_configuration_service
        try:
            if service is not None and hasattr(service, "update_for_guild"):
                settings = await service.get_settings(guild_id)
                # A thread archives and would silently lose announcements: text channels only.
                if not getattr(settings, "gift_code_channel_id", None) and is_announcement_target(channel):
                    await service.update_for_guild(guild_id, gift_code_channel_id=int(channel.id))
                    note += (
                        f"\nGift-code announcements will be posted in <#{channel.id}> as well "
                        "(no channel was set). Change it in `/configure`."
                    )
        except Exception:
            logger.warning("Could not set the gift-code channel of guild %s from /giftpanel", guild_id, exc_info=True)
        await interaction.followup.send(
            embed=self._build_status_embed(title="✅ Gift-Code Panel Posted", description=note, color=EmbedColors.SUCCESS),
            ephemeral=True,
        )

    async def _handle_my_accounts(self, interaction: discord.Interaction) -> None:
        """Show the caller's own accounts in this server and how the latest codes went."""
        guild_id = interaction_guild_id(interaction)
        if guild_id is None:
            await send_denied(interaction, "Use this in a server: each server keeps its own gift-code list.")
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            entries = await self._player_registry_service.list_owned_entries(guild_id, interaction.user.id)
            embed = await self._build_my_accounts_embed(entries)
        except Exception:
            logger.exception("Could not show the accounts of user %s in guild %s", interaction.user.id, guild_id)
            embed = self._build_status_embed(
                title="❌ Could Not Load Your Accounts",
                description="Try again in a minute.",
                color=EmbedColors.ERROR,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _build_my_accounts_embed(self, entries: Sequence[Any]) -> discord.Embed:
        if not entries:
            return self._build_status_embed(
                title="🎁 My Accounts",
                description=(
                    "You have not added an account in this server yet. Press **Add my ID** on the gift-code "
                    "panel or run `/addplayer` with your Governor ID, and new codes will be redeemed for you "
                    "automatically."
                ),
                color=EmbedColors.INFO,
            )
        shown = list(entries)[: self.MY_ACCOUNTS_LISTED]
        codes = [
            str(code.code) for code in await self._gift_code_service.get_recent_gift_codes(self.MY_ACCOUNTS_CODES)
        ]
        rows = await self._gift_code_service.get_redemption_rows(codes, [str(entry.player_id) for entry in shown])
        outcomes: dict[tuple[str, str], tuple[int, str]] = {}
        for row in rows:
            if row.success:
                outcome = (3, "✅ redeemed")
            elif row.error_code in ALREADY_REDEEMED_ERROR_CODES:
                outcome = (2, "☑️ already claimed")
            else:
                hint = ACCOUNT_ERROR_HINTS.get(str(row.error_code or ""), "not redeemed")
                outcome = (1, f"❌ {hint}")
            key = (str(row.gift_code), str(row.player_id))
            if key not in outcomes or outcome[0] >= outcomes[key][0]:
                outcomes[key] = outcome
        embed = discord.Embed(
            title="🎁 My Accounts",
            description=f"You have {len(entries)} account(s) on this server's gift-code list.",
            color=EmbedColors.INFO,
        )
        for entry in shown:
            active = bool(entry.enabled and entry.profile_enabled)
            details = [f"`{entry.player_id}`"]
            if entry.kingdom:
                details.append(f"Kingdom {discord.utils.escape_markdown(str(entry.kingdom))[:20]}")
            details.append("✅ receiving codes" if active else "⏸️ paused")
            lines = [" · ".join(details)]
            lines.extend(
                f"`{code[:40]}` {outcomes.get((code, str(entry.player_id)), (0, '— no result yet'))[1]}"
                for code in codes
            )
            embed.add_field(
                name=discord.utils.escape_markdown(str(entry.player_name or "Unnamed player"))[:256],
                value="\n".join(lines)[:EMBED_FIELD_LIMIT],
                inline=False,
            )
        footer = "Paused account? Check the Governor ID, then switch it back on in /listplayers."
        if len(entries) > len(shown):
            footer = f"Showing {len(shown)} of {len(entries)} accounts; /listplayers has the rest. " + footer
        embed.set_footer(text=footer)
        return embed

    def _claim_submit_slot(self, user_id: int, guild_id: int | None = None) -> str | None:
        """Rate-limit code submissions; returns a refusal text or None when allowed.

        Besides the short cooldown there are hourly budgets per member, per server and
        overall, so one person (or one server) cannot use up everybody's submissions.
        """
        now = time.monotonic()
        wait = self.SUBMIT_USER_COOLDOWN_SECONDS - (now - self._submit_cooldowns.get(user_id, float("-inf")))
        if wait > 0:
            return f"You just submitted a code. Try again in {int(wait) + 1} seconds."
        self._submit_times = [entry for entry in self._submit_times if now - entry[0] < 3600]
        if sum(1 for _, user, _ in self._submit_times if user == user_id) >= self.SUBMIT_USER_MAX_PER_HOUR:
            return "You have submitted several codes in the last hour. Try again later."
        if (
            guild_id is not None
            and sum(1 for _, _, guild in self._submit_times if guild == guild_id) >= self.SUBMIT_GUILD_MAX_PER_HOUR
        ):
            return "This server submitted many codes in the last hour. Try again later."
        if len(self._submit_times) >= self.SUBMIT_MAX_PER_HOUR:
            return "Too many codes were submitted in the last hour. Try again later."
        self._submit_cooldowns[user_id] = now
        self._submit_times.append((now, user_id, guild_id))
        if len(self._submit_cooldowns) > 1000:
            self._submit_cooldowns = {
                key: stamp for key, stamp in self._submit_cooldowns.items()
                if now - stamp < self.SUBMIT_USER_COOLDOWN_SECONDS
            }
        return None

    async def _handle_submit_code(self, interaction: discord.Interaction, raw_code: str) -> None:
        """Check a member-submitted code with one account; a working code is queued for everyone."""
        guild_id = interaction_guild_id(interaction)
        if guild_id is None:
            await send_denied(interaction, "Use this in a server.")
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        async def reply(title: str, description: str, color: discord.Color) -> None:
            await interaction.followup.send(
                embed=self._build_status_embed(title=title, description=description, color=color), ephemeral=True
            )

        gift_code = (raw_code or "").strip()
        if not GIFT_CODE_PATTERN.fullmatch(gift_code):
            await reply(
                "❌ That Does Not Look Like a Gift Code",
                "Enter the code exactly as published: up to 64 characters, no spaces.",
                EmbedColors.ERROR,
            )
            return
        if self._closing:
            await reply("⏳ Restarting", "The bot is restarting. Try again in a minute.", EmbedColors.WARNING)
            return
        safe_code = gift_code.replace("`", "")
        try:
            known = await self._gift_code_service.get_gift_code(gift_code)
            if known is not None:
                pending = getattr(known, "auto_redeem_status", None) == "pending"
                await reply(
                    "ℹ️ The Bot Already Has This Code",
                    f"`{safe_code}` is "
                    + ("being redeemed for everyone on the list right now." if pending
                       else "already redeemed for everyone on the list.")
                    + " Press **My accounts** (or run `/myaccounts`) to see your result.",
                    EmbedColors.INFO,
                )
                return
            running = self._jobs.get(gift_code)
            if running is not None:
                await interaction.followup.send(embed=self._job_busy_embed(running, guild_id), ephemeral=True)
                return
            refusal = self._claim_submit_slot(interaction.user.id, guild_id)
            if refusal is not None:
                await reply("⏳ Slow Down", refusal, EmbedColors.WARNING)
                return

            await self._tracking_service.track_user(
                session=None,
                user_id=interaction.user.id,
                username=interaction.user.name,
                discriminator=interaction.user.discriminator,
                display_name=interaction.user.display_name,
            )
            targets = await self._player_registry_service.get_auto_redeem_targets(
                self_registered_max=self._max_self_registered
            )
            if not targets:
                await reply(
                    "ℹ️ Nobody to Redeem For",
                    "No account is on a gift-code list yet. Add yours with `/addplayer` first.",
                    EmbedColors.INFO,
                )
                return
            target_ids = {str(player.player_id) for player in targets}
            owned = await self._player_registry_service.list_owned_entries(guild_id, interaction.user.id)
            own_id = next((str(entry.player_id) for entry in owned if str(entry.player_id) in target_ids), None)
            probe_player = next(
                (player for player in targets if str(player.player_id) == own_id), targets[0]
            )

            job = RedemptionJob(gift_code=gift_code, kind=JOB_PROBE, guild_id=guild_id, requester_id=interaction.user.id)
            existing = self._jobs.claim(job)
            if existing is not None:
                await interaction.followup.send(embed=self._job_busy_embed(existing, guild_id), ephemeral=True)
                return
            try:
                result = await self._redemption.probe_code(
                    gift_code=gift_code,
                    player=probe_player,
                    actor_user_id=interaction.user.id,
                    guild_id=guild_id,
                    channel_id=getattr(getattr(interaction, "channel", None), "id", None),
                )
            finally:
                self._jobs.release(job)

            status = result.get("status_category")
            error_code = str(result.get("error_code") or "").upper()
            message = discord.utils.escape_markdown(str(result.get("message") or "The code was rejected."))[:300]
            if is_global_code_rejection(result):
                await reply("❌ The Code Does Not Work", f"`{safe_code}`: {message}", EmbedColors.ERROR)
                return
            if is_retryable_result(result):
                await reply(
                    "⚠️ Could Not Check the Code",
                    "The gift-code server is not answering right now. Try again in a few minutes.",
                    EmbedColors.WARNING,
                )
                return
            if status not in (STATUS_SUCCESS, STATUS_ALREADY_REDEEMED) and error_code not in CODE_VALID_ERROR_CODES:
                await reply(
                    "⚠️ Could Not Check the Code",
                    f"The test account could not use `{safe_code}` ({message}), so the code is not confirmed. "
                    "A server manager can run `/redeem` with it.",
                    EmbedColors.WARNING,
                )
                return

            is_new, _ = await self._gift_code_service.add_submitted_gift_code(gift_code)
            logger.info(
                "User %s submitted gift code '%s' in guild %s (new=%s, probe=%s)",
                interaction.user.id, gift_code, guild_id, is_new, error_code or status,
            )
            self._ensure_pending_worker()
            own_note = ""
            if own_id is not None and str(probe_player.player_id) == own_id and status == STATUS_SUCCESS:
                own_note = " Your account already received it."
            await reply(
                "✅ Code Accepted — Thank You!",
                f"`{safe_code}` works.{own_note} It is now being redeemed for {len(targets)} account(s); "
                "the summary is posted in the gift-code channel.",
                EmbedColors.SUCCESS,
            )
        except Exception:
            logger.exception("Could not process the submitted gift code '%s'", gift_code)
            await reply("❌ Could Not Check the Code", "Something went wrong. Try again in a minute.", EmbedColors.ERROR)

    # ---------------------------------------------------------------- /redeem

    def _job_busy_embed(self, job: RedemptionJob, guild_id: int | None) -> discord.Embed:
        if job.kind == JOB_AUTO:
            description = (
                f"Auto-redemption is already processing `{job.gift_code}` for every registered player, "
                "including this server's roster. Its summary is posted when it finishes."
            )
        elif job.guild_id == guild_id:
            description = (
                f"This server is already redeeming `{job.gift_code}`"
                + (f" (started by <@{job.requester_id}>)" if job.requester_id else "")
                + ". Wait for its summary, or press **Cancel** on its progress card."
            )
        else:
            description = (
                f"Another server is redeeming `{job.gift_code}` right now. Try again when it finishes; "
                "players who already have the code are skipped."
            )
        return self._build_status_embed(
            title="⏳ Redemption Job Already Running", description=description, color=EmbedColors.WARNING
        )

    async def _handle_redeem_gift_code_slash(
        self,
        interaction: discord.Interaction,
        gift_code: str,
        all_servers: bool = False,
    ):
        """Start a background job that redeems one code for this server's roster.

        ``all_servers`` (bot operators) runs the same global target set as the
        automatic run instead.
        """
        try:
            await interaction.response.defer(thinking=True, ephemeral=True)
        except discord.NotFound:
            logger.warning(
                "Could not acknowledge /redeem interaction before it expired "
                "(interaction_id=%s, user_id=%s, created_at=%s)",
                interaction.id,
                interaction.user.id,
                interaction.created_at.isoformat() if interaction.created_at else None,
            )
            return

        if self._closing:
            # Shutdown already stopped the job guard; a job started now would be killed
            # mid-run with its progress card stuck "in progress".
            await interaction.followup.send(
                "The bot is restarting. Run /redeem again in a minute.", ephemeral=True
            )
            return

        if all_servers:
            if not await self._access.require_super_admin(interaction, action="redeem a code for every server"):
                return
        elif not await self._access.require_manager(interaction, action="start a redemption job"):
            return

        guild_id = interaction_guild_id(interaction)
        if guild_id is None and not all_servers:
            await send_denied(interaction, "Use /redeem in a server: it redeems that server's roster.")
            return

        user_info = f"{interaction.user.name} (ID: {interaction.user.id})"
        logger.info(
            "Bulk redeem for code '%s' requested by %s in guild %s (all_servers=%s)",
            gift_code, user_info, guild_id, all_servers,
        )

        channel = interaction.channel
        if channel is None or not hasattr(channel, "send") or not bot_can_post(channel):
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Channel Unavailable",
                    description=(
                        "The bot cannot post the job's progress here. Run /redeem in a text channel where it has "
                        "**View Channel**, **Send Messages** and **Embed Links**."
                    ),
                    color=EmbedColors.ERROR,
                ),
                ephemeral=True,
            )
            return

        gift_code = (gift_code or "").strip()
        if not GIFT_CODE_PATTERN.match(gift_code):
            await interaction.followup.send(
                "Enter the gift code exactly as published: 1–64 characters, without spaces.", ephemeral=True
            )
            return

        async with self._job_start_lock:
            existing = self._jobs.get(gift_code)
            if existing is None and not all_servers:
                existing = self._jobs.manual_job_for_guild(guild_id)
            if existing is not None:
                await interaction.followup.send(embed=self._job_busy_embed(existing, guild_id), ephemeral=True)
                return

            try:
                await self._tracking_service.track_user(
                    session=None,
                    user_id=interaction.user.id,
                    username=interaction.user.name,
                    discriminator=interaction.user.discriminator,
                    display_name=interaction.user.display_name,
                )
                if all_servers:
                    targets = await self._player_registry_service.get_auto_redeem_targets()
                else:
                    targets = await self._player_registry_service.get_guild_redeem_targets(guild_id)
            except Exception as exc:
                logger.error("Could not prepare the manual redemption job: %s", exc, exc_info=True)
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="❌ Redemption Job Not Started",
                        description="The player list could not be loaded. Try again in a minute.",
                        color=EmbedColors.ERROR,
                    ),
                    ephemeral=True,
                )
                return

            if not targets:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="📭 No Enabled Players",
                        description=(
                            "This server's roster has no enabled players. Add Governor IDs with "
                            "`/addplayer`, or enable players in `/listplayers`."
                        ),
                        color=EmbedColors.WARNING,
                    ),
                    ephemeral=True,
                )
                return

            job = RedemptionJob(
                gift_code=gift_code,
                kind=JOB_MANUAL,
                guild_id=guild_id,
                requester_id=interaction.user.id,
                all_servers=all_servers,
                total=len(targets),
            )
            # The auto poller claims codes without this lock, so it may have taken the
            # code during the awaits above: re-check atomically via claim().
            busy = self._jobs.claim(job)
            if busy is not None:
                await interaction.followup.send(embed=self._job_busy_embed(busy, guild_id), ephemeral=True)
                return
            task = asyncio.create_task(
                self._run_manual_redemption_job(
                    gift_code=gift_code,
                    registered_players=targets,
                    actor_user_id=interaction.user.id,
                    guild_id=guild_id,
                    channel=channel,
                    job=job,
                ),
                name="manual-gift-code-redemption",
            )
            self._jobs.attach(job, task)
            task.add_done_callback(self._manual_redemption_finished)

        scope = "every server's enabled players" if all_servers else "this server's enabled players"
        try:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="🎁 Redemption Job Started",
                    description=(
                        f"Started redeeming `{gift_code}` for {len(targets)} of {scope}.\n"
                        "Watch the progress card in this channel; you'll be pinged when it finishes."
                    ),
                    color=EmbedColors.SUCCESS,
                ),
                ephemeral=True,
            )
        except discord.HTTPException:
            logger.warning("Could not acknowledge the started redemption job", exc_info=True)

    @staticmethod
    def _manual_redemption_finished(task: asyncio.Task[None]) -> None:
        """Surface unexpected task failures (the code guard releases itself)."""
        if task.cancelled():
            logger.warning("Manual gift-code redemption task was cancelled")
            return
        error = task.exception()
        if error is not None:
            logger.error(
                "Manual gift-code redemption task crashed: %s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _run_bulk_redemption(
        self,
        *,
        gift_code: str,
        registered_players: list[Any],
        actor_user_id: int,
        guild_id: int | None,
        channel_id: int | None,
        progress=None,
        stop_event: asyncio.Event | None = None,
        use_recorded_outcomes: bool = False,
    ) -> list[dict[str, Any]]:
        """Delegate to ``GiftRedemptionService.run_bulk`` (kept for the job wrappers)."""
        return await self._redemption.run_bulk(
            gift_code=gift_code,
            players=registered_players,
            actor_user_id=actor_user_id,
            guild_id=guild_id,
            channel_id=channel_id,
            progress=progress,
            stop_event=stop_event,
            use_recorded_outcomes=use_recorded_outcomes,
        )

    async def _redeem_with_retries(self, *args, **kwargs) -> Dict:
        return await self._redemption.redeem_with_retries(*args, **kwargs)

    async def _run_manual_redemption_job(
        self,
        *,
        gift_code: str,
        registered_players: list[Any],
        actor_user_id: int,
        guild_id: int | None,
        channel: Any,
        job: RedemptionJob | None = None,
    ) -> None:
        """Update a normal message so long jobs survive interaction token expiry."""
        job = job or RedemptionJob(
            gift_code=gift_code,
            kind=JOB_MANUAL,
            guild_id=guild_id,
            requester_id=actor_user_id,
            total=len(registered_players),
        )
        total = len(registered_players)
        started_at = time.time()
        message = None
        progress_view = RedemptionProgressView(self, job)
        update_lock = asyncio.Lock()
        last_update = 0.0
        last_phase = ""

        async def progress(results: list[dict], phase: str) -> None:
            nonlocal last_update, last_phase
            if message is None:
                return
            async with update_lock:
                now = asyncio.get_running_loop().time()
                if phase == last_phase and now - last_update < self.PROGRESS_UPDATE_INTERVAL_SECONDS:
                    return
                last_update, last_phase = now, phase
                try:
                    await message.edit(embed=self._build_redemption_progress(
                        gift_code, total, results, phase, started_at=started_at, job=job,
                    ))
                except Exception:
                    logger.warning("Could not update redemption progress", exc_info=True)

        try:
            message = await channel.send(
                embed=self._build_redemption_progress(
                    gift_code, total, [], "Checking prior claims", started_at=started_at, job=job,
                ),
                view=progress_view,
            )
            progress_view.message = message
            results = await self._run_bulk_redemption(
                gift_code=gift_code,
                registered_players=registered_players,
                actor_user_id=actor_user_id,
                guild_id=guild_id,
                channel_id=getattr(channel, "id", None),
                progress=progress,
                stop_event=job.stop_event,
            )
            await self._send_redemption_results_to_channel(
                channel=channel,
                requester_user_id=actor_user_id,
                gift_code=gift_code,
                results=results,
                message=message,
                total=total,
                job=job,
            )
        except asyncio.CancelledError:
            await self._mark_progress_interrupted(message, gift_code)
            raise
        except Exception as exc:
            logger.error("Error in manual redemption job: %s", exc, exc_info=True)
            embed = self._build_status_embed(
                title="❌ Redemption Job Failed",
                description=(
                    f"The redemption job for `{gift_code}` stopped unexpectedly. Results so far are saved; "
                    "run `/redeem` again to continue — players who already have the code are skipped."
                ),
                color=EmbedColors.ERROR,
            )
            try:
                edited = False
                if message is not None:
                    try:
                        await message.edit(embed=embed, view=None)
                        edited = True
                    except discord.HTTPException:
                        pass
                if not edited:
                    message = await channel.send(embed=embed)
                await self._send_completion_ping(channel, actor_user_id, gift_code, "redemption job failed", None, message)
            except Exception as response_error:
                logger.error("Could not post the manual redemption failure: %s", response_error, exc_info=True)
        finally:
            progress_view.stop()

    async def _mark_progress_interrupted(self, message: Any, gift_code: str) -> None:
        """Best-effort note on the progress card when the job is cancelled hard."""
        if message is None:
            return
        try:
            await asyncio.wait_for(
                message.edit(
                    embed=self._build_status_embed(
                        title="⏸️ Redemption Stopped",
                        description=(
                            f"The redemption job for `{gift_code}` stopped because the bot is restarting. "
                            "Results so far are saved; run `/redeem` again to finish."
                        ),
                        color=EmbedColors.WARNING,
                    ),
                    view=None,
                ),
                timeout=3,
            )
        except Exception:
            logger.debug("Could not mark the interrupted progress card", exc_info=True)

    @staticmethod
    def _estimate_finish(total: int, results: list[dict], started_at: float | None) -> int | None:
        """Unix time the job should finish, from observed speed and the 2 s request pacing."""
        remaining = total - len(results)
        if remaining <= 0 or started_at is None:
            return None
        attempted = sum(1 for result in results if result.get("redemption_attempted"))
        elapsed = max(0.0, time.time() - started_at)
        per_player = elapsed / attempted if attempted >= 3 else 0.0
        per_player = max(per_player, MIN_REQUEST_INTERVAL_SECONDS)
        return int(time.time() + remaining * per_player)

    def _build_redemption_progress(
        self,
        gift_code: str,
        total: int,
        results: list[dict],
        phase: str,
        *,
        started_at: float | None = None,
        job: RedemptionJob | None = None,
    ) -> discord.Embed:
        processed = len(results)
        lines = [
            f"**Gift code:** `{gift_code}`",
            progress_bar(processed, total),
            f"**Processed:** {processed}/{total} players",
        ]
        finish = self._estimate_finish(total, results, started_at)
        if finish is not None:
            lines.append(f"**Estimated finish:** <t:{finish}:R>")
        if job is not None and job.stop_requested:
            phase = (
                "Stopping — the bot is restarting"
                if job.stop_reason == STOP_SHUTDOWN
                else "Cancelling — finishing the players in progress"
            )
        lines.append(f"**Status:** {phase}")
        embed = discord.Embed(
            title="🎁 Redemption in Progress",
            description="\n".join(lines) + "\n\n" + format_status_counts(results),
            color=EmbedColors.INFO,
        )
        embed.set_footer(text="Cancel stops after the players in progress; results so far are saved.")
        return embed

    async def _send_completion_ping(
        self,
        channel: Any,
        user_id: int,
        gift_code: str,
        headline: str,
        results: list[dict] | None,
        message: Any,
    ) -> None:
        """Send a short new message: Discord does not notify mentions added by edits."""
        summary = ""
        if results is not None:
            counts = count_statuses(results)
            summary = " — " + " · ".join(
                f"{STATUS_LABELS[status]}: {counts[status]}" for status in STATUS_ORDER if counts[status]
            )
        jump_url = getattr(message, "jump_url", None)
        content = f"<@{user_id}> {headline} for `{gift_code}`{summary}" + (f"\n{jump_url}" if jump_url else "")
        try:
            await channel.send(
                content=content[:2000],
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False, users=[discord.Object(id=user_id)], replied_user=False
                ),
            )
        except discord.HTTPException:
            logger.warning("Could not send the redemption completion ping", exc_info=True)

    async def _send_redemption_results_to_channel(
        self,
        channel: Any,
        requester_user_id: int,
        gift_code: str,
        results: List[Dict],
        message: discord.Message | None = None,
        *,
        total: int | None = None,
        job: RedemptionJob | None = None,
    ) -> None:
        """Replace the progress card with a compact summary, then ping the requester."""
        counts = count_statuses(results)
        success_count = counts[STATUS_SUCCESS]
        already_count = counts[STATUS_ALREADY_REDEEMED]
        issues = [result for result in results if result.get("status_category") not in {
            STATUS_SUCCESS, STATUS_ALREADY_REDEEMED,
        }]
        global_error = next((result for result in results if is_global_code_rejection(result)), None)
        stopped = job is not None and job.stop_requested and (total is None or len(results) < total)
        if stopped and job.stop_reason == STOP_SHUTDOWN:
            title, color, headline = "⏸️ Redemption Stopped", EmbedColors.WARNING, "redemption stopped (bot restart)"
        elif stopped:
            title, color, headline = "⏹️ Redemption Cancelled", EmbedColors.WARNING, "redemption cancelled"
        elif global_error is not None:
            title, color, headline = "❌ Gift Code Rejected", EmbedColors.ERROR, "gift code rejected"
        elif results and already_count == len(results):
            title, color, headline = "✅ Everyone Has Already Claimed This Code", EmbedColors.SUCCESS, "redemption finished"
        elif results and success_count == len(results):
            title, color, headline = "✅ All Gift Codes Redeemed Successfully!", EmbedColors.SUCCESS, "redemption finished"
        elif not issues:
            title, color, headline = "✅ Gift Code Redemption Completed", EmbedColors.SUCCESS, "redemption finished"
        elif success_count + already_count:
            title, color, headline = "⚠️ Gift Code Redemption Completed", EmbedColors.WARNING, "redemption finished"
        else:
            title, color, headline = "❌ All Gift Code Redemptions Failed", EmbedColors.ERROR, "redemption failed"
        processed = f"{len(results)}/{total}" if total is not None else str(len(results))
        embed = discord.Embed(
            title=title, color=color,
            description=f"**Gift code:** `{gift_code}`\n**Processed:** {processed} players\n\n" + format_status_counts(results),
        )
        if stopped:
            not_started = (total or len(results)) - len(results)
            if job.stop_reason == STOP_SHUTDOWN:
                note = "The bot is restarting."
            else:
                note = f"Cancelled by <@{job.stopped_by}>." if job.stopped_by else "Cancelled."
            embed.add_field(
                name="Stopped early",
                value=(
                    f"{note} {not_started} player(s) were not processed. Run `/redeem` again to finish; "
                    "players who already have the code are skipped."
                )[:1024],
                inline=False,
            )
        if global_error is not None:
            embed.add_field(name="Code rejected", value=str(global_error.get("message") or "The code is unavailable.")[:1024], inline=False)
        embed.set_footer(text="Check in-game mail for successful claims." + (" Open Details to review failed or skipped players." if issues else ""))
        view = (
            RedemptionResultsView(
                embed, issues, requester_user_id,
                access_policy=self._access, timeout=self.RESULT_DETAILS_TIMEOUT_SECONDS,
                all_servers=bool(job is not None and job.all_servers),
            )
            if issues else None
        )
        if message is not None:
            try:
                await message.edit(embed=embed, view=view)
            except discord.HTTPException:
                message = None
        if message is None:
            message = await channel.send(embed=embed, view=view)
        if view is not None:
            view.message = message
        await self._send_completion_ping(channel, requester_user_id, gift_code, headline, results, message)
        logger.info("Bulk redemption completed: success=%s, already_claimed=%s, issues=%s", success_count, already_count, len(issues))

    # --------------------------------------------------------------- rosters

    def _build_player_lines(self, players: Sequence[Any]) -> List[str]:
        """Format bounded, labeled player rows; missing data stays unavailable."""
        lines = []
        for player in players:
            status = "Enabled" if player.enabled else "Disabled"
            if player.enabled and getattr(player, "profile_enabled", True) is False:
                status = "Enabled · paused by the bot operator"
            name = discord.utils.escape_markdown(str(player.player_name or "Unnamed player")[:80])
            kingdom = getattr(player, "kingdom", None) or "Unavailable"
            castle = getattr(player, "castle_level", None)
            castle = "Unavailable" if castle in (None, "") else castle
            owner = getattr(player, "added_by_user_id", None)
            owner_text = f" · added by <@{owner}>" if owner else ""
            lines.append(
                f"**{name}** · `{player.player_id}` · {status}\nKingdom: {kingdom} · Castle: {castle}{owner_text}"
            )
        return lines

    @staticmethod
    def _normalize_ranked_alliances(payload: Any) -> List[Dict[str, Any]]:
        """Validate and consistently order ranked-alliance API rows."""
        if not isinstance(payload, list):
            return []

        alliances_by_aid: Dict[str, Dict[str, Any]] = {}
        for row in payload:
            if not isinstance(row, dict):
                continue

            aid = str(row.get("aid", "")).strip()
            if not aid.isdigit() or int(aid) <= 0:
                continue
            abbr = row.get("abbr")
            if not isinstance(abbr, str) or len(abbr.strip()) != 3 or not abbr.strip().isalnum():
                abbr = None
            else:
                abbr = abbr.strip()

            power: Optional[int]
            rank: Optional[int]
            try:
                power = int(row["power"]) if row.get("power") is not None else None
            except (TypeError, ValueError):
                power = None
            try:
                rank = int(row["rank"]) if row.get("rank") is not None else None
            except (TypeError, ValueError):
                rank = None

            name: Optional[str] = None
            raw_name = row.get("name")
            if isinstance(raw_name, str):
                printable_name = "".join(
                    " " if unicodedata.category(character) == "Cc" else character
                    for character in raw_name
                )
                normalized_name = " ".join(printable_name.split())
                if normalized_name:
                    name = normalized_name

            member_count: Optional[int] = None
            raw_member_count = row.get("member_count")
            if not isinstance(raw_member_count, bool):
                try:
                    normalized_member_count = int(raw_member_count)
                except (TypeError, ValueError):
                    normalized_member_count = -1
                if 0 <= normalized_member_count <= 10_000:
                    member_count = normalized_member_count

            alliances_by_aid.setdefault(
                aid,
                {
                    "aid": aid,
                    "abbr": abbr,
                    "power": power,
                    "rank": rank,
                    "name": name,
                    "member_count": member_count,
                },
            )

        return sorted(
            alliances_by_aid.values(),
            key=lambda alliance: (
                alliance["power"] is None,
                -(alliance["power"] or 0),
                alliance["rank"] is None,
                alliance["rank"] or 0,
                (alliance["abbr"] or "").casefold(),
                int(alliance["aid"]),
            ),
        )

    def _get_cached_alliances(
        self,
        kid: int,
    ) -> tuple[Optional[List[Dict[str, Any]]], bool, bool]:
        """Return a cached snapshot plus its freshness and resolution state."""
        cached = self._alliance_cache.get(kid)
        if cached is None:
            return None, False, False

        cached_at, alliances, resolution_complete = cached
        is_fresh = time.monotonic() - cached_at < self.ALLIANCE_CACHE_TTL_SECONDS
        return alliances, is_fresh, resolution_complete

    def _cancel_alliance_prewarm(self, user_id: int) -> None:
        """Cancel a user's pending debounce before it starts an API lookup."""
        pending = self._alliance_prewarm_tasks.pop(user_id, None)
        if pending is not None and not pending[1].done():
            pending[1].cancel()

    def _schedule_alliance_prewarm(self, user_id: int, kid: int) -> None:
        """Debounce KID typing and warm only the user's latest kingdom."""
        cached, is_fresh, resolution_complete = self._get_cached_alliances(kid)
        if cached is not None:
            if not resolution_complete or not is_fresh:
                self._start_alliance_completion(kid)
            self._cancel_alliance_prewarm(user_id)
            return

        pending = self._alliance_prewarm_tasks.get(user_id)
        if pending is not None:
            pending_kid, pending_task = pending
            if pending_kid == kid and not pending_task.done():
                return
            if not pending_task.done():
                pending_task.cancel()

        async def prewarm() -> None:
            await asyncio.sleep(self.ALLIANCE_KID_PREWARM_DEBOUNCE_SECONDS)
            cached_after_delay, fresh_after_delay, complete_after_delay = (
                self._get_cached_alliances(kid)
            )
            if cached_after_delay is None:
                self._start_alliance_refresh(kid)
            elif not complete_after_delay or not fresh_after_delay:
                self._start_alliance_completion(kid)

        task = asyncio.create_task(
            prewarm(),
            name=f"alliance-prewarm-{user_id}-{kid}",
        )
        self._alliance_prewarm_tasks[user_id] = (kid, task)

        def cleanup(finished: asyncio.Task[None]) -> None:
            current = self._alliance_prewarm_tasks.get(user_id)
            if current is not None and current[1] is finished:
                self._alliance_prewarm_tasks.pop(user_id, None)

        task.add_done_callback(cleanup)

    def _start_alliance_refresh(
        self,
        kid: int,
    ) -> asyncio.Task[Optional[List[Dict[str, Any]]]]:
        """Return the one in-flight bounded alliance lookup for a kingdom."""
        existing = self._alliance_refresh_tasks.get(kid)
        if existing is not None and not existing.done():
            return existing

        completion = self._alliance_completion_tasks.get(kid)
        if completion is not None and not completion.done():
            return completion

        task = asyncio.create_task(
            self._refresh_alliance_cache(kid, exhaustive=False),
            name=f"alliance-autocomplete-{kid}",
        )
        self._alliance_refresh_tasks[kid] = task
        return task

    def _start_alliance_completion(
        self,
        kid: int,
    ) -> Optional[asyncio.Task[Optional[List[Dict[str, Any]]]]]:
        """Continue one kingdom's retained service batch without duplicate requests."""
        existing = self._alliance_completion_tasks.get(kid)
        if existing is not None and not existing.done():
            return existing

        retry_after = self._alliance_completion_retry_after.get(kid, 0.0)
        if time.monotonic() < retry_after:
            return None

        self._alliance_completion_retry_after.pop(kid, None)
        task = asyncio.create_task(
            self._refresh_alliance_cache(kid, exhaustive=True),
            name=f"alliance-autocomplete-completion-{kid}",
        )
        self._alliance_completion_tasks[kid] = task
        return task

    async def _refresh_alliance_cache(
        self,
        kid: int,
        *,
        exhaustive: bool,
    ) -> Optional[List[Dict[str, Any]]]:
        """Fetch and cache one bounded or exhaustive ranked-alliance snapshot."""
        current_task = asyncio.current_task()
        task_registry = (
            self._alliance_completion_tasks if exhaustive else self._alliance_refresh_tasks
        )
        try:
            if self._alliance_directory_service is not None:
                try:
                    directory_result = await self._alliance_directory_service.get_ranked_alliances(
                        kid,
                        limit=self.ALLIANCE_AUTOCOMPLETE_MAX_CHOICES,
                    )
                    directory_choices = self._normalize_ranked_alliances(
                        directory_result.get("data")
                    )
                    if directory_choices:
                        self._alliance_cache[kid] = (
                            time.monotonic(),
                            directory_choices,
                            True,
                        )
                        self._alliance_completion_retry_after.pop(kid, None)
                        logger.info(
                            "Cached %s named alliance choices for kingdom %s from public directory",
                            len(directory_choices),
                            kid,
                        )
                        return directory_choices
                except MightPulseUnavailableError as exc:
                    logger.warning(
                        "Named alliance directory unavailable for kingdom %s: %s; "
                        "trying KingShot leaderboard",
                        kid,
                        exc,
                    )

            if self._kingshot_data_service is None:
                return None

            result = await self._kingshot_data_service.get_ranked_alliances(
                kid,
                limit=self.ALLIANCE_AUTOCOMPLETE_MAX_CHOICES,
                exhaustive=exhaustive,
            )
            if not result.get("success"):
                logger.warning(
                    "Could not %s ranked alliances for kingdom %s: %s",
                    "finish loading" if exhaustive else "load",
                    kid,
                    result.get("error_message", "KingShot Data API request failed."),
                )
                if exhaustive:
                    self._alliance_completion_retry_after[kid] = (
                        time.monotonic() + self.ALLIANCE_COMPLETION_RETRY_SECONDS
                    )
                return None

            payload = result.get("data")
            if not isinstance(payload, list):
                logger.warning("Ranked alliances for kingdom %s were not a list", kid)
                if exhaustive:
                    self._alliance_completion_retry_after[kid] = (
                        time.monotonic() + self.ALLIANCE_COMPLETION_RETRY_SECONDS
                    )
                return None

            alliances = self._normalize_ranked_alliances(payload)
            resolution_complete = result.get("resolution_complete", True) is True
            if not alliances:
                logger.warning(
                    "Ranked alliance lookup returned no usable choices for kingdom %s; "
                    "leaving the cache empty",
                    kid,
                )
            else:
                self._alliance_cache[kid] = (
                    time.monotonic(),
                    alliances,
                    resolution_complete,
                )
                logger.info(
                    "Cached %s alliance choices for kingdom %s (metadata complete=%s)",
                    len(alliances),
                    kid,
                    resolution_complete,
                )

            if resolution_complete:
                self._alliance_completion_retry_after.pop(kid, None)
            elif not exhaustive:
                self._start_alliance_completion(kid)
            else:
                self._alliance_completion_retry_after[kid] = (
                    time.monotonic() + self.ALLIANCE_COMPLETION_RETRY_SECONDS
                )
            return alliances
        except Exception as exc:
            logger.error(
                "Error loading ranked alliances for kingdom %s: %s",
                kid,
                exc,
                exc_info=True,
            )
            if exhaustive:
                self._alliance_completion_retry_after[kid] = (
                    time.monotonic() + self.ALLIANCE_COMPLETION_RETRY_SECONDS
                )
            return None
        finally:
            if task_registry.get(kid) is current_task:
                task_registry.pop(kid, None)

    async def _get_alliances_for_autocomplete(
        self,
        kid: int,
    ) -> List[Dict[str, Any]]:
        """Serve fresh/stale cache data without exceeding Discord's response window."""
        started_at = time.monotonic()
        cached, is_fresh, resolution_complete = self._get_cached_alliances(kid)
        if cached is not None:
            if not resolution_complete or not is_fresh:
                self._start_alliance_completion(kid)
            return cached

        refresh = self._alliance_completion_tasks.get(kid)
        if refresh is None or refresh.done():
            refresh = self._start_alliance_refresh(kid)
        try:
            alliances = await asyncio.wait_for(
                asyncio.shield(refresh),
                timeout=self.ALLIANCE_AUTOCOMPLETE_WAIT_SECONDS,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - started_at
            logger.warning(
                "Alliance autocomplete timed out for kingdom %s after %.2fs "
                "(budget %.2fs, cache miss, lookup continues=%s)",
                kid,
                elapsed,
                self.ALLIANCE_AUTOCOMPLETE_WAIT_SECONDS,
                not refresh.done(),
            )
            return []

        cached, _, _ = self._get_cached_alliances(kid)
        if cached is not None:
            return cached
        if alliances:
            return alliances

        completion = self._alliance_completion_tasks.get(kid)
        remaining = self.ALLIANCE_AUTOCOMPLETE_WAIT_SECONDS - (
            time.monotonic() - started_at
        )
        if completion is not None and not completion.done() and remaining > 0:
            try:
                await asyncio.wait_for(asyncio.shield(completion), timeout=remaining)
            except asyncio.TimeoutError:
                logger.debug(
                    "Serving %s partial alliance choices for kingdom %s while exhaustive "
                    "resolution continues",
                    len(cached or alliances or []),
                    kid,
                )

        latest, _, _ = self._get_cached_alliances(kid)
        return latest if latest is not None else alliances or []

    async def _get_alliance_autocomplete_choices(
        self,
        kid: Any,
        current: str,
    ) -> List[app_commands.Choice[str]]:
        """Build Discord autocomplete choices for a valid kingdom option."""
        if isinstance(kid, bool):
            return []
        try:
            normalized_kid = int(kid)
        except (TypeError, ValueError):
            return []
        if normalized_kid <= 0:
            return []

        alliances = await self._get_alliances_for_autocomplete(normalized_kid)
        if not alliances:
            # A shared refresh can finish between its timeout and this check.
            cached, _, _ = self._get_cached_alliances(normalized_kid)
            if cached:
                alliances = cached
        if not alliances and self._alliance_lookup_in_flight(normalized_kid):
            # An empty list would read as "this kingdom has no alliances".
            return [
                app_commands.Choice(
                    name="Still loading alliances — type again in a few seconds",
                    value=ALLIANCE_LOADING_VALUE,
                )
            ]
        needle = str(current or "").strip().casefold()
        if needle:
            alliances = [
                alliance
                for alliance in alliances
                if needle in (alliance.get("abbr") or "").casefold()
                or needle in alliance["aid"]
                or (
                    isinstance(alliance.get("name"), str)
                    and needle in alliance["name"].casefold()
                )
            ]

        return [
            app_commands.Choice(
                name=self._format_alliance_choice_name(alliance),
                value=alliance["aid"],
            )
            for alliance in alliances[: self.ALLIANCE_AUTOCOMPLETE_MAX_CHOICES]
        ]

    def _alliance_lookup_in_flight(self, kid: int) -> bool:
        return any(
            task is not None and not task.done()
            for task in (self._alliance_refresh_tasks.get(kid), self._alliance_completion_tasks.get(kid))
        )

    @classmethod
    def _format_alliance_choice_name(cls, alliance: Dict[str, Any]) -> str:
        """Render one safe Discord choice label while preserving its suffix."""
        tag = alliance.get("abbr")
        full_name = alliance.get("name")
        if not isinstance(full_name, str) or not full_name:
            full_name = "Unknown Alliance" if tag else f"Alliance {alliance['aid']}"
        elif not tag:
            full_name = f"{full_name} (AID {alliance['aid']})"

        member_count = alliance.get("member_count")
        count_text = (
            str(member_count)
            if isinstance(member_count, int) and not isinstance(member_count, bool)
            else "?"
        )
        prefix = f"[{tag}] " if tag else ""
        suffix = f" - {count_text} members"
        available_name_length = cls.ALLIANCE_CHOICE_NAME_MAX_LENGTH - len(prefix) - len(suffix)
        if len(full_name) > available_name_length:
            full_name = full_name[: max(1, available_name_length - 1)].rstrip() + "…"

        return f"{prefix}{full_name}{suffix}"

    async def _get_kid_autocomplete_choices(
        self,
        interaction: discord.Interaction,
        current: Any,
    ) -> List[app_commands.Choice[int]]:
        """Echo a positive KID immediately and debounce its cache prewarm."""
        user_id = getattr(getattr(interaction, "user", None), "id", None)
        if isinstance(current, bool):
            if isinstance(user_id, int):
                self._cancel_alliance_prewarm(user_id)
            return []

        try:
            kid = int(current)
        except (TypeError, ValueError):
            if isinstance(user_id, int):
                self._cancel_alliance_prewarm(user_id)
            return []

        if kid <= 0:
            if isinstance(user_id, int):
                self._cancel_alliance_prewarm(user_id)
            return []

        if isinstance(user_id, int):
            self._schedule_alliance_prewarm(user_id, kid)
        return [app_commands.Choice(name=str(kid), value=kid)]

    @staticmethod
    def _find_alliance_selection(
        alliances: List[Dict[str, Any]],
        selection: str,
    ) -> Optional[Dict[str, Any]]:
        """Resolve a hidden autocomplete ID or an exact manually entered tag."""
        value = selection.strip()
        if value.isdigit():
            if int(value) <= 0:
                return None
            matches = [alliance for alliance in alliances if alliance["aid"] == value]
        elif len(value) == 3:
            normalized_tag = value.casefold()
            matches = [
                alliance
                for alliance in alliances
                if isinstance(alliance.get("abbr"), str)
                and alliance["abbr"].casefold() == normalized_tag
            ]
        else:
            return None

        return matches[0] if len(matches) == 1 else None

    @classmethod
    def _extract_alliance_members(cls, payload: Any) -> List[Dict[str, Any]]:
        """Extract alliance member dictionaries from known API response shapes."""
        if isinstance(payload, list):
            return [member for member in payload if isinstance(member, dict)]
        if not isinstance(payload, dict):
            return []

        for key in ("members", "roster", "memberList", "players"):
            value = payload.get(key)
            if isinstance(value, list):
                return [member for member in value if isinstance(member, dict)]
            if isinstance(value, dict):
                nested = cls._extract_alliance_members(value)
                if nested:
                    return nested

        for key in ("alliance", "data", "result", "payload"):
            nested = cls._extract_alliance_members(payload.get(key))
            if nested:
                return nested

        return []

    @staticmethod
    def _extract_member_fid(member: Dict[str, Any]) -> Optional[str]:
        for key in ("fid", "player_fid", "playerFid", "governorId", "governor_id", "playerId"):
            value = member.get(key)
            if value not in (None, "", 0, "0"):
                return str(value)

        player = member.get("player")
        if isinstance(player, dict):
            for key in ("fid", "player_fid", "playerFid", "governorId", "governor_id", "playerId"):
                value = player.get(key)
                if value not in (None, "", 0, "0"):
                    return str(value)
        return None

    @staticmethod
    def _extract_member_uid(member: Dict[str, Any]) -> Optional[str]:
        for key in ("uid", "player_uid", "playerUid", "internalUid", "internal_uid"):
            value = member.get(key)
            if value not in (None, "", 0, "0"):
                return str(value)

        player = member.get("player")
        if isinstance(player, dict):
            for key in ("uid", "player_uid", "playerUid", "internalUid", "internal_uid"):
                value = player.get(key)
                if value not in (None, "", 0, "0"):
                    return str(value)
        return None

    @staticmethod
    def _extract_member_name(member: Dict[str, Any]) -> Optional[str]:
        for key in ("name", "nickname", "nick_name", "playerName", "player_name"):
            value = member.get(key)
            if value:
                return str(value)

        player = member.get("player")
        if isinstance(player, dict):
            for key in ("name", "nickname", "playerName", "player_name"):
                value = player.get(key)
                if value:
                    return str(value)
        return None

    @staticmethod
    def _extract_member_castle_level(member: Dict[str, Any]) -> Optional[str]:
        for key in ("castleLevel", "castle", "stove_lv", "town_center_level", "level", "lv"):
            value = member.get(key)
            if value is not None:
                return str(value)

        player = member.get("player")
        if isinstance(player, dict):
            for key in ("castleLevel", "castle", "stove_lv", "level", "lv"):
                value = player.get(key)
                if value is not None:
                    return str(value)
        return None

    @classmethod
    def _extract_player_payload(cls, payload: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(payload, dict):
            return None

        if any(key in payload for key in ("fid", "uid", "name", "nickname")):
            return payload

        for key in ("player", "data", "result", "payload"):
            nested = cls._extract_player_payload(payload.get(key))
            if nested:
                return nested
        return None

    @staticmethod
    def _extract_profile_fid(profile: Dict[str, Any]) -> Optional[str]:
        for key in ("fid", "player_fid", "playerFid", "governorId", "governor_id", "playerId"):
            value = profile.get(key)
            if value not in (None, "", 0, "0"):
                return str(value)
        return None

    @staticmethod
    def _extract_profile_uid(profile: Dict[str, Any]) -> Optional[str]:
        for key in ("uid", "player_uid", "playerUid", "internalUid", "internal_uid"):
            value = profile.get(key)
            if value not in (None, "", 0, "0"):
                return str(value)
        return None

    @staticmethod
    def _extract_profile_name(profile: Dict[str, Any]) -> Optional[str]:
        for key in ("name", "nickname", "playerName", "player_name"):
            value = profile.get(key)
            if value:
                return str(value)
        return None

    @staticmethod
    def _extract_profile_kingdom(profile: Dict[str, Any]) -> Optional[str]:
        for key in ("kid", "kingdom", "serverid", "server_id"):
            value = profile.get(key)
            if value is not None:
                return str(value)
        return None

    @staticmethod
    def _extract_profile_castle_level(profile: Dict[str, Any]) -> Optional[str]:
        for key in ("castleLevel", "castle", "stove_lv", "level", "lv"):
            value = profile.get(key)
            if value is not None:
                return str(value)
        return None

    @classmethod
    def _extract_alliance_name(cls, payload: Any) -> Optional[str]:
        if not isinstance(payload, dict):
            return None
        for key in ("name", "allianceName", "alliance_name", "tag", "abbr"):
            value = payload.get(key)
            if value:
                return str(value)
        for key in ("alliance", "data", "result", "payload"):
            value = cls._extract_alliance_name(payload.get(key))
            if value:
                return value
        return None

    # ------------------------------------------------------------ /addplayer

    @staticmethod
    def _profile_from_lookup(player_id: str, player_info: Dict[str, Any]) -> Dict[str, Any]:
        raw_uid = player_info.get("playerUid") or player_info.get("uid")
        level = player_info.get("levelRenderedDetailed") or player_info.get("level")
        return {
            "player_id": str(player_info.get("playerId") or player_id),
            "player_uid": str(raw_uid) if raw_uid not in (None, "", 0, "0") else None,
            "player_name": player_info.get("name"),
            "kingdom": str(player_info["kingdom"]) if player_info.get("kingdom") is not None else None,
            "castle_level": str(level) if level is not None else None,
        }

    def _limit_hint(self, limit_kind: Optional[str], max_owned: Optional[int]) -> str:
        if limit_kind == "personal" and max_owned is not None:
            return (
                f"You can add up to {max_owned} players yourself in this server. "
                "Ask a server manager to add more."
            )
        return (
            f"This server's roster is full ({self._max_players_per_guild} players). "
            "Remove players with `/removeplayer` to make room."
        )

    async def _handle_add_player_slash(self, interaction: discord.Interaction, player_ids: str):
        """Add Governor IDs to this server's roster within the server and personal caps."""
        guild_id = interaction_guild_id(interaction)
        if guild_id is None:
            await send_denied(interaction, "Use /addplayer in a server: each server keeps its own gift-code roster.")
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        raw_ids = list(dict.fromkeys(pid.strip() for pid in re.split(r"[,\s]+", player_ids or "") if pid.strip()))
        if not raw_ids:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Invalid Input",
                    description="Enter one or more Governor IDs separated by commas, e.g. `123456, 654321`.",
                    color=EmbedColors.ERROR,
                ),
                ephemeral=True,
            )
            return

        invalid_ids = [pid for pid in raw_ids if not pid.isdigit()]
        candidates = [pid for pid in raw_ids if pid.isdigit()]
        ignored_ids = candidates[MAX_IDS_PER_ADDPLAYER:]
        candidates = candidates[:MAX_IDS_PER_ADDPLAYER]

        try:
            is_manager = await self._access.can_manage(interaction)
            max_total = self._max_players_per_guild
            max_owned = None if is_manager else self._max_self_registered
            if max_owned == 0:
                await send_denied(
                    interaction,
                    "Self-registration is turned off on this bot. Ask a server manager to add your Governor ID.",
                )
                return

            await self._tracking_service.track_user(
                session=None,
                user_id=interaction.user.id,
                username=interaction.user.name,
                discriminator=interaction.user.discriminator,
                display_name=interaction.user.display_name,
            )

            # Skip lookups for players already listed and for players over the cap;
            # the registration transaction below re-checks both.
            existing = await self._player_registry_service.get_guild_registrations(guild_id, candidates)
            total, owned = await self._player_registry_service.count_guild_registrations(
                guild_id, owner_id=interaction.user.id
            )
            capacity = max_total - total
            limit_kind = "server"
            if max_owned is not None and max_owned - owned < capacity:
                capacity, limit_kind = max_owned - owned, "personal"
            capacity = max(0, capacity)
            already_ids = [pid for pid in candidates if pid in existing]
            new_ids = [pid for pid in candidates if pid not in existing]
            to_lookup, over_limit = new_ids[:capacity], new_ids[capacity:]

            profiles: list[Dict[str, Any]] = []
            not_found: list[str] = []
            unchecked: list[str] = []
            for index, pid in enumerate(to_lookup):
                try:
                    player_info = await self._player_info_service.get_player_info(pid)
                except PlayerLookupUnavailable as exc:
                    logger.warning("Player lookup unavailable while adding %s: %s", pid, exc)
                    unchecked = to_lookup[index:]
                    break
                if player_info is None:
                    not_found.append(pid)
                    continue
                profiles.append(self._profile_from_lookup(pid, player_info))

            outcome = RegistrationOutcome(roster_size=total)
            if profiles:
                outcome = await self._player_registry_service.register_players_for_guild(
                    guild_id,
                    profiles,
                    added_by_user_id=interaction.user.id,
                    max_total=max_total,
                    max_owned=max_owned,
                )
                limit_kind = outcome.limit_kind or limit_kind
            names = {profile["player_id"]: profile.get("player_name") for profile in profiles}
            embed = self._build_add_player_embed(
                outcome=outcome,
                names=names,
                already_ids=already_ids + outcome.already_registered,
                over_limit=over_limit + outcome.over_limit,
                limit_hint=self._limit_hint(limit_kind, max_owned),
                not_found=not_found,
                unchecked=unchecked,
                invalid_ids=invalid_ids,
                ignored_ids=ignored_ids,
                owned_after=(owned + len(outcome.added)) if max_owned is not None else None,
                max_owned=max_owned,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(
                "User %s added %s players to guild %s roster", interaction.user.id, len(outcome.added), guild_id
            )
            if outcome.added and is_manager:
                await offer_channel_setup(
                    interaction,
                    setting="gift_code_channel_id",
                    guild_config_service=self._guild_configuration_service,
                    access_policy=self._access,
                )
        except Exception as e:
            logger.error("Error adding players %s: %s", player_ids, e, exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Could Not Add Players",
                    description=(
                        "The players could not be saved. Try again in a minute; if it keeps failing, "
                        "a server manager can check `/status`."
                    ),
                    color=EmbedColors.ERROR,
                ),
                ephemeral=True,
            )

    def _build_add_player_embed(
        self,
        *,
        outcome: RegistrationOutcome,
        names: Dict[str, Optional[str]],
        already_ids: list[str],
        over_limit: list[str],
        limit_hint: str,
        not_found: list[str],
        unchecked: list[str],
        invalid_ids: list[str],
        ignored_ids: list[str],
        owned_after: Optional[int],
        max_owned: Optional[int],
    ) -> discord.Embed:
        problems = bool(over_limit or not_found or unchecked or invalid_ids or ignored_ids)
        if outcome.added:
            title = f"✅ Added {len(outcome.added)} Player(s)"
            color = EmbedColors.WARNING if problems else EmbedColors.SUCCESS
        elif already_ids and not problems:
            title, color = "ℹ️ Already in This Server's Roster", EmbedColors.INFO
        else:
            title, color = "❌ No Players Added", EmbedColors.ERROR
        description = "Added players are enabled for automatic gift-code redemption." if outcome.added else ""
        description += f"\nThis server's roster: {outcome.roster_size}/{self._max_players_per_guild} players."
        if owned_after is not None and max_owned is not None:
            description += f"\nYou have added {owned_after}/{max_owned} players here."
        embed = discord.Embed(title=title, description=description.strip(), color=color)

        def add_list(name: str, values: list[str], hint: str = "", *, labels: bool = False) -> None:
            if not values:
                return
            items = [_player_label(value, names.get(value)) if labels else f"`{value[:40]}`" for value in values]
            prefix = f"{hint}\n" if hint else ""
            embed.add_field(
                name=f"{name} ({len(values)})",
                value=prefix + bounded_join(items, separator=", " if not labels else "\n",
                                            limit=EMBED_LIST_LIMIT),
                inline=False,
            )

        add_list("Added", outcome.added, labels=True)
        add_list("Already in this server's roster", list(dict.fromkeys(already_ids)))
        add_list("Not added — limit reached", over_limit, limit_hint)
        add_list("Not found", not_found, "Check these Governor IDs in game and try again.")
        add_list(
            "Not checked",
            unchecked,
            "The player lookup service is unavailable right now. Try these again in a few minutes.",
        )
        add_list("Invalid", invalid_ids, "Governor IDs contain digits only.")
        add_list("Ignored", ignored_ids, f"Add at most {MAX_IDS_PER_ADDPLAYER} IDs per command.")
        return embed

    # ----------------------------------------------------------- /addalliance

    async def _handle_add_alliance_choice_slash(
        self,
        interaction: discord.Interaction,
        kid: int,
        alliance: str,
    ) -> None:
        """Validate a native autocomplete selection, then import its roster."""
        if not await self._access.require_manager(interaction, action="import alliances"):
            return
        await interaction.response.defer(thinking=True)

        if isinstance(kid, bool) or kid <= 0:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Invalid Kingdom",
                    description="Enter a positive kingdom ID.",
                    color=EmbedColors.ERROR,
                )
            )
            return

        if self._kingshot_data_service is None:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ KingShot Data API Not Configured",
                    description="Alliance discovery requires the KingShot Data API service. Ask the bot operator to configure it.",
                    color=EmbedColors.ERROR,
                )
            )
            return

        selection = alliance.strip() if isinstance(alliance, str) else ""
        if selection in (ALLIANCE_LOADING_VALUE, f"loading:{kid}"):
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="⏳ Alliances Still Loading",
                    description=(
                        f"The alliance list for kingdom `{kid}` was still loading. Run `/addalliance` again in a "
                        "few seconds and choose from the list, or type the exact 3-character tag."
                    ),
                    color=EmbedColors.WARNING,
                )
            )
            return

        cached, is_fresh, resolution_complete = self._get_cached_alliances(kid)
        refresh: Optional[asyncio.Task[Optional[List[Dict[str, Any]]]]] = None
        if cached is None:
            refresh = self._alliance_completion_tasks.get(kid)
            if refresh is None or refresh.done():
                refresh = self._start_alliance_refresh(kid)
            loaded = await refresh
            cached, is_fresh, resolution_complete = self._get_cached_alliances(kid)
            completion = self._alliance_completion_tasks.get(kid)
            if cached is None and completion is not None and not completion.done():
                loaded = await completion
                cached, is_fresh, resolution_complete = self._get_cached_alliances(kid)
            if cached is None:
                if loaded == []:
                    await interaction.followup.send(
                        embed=self._build_status_embed(
                            title="No Alliances Found",
                            description=(
                                f"No alliance leaderboard entries were found in kingdom `{kid}`."
                            ),
                            color=EmbedColors.WARNING,
                        )
                    )
                    return
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="❌ Could Not Inspect Kingdom",
                        description="Could not load alliances from the KingShot Data API. Try again in a minute.",
                        color=EmbedColors.ERROR,
                    )
                )
                return

        alliances = cached
        refresh = self._alliance_completion_tasks.get(kid)
        if not resolution_complete or not is_fresh:
            refresh = self._start_alliance_completion(kid)

        selected = self._find_alliance_selection(alliances, selection)
        if selected is None and refresh is not None:
            refreshed = await refresh
            if refreshed:
                alliances = refreshed
                selected = self._find_alliance_selection(alliances, selection)

        if not alliances:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="No Alliances Found",
                    description=f"No alliance leaderboard entries were found in kingdom `{kid}`.",
                    color=EmbedColors.WARNING,
                )
            )
            return

        if selected is None:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Invalid Alliance",
                    description=(
                        "Choose an alliance from the autocomplete list for this kingdom, "
                        "or enter its exact 3-character tag."
                    ),
                    color=EmbedColors.ERROR,
                )
            )
            return

        aid = str(selected.get("aid", "")).strip()
        if not aid.isdigit() or int(aid) <= 0:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Invalid Alliance",
                    description="The selected alliance does not have a valid internal ID. Pick another entry from the list.",
                    color=EmbedColors.ERROR,
                )
            )
            return

        await self._handle_add_alliance_slash(
            interaction,
            aid=aid,
            kid=kid,
            defer_response=False,
            alliance_label=selected.get("abbr") or selected.get("name") or f"Alliance {aid}",
        )

    async def _handle_add_alliance_slash(
        self,
        interaction: discord.Interaction,
        aid: str,
        kid: int,
        *,
        defer_response: bool = True,
        alliance_label: Optional[str] = None,
    ):
        """Import all fid-bearing alliance members into this server's roster (managers)."""
        if defer_response:
            await interaction.response.defer(thinking=True)

        guild_id = interaction_guild_id(interaction)
        if guild_id is None:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Server Required",
                    description="Run `/addalliance` in the server whose roster should receive the players.",
                    color=EmbedColors.ERROR,
                )
            )
            return

        if not aid or kid <= 0:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Invalid Input",
                    description="`aid` and positive `kid` are required.",
                    color=EmbedColors.ERROR,
                )
            )
            return

        if self._kingshot_data_service is None:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ KingShot Data API Not Configured",
                    description="Alliance import requires the KingShot Data API service. Ask the bot operator to configure it.",
                    color=EmbedColors.ERROR,
                )
            )
            return

        try:
            await self._tracking_service.track_user(
                session=None,
                user_id=interaction.user.id,
                username=interaction.user.name,
                discriminator=interaction.user.discriminator,
                display_name=interaction.user.display_name,
            )

            result = await self._kingshot_data_service.get_alliance(aid=aid, kid=kid)
            if not result.get("success"):
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="❌ Could Not Fetch Alliance",
                        description=str(result.get("error_message") or "KingShot Data API request failed.")[:1000]
                        + "\nTry again in a minute.",
                        color=EmbedColors.ERROR,
                    )
                )
                return

            payload = result.get("data")
            if isinstance(payload, dict) and payload.get("aid") is not None:
                if str(payload["aid"]) != str(aid):
                    await interaction.followup.send(
                        embed=self._build_status_embed(
                            title="❌ Could Not Fetch Alliance",
                            description="The alliance response does not match the selected alliance ID.",
                            color=discord.Color.red(),
                        )
                    )
                    return
            members = self._extract_alliance_members(payload)
            if not members:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="No Alliance Members Found",
                        description="The alliance response did not include a readable member roster.",
                        color=EmbedColors.WARNING,
                    )
                )
                return

            skipped_members: List[str] = []
            seen_fids: set[str] = set()
            cached_uid_count = 0
            resolved_uid_count = 0
            alternate_uid_count = 0
            profiles: List[Dict[str, Any]] = []
            names: Dict[str, Optional[str]] = {}
            member_uids = []
            for member in members:
                member_uid = self._extract_member_uid(member)
                if member_uid and member_uid not in member_uids:
                    member_uids.append(member_uid)

            cached_players_by_uid = await self._player_registry_service.get_registered_players_by_uids(member_uids)
            missing_uids = {
                member_uid
                for member in members
                if (member_uid := self._extract_member_uid(member))
                and not self._extract_member_fid(member)
                and member_uid not in cached_players_by_uid
            }
            alternate_members_by_uid: dict[str, Dict[str, Any]] = {}
            alternate_alliance_name = None
            if missing_uids and self._mightpulse_service is not None:
                try:
                    alternate_roster = await self._mightpulse_service.get_alliance_roster_by_aid(kid, aid)
                    alternate_members_by_uid = alternate_roster["members_by_uid"]
                    alternate_alliance_name = alternate_roster.get("name")
                    missing_uids.difference_update(
                        uid for uid, member in alternate_members_by_uid.items()
                        if self._extract_member_fid(member)
                    )
                except MightPulseUnavailableError as exc:
                    logger.warning("Could not resolve Governor IDs for alliance %s: %s", aid, exc)

            resolved_profiles_by_uid: dict[str, Dict[str, Any]] = {}
            if missing_uids:
                resolve_semaphore = asyncio.Semaphore(5)

                async def resolve_missing_uid(uid: str) -> tuple[str, Optional[Dict[str, Any]]]:
                    async with resolve_semaphore:
                        profile_result = await self._kingshot_data_service.get_player(uid)
                    if not profile_result.get("success"):
                        logger.warning(
                            "Could not resolve alliance member uid %s to fid: %s",
                            uid,
                            profile_result.get("error_message", "KingShot Data API request failed."),
                        )
                        return uid, None
                    profile = self._extract_player_payload(profile_result.get("data"))
                    if profile and self._extract_profile_uid(profile) != uid:
                        logger.warning("Player profile uid did not match requested uid %s", uid)
                        return uid, None
                    return uid, profile

                for uid, profile in await asyncio.gather(
                    *(resolve_missing_uid(uid) for uid in sorted(missing_uids))
                ):
                    if profile:
                        resolved_profiles_by_uid[uid] = profile

            for member in members:
                member_uid = self._extract_member_uid(member)
                fid = self._extract_member_fid(member)
                member_name = self._extract_member_name(member)
                castle_level = self._extract_member_castle_level(member)
                member_kingdom = str(kid)

                if not fid and member_uid:
                    cached_player = cached_players_by_uid.get(member_uid)
                    if cached_player is not None:
                        fid = cached_player.player_id
                        member_name = member_name or cached_player.player_name
                        castle_level = castle_level or cached_player.castle_level
                        member_kingdom = cached_player.kingdom or member_kingdom
                        cached_uid_count += 1
                    else:
                        alternate_member = alternate_members_by_uid.get(member_uid)
                        if alternate_member:
                            fid = self._extract_member_fid(alternate_member)
                            member_name = member_name or self._extract_member_name(alternate_member)
                            castle_level = castle_level or self._extract_member_castle_level(alternate_member)
                            if fid:
                                alternate_uid_count += 1
                        if not fid:
                            profile = resolved_profiles_by_uid.get(member_uid)
                            if profile:
                                fid = self._extract_profile_fid(profile)
                                member_uid = self._extract_profile_uid(profile) or member_uid
                                member_name = member_name or self._extract_profile_name(profile)
                                castle_level = castle_level or self._extract_profile_castle_level(profile)
                                member_kingdom = self._extract_profile_kingdom(profile) or member_kingdom
                                if fid:
                                    resolved_uid_count += 1

                if not fid:
                    skipped_members.append(member_name or str(member_uid or member.get("id") or "unknown"))
                    continue
                if fid in seen_fids:
                    continue
                seen_fids.add(fid)
                names[fid] = member_name
                profiles.append(
                    {
                        "player_id": fid,
                        "player_uid": member_uid,
                        "player_name": member_name,
                        "kingdom": member_kingdom,
                        "castle_level": castle_level,
                    }
                )

            outcome = RegistrationOutcome()
            if profiles:
                outcome = await self._player_registry_service.register_players_for_guild(
                    guild_id,
                    profiles,
                    added_by_user_id=interaction.user.id,
                    max_total=self._max_players_per_guild,
                    enable_existing=True,
                )

            alliance_name = (
                self._extract_alliance_name(payload) or alternate_alliance_name or alliance_label or f"Alliance {aid}"
            )
            safe_name = discord.utils.escape_markdown(str(alliance_name))[:100]
            description = (
                f"Roster imported from **{safe_name}** in kingdom `{kid}`.\n"
                f"This server's roster: {outcome.roster_size}/{self._max_players_per_guild} players."
            )
            if outcome.added:
                title = f"✅ Added {len(outcome.added)} Alliance Member(s)"
                color = EmbedColors.WARNING if outcome.over_limit else EmbedColors.SUCCESS
            elif outcome.already_registered and not outcome.over_limit:
                title, color = "ℹ️ Alliance Already in This Server's Roster", EmbedColors.INFO
            elif not profiles and skipped_members:
                title, color = "⚠️ No Governor IDs Available", EmbedColors.WARNING
                description = (
                    f"The roster for **{safe_name}** in kingdom `{kid}` contains internal IDs, "
                    "but no connected source supplied Governor IDs. Ask a bot operator to enable "
                    "the alliance roster source, or use `/addplayer` with Governor IDs."
                )
            else:
                title, color = "⚠️ No Alliance Members Added", EmbedColors.WARNING
            embed = discord.Embed(
                title=title,
                description=description,
                color=color,
            )
            if outcome.added:
                embed.add_field(
                    name=f"Added ({len(outcome.added)})",
                    value=bounded_join(
                        [_player_label(fid, names.get(fid)) for fid in outcome.added], limit=EMBED_LIST_LIMIT * 2
                    ),
                    inline=False,
                )
            if outcome.already_registered:
                reenabled = f" ({len(outcome.reenabled)} re-enabled)" if outcome.reenabled else ""
                embed.add_field(
                    name=f"Already in this server's roster ({len(outcome.already_registered)})",
                    value=f"Kept as they are{reenabled}.",
                    inline=False,
                )
            if outcome.over_limit:
                embed.add_field(
                    name=f"Not added — server limit ({len(outcome.over_limit)})",
                    value=self._limit_hint("server", None) + "\n"
                    + bounded_join([f"`{fid}`" for fid in outcome.over_limit], separator=", ", limit=EMBED_LIST_LIMIT),
                    inline=False,
                )
            if skipped_members:
                skipped_preview = bounded_join(
                    [f"`{discord.utils.escape_markdown(str(value))[:40]}`" for value in skipped_members],
                    separator=", ",
                    limit=EMBED_LIST_LIMIT,
                )
                embed.add_field(
                    name=f"Skipped ({len(skipped_members)})",
                    value=(
                        "These members could not be mapped to a Governor ID (`fid`), "
                        f"so they cannot be added for gift redemption.\n{skipped_preview}"
                    ),
                    inline=False,
                )

            embed.set_footer(
                text=(
                    f"UID cache hits: {cached_uid_count} | UID API resolves: {resolved_uid_count} | "
                    f"Roster source resolves: {alternate_uid_count} | "
                    "Gift redemption uses Governor ID (fid)"
                )
            )
            await interaction.followup.send(embed=embed)
            if outcome.added:
                await offer_channel_setup(
                    interaction,
                    setting="gift_code_channel_id",
                    guild_config_service=self._guild_configuration_service,
                    access_policy=self._access,
                )

        except Exception as e:
            logger.error("Error adding alliance %s in kingdom %s: %s", aid, kid, e, exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Could Not Add Alliance",
                    description=(
                        "The alliance roster could not be imported. Try again in a minute; if it keeps failing, "
                        "check `/status` for KingShot Data API and database health."
                    ),
                    color=EmbedColors.ERROR,
                )
            )

    # ---------------------------------------------------------- /removeplayer

    async def _handle_remove_player_slash(self, interaction: discord.Interaction, player_id: str):
        """Ask for confirmation, then remove a player from this server's roster only."""
        guild_id = interaction_guild_id(interaction)
        if guild_id is None:
            await send_denied(interaction, "Use /removeplayer in the server whose roster lists the player.")
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        player_id = str(player_id or "").strip()

        try:
            entry = await self._player_registry_service.get_guild_registration(guild_id, player_id)
            if entry is None:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="❌ Player Not Found",
                        description=(
                            f"`{discord.utils.escape_markdown(player_id[:40])}` is not in this server's roster. "
                            "Check the ID in `/listplayers`."
                        ),
                        color=EmbedColors.ERROR,
                    ),
                    ephemeral=True,
                )
                return

            if entry.added_by_user_id != interaction.user.id and not await self._access.can_manage(interaction):
                await send_denied(
                    interaction,
                    "You can only remove players you added. Ask a server manager to remove other players.",
                )
                return

            view = RemovePlayerConfirmView(self, interaction.user.id, guild_id, entry)
            view.message = await interaction.followup.send(
                embed=self._build_status_embed(
                    title="Remove Player?",
                    description=(
                        f"Remove {_player_label(entry.player_id, entry.player_name)} from this server's gift-code "
                        "roster? Other servers' rosters are not affected."
                    ),
                    color=EmbedColors.WARNING,
                ),
                view=view,
                ephemeral=True,
                wait=True,
            )
        except Exception as e:
            logger.error(f"Error removing player {player_id}: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Could Not Remove Player",
                    description="The roster could not be loaded. Try again in a minute.",
                    color=EmbedColors.ERROR,
                ),
                ephemeral=True,
            )

    async def _confirm_remove_player(self, interaction: discord.Interaction, view: RemovePlayerConfirmView) -> None:
        entry = view.entry
        if entry.added_by_user_id != interaction.user.id and not await self._access.require_manager(
            interaction, action="remove players other people added"
        ):
            return
        view.stop()
        outcome = await self._player_registry_service.remove_player_from_guild(view.guild_id, entry.player_id)
        if not outcome.removed:
            embed = self._build_status_embed(
                title="Already Removed",
                description=f"{_player_label(entry.player_id, entry.player_name)} is no longer in this server's roster.",
                color=EmbedColors.INFO,
            )
        else:
            description = f"{_player_label(entry.player_id, entry.player_name)} was removed from this server's roster."
            if outcome.profile_deleted:
                description += "\nNo other server lists this player, so their saved profile was deleted as well."
            embed = self._build_status_embed(title="✅ Player Removed", description=description, color=EmbedColors.SUCCESS)
            logger.info(
                "Player %s removed from guild %s by %s (profile_deleted=%s)",
                entry.player_id, view.guild_id, interaction.user.id, outcome.profile_deleted,
            )
        await interaction.response.edit_message(embed=embed, view=None)

    # ----------------------------------------------------------- /listplayers

    async def _handle_list_players_slash(self, interaction: discord.Interaction):
        """Show this server's roster with per-server enabled flags and actions."""
        guild_id = interaction_guild_id(interaction)
        if guild_id is None:
            await send_denied(interaction, "Use /listplayers in a server: each server keeps its own roster.")
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            roster = await self._player_registry_service.list_guild_roster(guild_id)
            is_operator = self._access.is_super_admin(interaction.user.id)
            can_manage = await self._access.can_manage(interaction)

            if not roster and not is_operator:
                hint = "Add Governor IDs with `/addplayer`"
                if can_manage:
                    hint += " or import an alliance with `/addalliance`"
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="📋 No Players Yet",
                        description=f"This server's gift-code roster is empty. {hint}.",
                        color=EmbedColors.INFO,
                    ),
                    ephemeral=True,
                )
                return

            ordered_players = sorted(roster, key=lambda player: not player.enabled)
            view = PlayerListPaginationView(
                self,
                ordered_players,
                interaction.user.id,
                guild_id=guild_id,
                can_manage=can_manage,
                is_operator=is_operator,
            )
            view.message = await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True, wait=True)

        except Exception as e:
            logger.error(f"Error listing players: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Could Not List Players",
                    description="The roster could not be loaded. Try again in a minute.",
                    color=EmbedColors.ERROR,
                ),
                ephemeral=True,
            )

    async def _toggle_roster_entry(self, interaction: discord.Interaction, view: PlayerListPaginationView) -> None:
        """Enable/disable the selected entry: managers, or owners for their own entries."""
        entry = view.selected_player
        if entry is None:
            await interaction.response.send_message("Select a player first.", ephemeral=True)
            return
        unassigned = view.status == "unassigned"
        if unassigned:
            if not await self._access.require_super_admin(interaction, action="change unassigned profiles"):
                return
        elif getattr(entry, "added_by_user_id", None) != interaction.user.id and not await self._access.require_manager(
            interaction, action="enable or disable players other people added"
        ):
            return

        new_state = not entry.enabled
        if unassigned:
            changed = await self._player_registry_service.set_profile_enabled(entry.player_id, new_state)
        else:
            changed = await self._player_registry_service.set_guild_players_enabled(
                view.guild_id, [entry.player_id], new_state
            ) > 0
        if not changed:
            await interaction.response.send_message(
                "This player is no longer in the roster. Run /listplayers again.", ephemeral=True
            )
            return
        entry.enabled = new_state
        if unassigned:
            entry.profile_enabled = new_state
        view.refresh(keep_selection=True)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def _send_roster_health(self, interaction: discord.Interaction, view: PlayerListPaginationView) -> None:
        if not await self._access.require_manager(interaction, action="view roster health"):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            roster = await self._player_registry_service.list_guild_roster(view.guild_id)
            report = await self._redemption.build_roster_health(roster, code_limit=self.ROSTER_HEALTH_CODES)
        except Exception:
            logger.exception("Could not build the roster health report for guild %s", view.guild_id)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Roster Health Unavailable",
                    description="Redemption history could not be loaded. Try again in a minute.",
                    color=EmbedColors.ERROR,
                ),
                ephemeral=True,
            )
            return
        kwargs: Dict[str, Any] = {"embed": self._build_roster_health_embed(report), "ephemeral": True}
        health_view = None
        if report.failing_players:
            health_view = RosterHealthView(self, interaction.user.id, view.guild_id, report.failing_players)
            kwargs.update(view=health_view, wait=True)
        message = await interaction.followup.send(**kwargs)
        if health_view is not None:
            health_view.message = message

    def _build_roster_health_embed(self, report: RosterHealthReport) -> discord.Embed:
        if report.codes:
            code_lines = [
                f"`{code.gift_code}` — reached **{code.reached}**/{report.enabled_players}"
                + (f" · {code.failed} failed" if code.failed else "")
                + (" · in progress" if code.pending else "")
                for code in report.codes
            ]
            description = (
                f"Last {len(report.codes)} gift codes · {report.enabled_players} enabled players in this roster.\n"
                "Reached = claimed by the bot or already claimed.\n\n"
                + bounded_join(code_lines, limit=3000)
            )
        else:
            description = "No gift codes have been tracked yet. Check back after the next code is redeemed."
        embed = discord.Embed(title="🩺 Roster Health", description=description, color=EmbedColors.INFO)
        if report.failing_players:
            lines = [
                f"{discord.utils.escape_markdown(str(player.player_name or 'Unnamed player'))[:60]} · "
                f"`{player.player_id}` — {player.failed_codes} codes ({STATUS_LABELS[STATUS_INVALID_ID]})"
                for player in report.failing_players
            ]
            embed.add_field(
                name=f"Repeatedly failing as invalid accounts ({len(report.failing_players)})",
                value=bounded_join(lines, limit=EMBED_FIELD_LIMIT),
                inline=False,
            )
            embed.set_footer(text="Disable these to stop spending redemption requests on them in this server.")
        else:
            embed.add_field(
                name="No repeated failures",
                value="No enabled player failed as an invalid account on several recent codes.",
                inline=False,
            )
        return embed

    async def _disable_failing_players(self, interaction: discord.Interaction, view: RosterHealthView) -> None:
        if not await self._access.require_manager(interaction, action="disable players"):
            return
        changed = await self._player_registry_service.set_guild_players_enabled(
            view.guild_id, [player.player_id for player in view.failing], False
        )
        view.disable_button.disabled = True
        view.stop()
        await interaction.response.edit_message(view=view)
        await interaction.followup.send(
            embed=self._build_status_embed(
                title="✅ Players Disabled",
                description=(
                    f"Disabled {changed} player(s) in this server's roster. "
                    "Re-enable them any time in `/listplayers`."
                ),
                color=EmbedColors.SUCCESS,
            ),
            ephemeral=True,
        )

    async def _assign_unassigned_players(self, interaction: discord.Interaction, view: PlayerListPaginationView) -> None:
        """Operators: register enabled legacy profiles to this server (within its cap)."""
        if not await self._access.require_super_admin(interaction, action="assign unassigned profiles"):
            return
        await interaction.response.defer()
        assigned, left = await self._player_registry_service.assign_unassigned_to_guild(
            view.guild_id, max_total=self._max_players_per_guild
        )
        view.roster = sorted(
            await self._player_registry_service.list_guild_roster(view.guild_id), key=lambda player: not player.enabled
        )
        view.unassigned = await self._player_registry_service.list_unassigned_players()
        view.refresh()
        await interaction.edit_original_response(embed=view.build_embed(), view=view)
        note = f"Assigned {assigned} enabled unassigned profile(s) to this server."
        if left:
            note += (
                f" {left} are still unassigned because the roster limit is {self._max_players_per_guild}; "
                "raise MAX_PLAYERS_PER_GUILD or assign them in another server."
            )
        await interaction.followup.send(note, ephemeral=True)
        logger.info("Operator %s assigned %s legacy profiles to guild %s", interaction.user.id, assigned, view.guild_id)

    @staticmethod
    def _build_status_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
        """Build a consistent status embed for command responses."""
        return build_status_embed(title=title, description=description, color=color)
