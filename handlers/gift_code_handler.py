"""Gift codes: the member panel (``/gift``), the managers' ``/giftadmin`` commands, the shared
gift-code list with its kingdom rosters, code polling and the automatic redemption runs.

Every redemption is an automatic run: a new code, a code a member or manager entered,
and a finished code that is run again for accounts added later all go through the
same resumable pipeline and report in the same summary.

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
from io import BytesIO
from typing import Any, Dict, Iterable, List, Optional, Sequence

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config.bot_config import BotConfig
from handlers.gift_panel import AddPlayerModal, GiftPanelView, build_panel_embed
from handlers.permissions import AccessPolicy, interaction_guild_id, send_denied
from handlers.setup_prompts import bot_can_attach, is_announcement_target
from handlers.ui import EmbedColors, OwnedView, build_status_embed, send_ui_error
from services import gift_card_renderer
from services.card_drawing import AMBER, BLUE, GOOD, MUTED
from services.gift_code_service import IGiftCodeService
from services.gift_redemption_service import (
    ALREADY_REDEEMED_ERROR_CODES,
    JOB_AUTO,
    STATUS_ALREADY_REDEEMED,
    STATUS_API_REJECTED,
    STATUS_INVALID_ID,
    STATUS_LABELS,
    STATUS_ORDER,
    STATUS_SKIPPED,
    STATUS_SUCCESS,
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
from services.player_registry_service import PlayerRegistryService, RegistrationOutcome, kingdom_key

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
AUTO_REDEEM_PENDING = "pending"
RERUN_NOTE = "Run again by a server manager for the accounts that did not have this code yet."
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


def _player_label(player_id: str, name: Optional[str]) -> str:
    safe_name = discord.utils.escape_markdown(str(name)[:60]) if name else None
    return f"`{player_id}`" + (f" ({safe_name})" if safe_name else "")


class PlayerSearchModal(discord.ui.Modal, title="Find a player"):
    query = discord.ui.TextInput(label="Player name or ID (empty shows everyone)", required=False, max_length=100)

    def __init__(self, view: "PlayerListPaginationView"):
        super().__init__(timeout=180)
        self.player_view = view
        self.query.default = view.query

    async def on_submit(self, interaction: discord.Interaction):
        view = self.player_view
        if not await view.interaction_check(interaction):
            return
        if view.is_finished():
            await interaction.response.send_message(
                "This player list has expired. Run /giftadmin roster again.", ephemeral=True
            )
            return
        view.query = str(self.query).strip()
        view.current_page = 0
        view.refresh()
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        await send_ui_error(interaction, error)


class PlayerListPaginationView(OwnedView):
    """Search, filter and manage a kingdom's gift-code roster in one message (managers)."""

    PAGE_SIZE = 10

    def __init__(
        self,
        handler: "GiftCodeHandler",
        players: list[Any],
        author_id: int,
        *,
        kingdom: str | None = None,
    ):
        super().__init__(author_id)
        self.handler = handler
        self.roster = list(players)
        # The kingdom whose accounts are listed, whoever added them.
        self.kingdom = kingdom
        self.query = ""
        self.status = "all"
        self.current_page = 0
        self.selected_index = 0
        self.selected_player = None
        self.refresh()

    @property
    def players(self) -> list[Any]:
        return self.roster

    def page_entries(self) -> list[Any]:
        start = self.current_page * self.PAGE_SIZE
        return self.filtered_players[start : start + self.PAGE_SIZE]

    def refresh(self, *, keep_selection: bool = False) -> None:
        query = self.query.casefold()
        self.filtered_players = [
            player for player in self.players
            if (self.status == "all" or bool(player.enabled) == (self.status == "enabled"))
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
        for option in self.status_select.options:
            option.default = option.value == self.status
        self._refresh_actions()

    def _refresh_actions(self) -> None:
        entry = self.selected_player
        self.remove_button.disabled = entry is None
        if entry is None:
            self.toggle_button.label = "Enable / Disable"
            self.toggle_button.style = discord.ButtonStyle.secondary
            self.toggle_button.disabled = True
        else:
            self.toggle_button.label = "Disable" if entry.enabled else "Enable"
            self.toggle_button.style = discord.ButtonStyle.secondary if entry.enabled else discord.ButtonStyle.success
            self.toggle_button.disabled = False

    def build_embed(self) -> discord.Embed:
        source = self.players
        enabled = sum(bool(player.enabled) for player in source)
        lines = self.handler._build_player_lines(self.page_entries())
        title = f"📋 Kingdom {self.kingdom} Gift-Code Roster" if self.kingdom else "📋 Gift-Code Roster"
        description = (
            f"**{len(self.filtered_players)} matches** · {len(source)} total · "
            f"{enabled} enabled · {len(source) - enabled} disabled\n"
            "Every account of this kingdom on the gift-code list, whoever added it."
        )
        description += f"\nFilter: **{self.status.title()}**"
        if self.query:
            description += f" · Search: {discord.utils.escape_markdown(self.query)}"
        embed = discord.Embed(
            title=title, color=EmbedColors.INFO,
            description=description + "\n\n" + (
                "\n".join(lines) or "No players match. Search with an empty box or change the filter."
            ),
        )
        embed.set_footer(
            text=f"Page {self.current_page + 1}/{self.page_count} · Select a player, then Enable/Disable or Remove"
        )
        return embed

    async def drop_player(self, player_id: str) -> None:
        """Take a removed account out of the open roster and redraw it."""
        self.roster = [player for player in self.roster if str(player.player_id) != str(player_id)]
        self.refresh()
        if self.message is None:
            return
        try:
            await self.message.edit(embed=self.build_embed(), view=self)
        except discord.HTTPException:
            logger.debug("Could not redraw the roster after a removal", exc_info=True)

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

    @discord.ui.button(label="Enable / Disable", style=discord.ButtonStyle.secondary, row=3)
    async def toggle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handler._toggle_roster_entry(interaction, self)

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger, row=3)
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = self.selected_player
        if entry is None:
            await interaction.response.send_message("Select a player first.", ephemeral=True)
            return
        await self.handler._request_remove(
            interaction, entry, on_removed=lambda: self.drop_player(str(entry.player_id))
        )

    @discord.ui.button(label="Roster health", style=discord.ButtonStyle.secondary, row=3)
    async def health_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handler._send_roster_health(interaction, self)


class RemovePlayerConfirmView(OwnedView):
    """Private confirm step before an account is taken off the gift-code list."""

    def __init__(self, handler: "GiftCodeHandler", author_id: int, entry: Any, on_removed=None):
        super().__init__(author_id, timeout=120)
        self.handler = handler
        self.entry = entry
        # Awaited after the account is gone, so the panel that asked can redraw itself.
        self.on_removed = on_removed

    @discord.ui.button(label="Remove from the list", style=discord.ButtonStyle.danger)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handler._confirm_remove_player(interaction, self)

    @discord.ui.button(label="Keep", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.response.edit_message(
            embed=build_status_embed(
                title="Nothing Changed",
                description=f"{_player_label(self.entry.player_id, self.entry.player_name)} stays on the gift-code list.",
                color=EmbedColors.INFO,
            ),
            view=None,
        )


class RosterHealthView(OwnedView):
    """Offers to disable players that keep failing as invalid accounts."""

    def __init__(self, handler: "GiftCodeHandler", author_id: int, failing: list[Any]):
        super().__init__(author_id)
        self.handler = handler
        self.failing = failing
        self.disable_button.label = f"Disable these ({len(failing)})"

    @discord.ui.button(label="Disable these", style=discord.ButtonStyle.danger)
    async def disable_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handler._disable_failing_players(interaction, self)


class MyAccountsView(OwnedView):
    """A member's own accounts in one private message: add one, pause or resume it, remove it."""

    # Discord allows 25 options in a menu.
    MENU_LIMIT = 25
    PANEL_TIMEOUT_SECONDS = 600.0

    def __init__(self, handler: "GiftCodeHandler", author_id: int, entries: Sequence[Any]):
        super().__init__(author_id, timeout=self.PANEL_TIMEOUT_SECONDS)
        self.handler = handler
        self.entries = list(entries)
        self.selected_id: str | None = None
        self.refresh()

    @property
    def shown(self) -> list[Any]:
        return self.entries[: self.MENU_LIMIT]

    @property
    def selected(self) -> Any:
        return next((entry for entry in self.shown if str(entry.player_id) == self.selected_id), None)

    def refresh(self) -> None:
        shown = self.shown
        if self.selected is None:
            self.selected_id = str(shown[0].player_id) if shown else None
        # A menu needs at least one option, and one account needs no menu.
        self.remove_item(self.account_select)
        if len(shown) > 1:
            self.account_select.options = [
                discord.SelectOption(
                    label=f"{entry.player_name or 'Unnamed player'} · {entry.player_id}"[:100],
                    value=str(entry.player_id),
                    description="Receiving codes" if entry.enabled else "Paused",
                    default=str(entry.player_id) == self.selected_id,
                )
                for entry in shown
            ]
            self.add_item(self.account_select)
        entry = self.selected
        self.toggle_button.disabled = self.remove_button.disabled = entry is None
        paused = entry is not None and not entry.enabled
        self.toggle_button.label = "Resume" if paused else "Pause"
        self.toggle_button.style = discord.ButtonStyle.success if paused else discord.ButtonStyle.secondary

    async def build_embed(self) -> discord.Embed:
        return await self.handler._build_my_accounts_embed(
            self.entries, selected_id=self.selected_id if len(self.shown) > 1 else None
        )

    async def reload(self) -> None:
        """Read the accounts again and redraw (after an account was added or removed)."""
        self.entries = list(await self.handler._player_registry_service.list_owned_entries(self.author_id))
        self.refresh()
        if self.message is None or self.is_finished():
            return
        try:
            await self.message.edit(embed=await self.build_embed(), view=self)
        except discord.HTTPException:
            logger.debug("Could not redraw the accounts panel", exc_info=True)

    @discord.ui.select(placeholder="Choose an account", row=0)
    async def account_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_id = select.values[0]
        self.refresh()
        await interaction.response.defer()
        await interaction.edit_original_response(embed=await self.build_embed(), view=self)

    @discord.ui.button(label="Add account", style=discord.ButtonStyle.success, row=1)
    async def add_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddPlayerModal(self.handler, after=self.reload))

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handler._toggle_own_account(interaction, self)

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger, row=1)
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        entry = self.selected
        if entry is None:
            await interaction.response.send_message("Choose an account first.", ephemeral=True)
            return
        await self.handler._request_remove(interaction, entry, on_removed=self.reload)


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
    SHUTDOWN_GRACE_SECONDS = 8.0
    AUTO_RETRY_AFTER_CRASH_SECONDS = 10 * 60
    # Passes that end with temporary failures (outage, rate limit) keep the code pending
    # and retry the unsettled players later, up to this many passes per code.
    AUTO_RETRY_AFTER_TRANSIENT_SECONDS = 15 * 60
    MAX_AUTO_TRANSIENT_PASSES = 6
    # A code without an expiry date is abandoned after this long if the gift-code
    # server never lets a pass through.
    MAX_AUTO_PENDING_HOURS = 72
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
    # An entered code is checked with at most this many accounts before it is queued.
    PROBE_ACCOUNTS = 3
    MY_ACCOUNTS_CODES = 3
    MY_ACCOUNTS_LISTED = 10
    # The active codes of the last poll are trusted this long before asking again.
    ACTIVE_CODES_MAX_AGE_SECONDS = 10 * 60.0
    ACTIVE_CODES_LISTED = 20

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
        # One running job per gift code: an automatic run, or the check of an entered code.
        self._jobs = CodeJobGuard()
        self._closing = False
        self._tracked_bot_user_id: int | None = None
        self._auto_retry_after: dict[str, float] = {}
        self._auto_transient_passes: dict[str, int] = {}
        # Codes the upstream list offered at the last successful poll, and when.
        self._active_codes: list[str] = []
        self._active_codes_at: float | None = None
        # Codes to run once more when their current run ends: accounts were added
        # after that run had taken its list of accounts.
        self._reopen_after_run: set[str] = set()
        # A manager's "run it again": the servers that get a fresh summary, by code key.
        self._rerun_guilds: dict[str, set[int]] = {}
        # ... and its first pass also retries players whose earlier failure looked final.
        self._retry_failed: set[str] = set()
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
        then they are cancelled. Runs stay ``pending`` and resume after the restart.
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
    def _max_self_registered(self) -> int:
        value = getattr(self._config, "max_self_registered_players", None)
        return 3 if value is None else int(value)

    def _can_poll(self) -> bool:
        return self._poll_backoff_until is None or datetime.now(timezone.utc) >= self._poll_backoff_until

    def _mark_poll_backoff(self, error: Exception) -> None:
        self._poll_backoff_until = datetime.now(timezone.utc) + self._poll_backoff_delta
        logger.warning("Gift code polling will retry after %s because of networking error: %s", self._poll_backoff_delta, error)

    def register_commands(self):
        """Register ``/gift`` (everyone: the gift-code panel) and ``/giftadmin ...`` (managers).

        Discord hides a command from members per top-level command, not per subcommand,
        so the manager actions live in a group of their own.
        """

        @self._bot.tree.command(
            name="gift",
            description="Gift codes: add your Governor ID, see your accounts and the active codes",
        )
        @app_commands.guild_only()
        async def gift(interaction: discord.Interaction):
            """Open the gift-code panel privately."""
            await self._handle_gift(interaction)

        admin = app_commands.Group(
            name="giftadmin",
            description="Managers: redeem a code, import an alliance, manage the roster, post the panel",
            guild_only=True,
            default_permissions=discord.Permissions(manage_guild=True),
        )

        @admin.command(name="redeem", description="Redeem a gift code for every enabled account on the list")
        @app_commands.describe(gift_code="The gift code, exactly as published (e.g., KINGSHOTXMAS)")
        async def redeem_gift_code(interaction: discord.Interaction, gift_code: str):
            """Check a new code and queue it, or run a known one again for accounts without it."""
            await self._handle_enter_code(interaction, gift_code)

        @admin.command(name="import", description="Import an alliance into the gift-code list")
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

        @admin.command(name="roster", description="Manage the gift-code roster of this server's kingdom")
        @app_commands.describe(kingdom="Bot operators only: the roster of another kingdom")
        async def list_players(
            interaction: discord.Interaction,
            kingdom: Optional[app_commands.Range[int, 1, 99999]] = None,
        ):
            """List the accounts of this server's kingdom."""
            await self._handle_list_players_slash(interaction, kingdom)

        @admin.command(name="panel", description="Post the gift-code panel with buttons in this channel")
        async def gift_panel(interaction: discord.Interaction):
            """Post the permanent gift-code panel."""
            await self._handle_gift_panel(interaction)

        self._bot.tree.add_command(admin)

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
                self._remember_active_codes(response.get("data") or [])
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

    def _remember_active_codes(self, rows: Iterable[Any]) -> None:
        self._active_codes = [str(row["code"]) for row in rows if isinstance(row, dict) and row.get("code")]
        self._active_codes_at = time.monotonic()

    async def _active_gift_codes(self) -> list[str]:
        """Codes that work right now: the last poll's list, or a fresh one when that is old."""
        fetched_at = self._active_codes_at
        if fetched_at is None or time.monotonic() - fetched_at >= self.ACTIVE_CODES_MAX_AGE_SECONDS:
            response = await self._gift_code_service.get_available_gift_codes()
            if response.get("success"):
                self._remember_active_codes(response.get("data") or [])
        return list(self._active_codes)

    async def _reopen_code(self, code_row: Any) -> bool:
        """Queue a finished code for another automatic pass; False when it was not queued."""
        reopen = getattr(self._gift_code_service, "reopen_gift_code", None)
        if not callable(reopen):
            return False
        return bool(await reopen(code_row.id))

    async def _catch_up_new_accounts(self) -> int:
        """Queue the active codes again so accounts added just now get them too.

        A re-run skips every account whose result is stored, so it costs one request
        per new account and code. Returns how many codes were queued; never raises
        (the accounts are added either way).
        """
        if self._closing:
            return 0
        queued = 0
        try:
            for code in await self._active_gift_codes():
                code_row = await self._gift_code_service.get_gift_code(code)
                if code_row is None:
                    # Not recorded yet: the next poll starts it as a new code.
                    continue
                if self._jobs.get(code) is not None:
                    # The running job took its list of accounts before these were added.
                    self._reopen_after_run.add(CodeJobGuard.key(code))
                    queued += 1
                elif getattr(code_row, "auto_redeem_status", None) == AUTO_REDEEM_PENDING:
                    # Waiting for its turn: it reads the list when it starts.
                    queued += 1
                elif await self._reopen_code(code_row):
                    queued += 1
        except Exception:
            logger.exception("Could not queue the active gift codes for newly added accounts")
        if queued:
            self._ensure_pending_worker()
        return queued

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
        # A re-run of a finished code never changed its summaries to "redeeming": they
        # are final already.
        rerun = getattr(code_row, "auto_redeem_completed_at", None) is not None
        try:
            if not rerun and await self._load_announcements(code):
                targets = await self._player_registry_service.get_auto_redeem_targets()
                recorded = await self._redemption.load_recorded_outcomes(code)
                actor_user_id = await self._ensure_bot_user()
                results = [
                    self._redemption.recorded_outcome_result(player, recorded[str(player.player_id)], actor_user_id)
                    for player in targets
                    if str(player.player_id) in recorded
                ]
                await self._announce_auto_redemption(
                    code, results, final=True, players=targets,
                    note="The code expired before the remaining players could be retried.",
                )
        except Exception:
            logger.exception("Could not finalize the announcements of expired code '%s'", code)
        finally:
            self._auto_announcements.pop(key, None)
            self._announcements_loaded.discard(key)
            self._auto_retry_after.pop(key, None)
            self._auto_transient_passes.pop(key, None)
            self._rerun_guilds.pop(key, None)
            self._retry_failed.discard(key)
            self._reopen_after_run.discard(key)

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
        # A code that finished once before is a re-run (for accounts added later, or a
        # manager's "run it again"): it updates the summaries that exist instead of
        # announcing the code as new.
        rerun = getattr(code_row, "auto_redeem_completed_at", None) is not None
        retry_failed = job.key in self._retry_failed
        self._retry_failed.discard(job.key)
        # This run reads the list of accounts now, so it covers everyone added so far.
        self._reopen_after_run.discard(job.key)
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
                    progress=self._auto_progress(code, targets, expires_at, job, live=not rerun),
                    stop_event=job.stop_event,
                    use_recorded_outcomes=not retry_failed,
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
                await self._announce_auto_redemption(
                    code, results, final=False, expires_at=expires_at, players=targets, rerun=rerun
                )
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
            # A quiet re-run that found the code used up or expired leaves the summaries
            # of the original run alone.
            try:
                if not (rerun and globally_rejected and not self._rerun_guilds.get(job.key)):
                    await self._announce_auto_redemption(
                        code, results, final=True, expires_at=expires_at, players=targets, rerun=rerun
                    )
            finally:
                self._rerun_guilds.pop(job.key, None)
            await self._gift_code_service.mark_gift_code_completed(code_row.id)
            self._auto_retry_after.pop(job.key, None)
            self._auto_transient_passes.pop(job.key, None)
            await self._disable_broken_accounts(code, targets)
            if job.key in self._reopen_after_run:
                # Accounts were added while this run was going: the next pass is theirs.
                self._reopen_after_run.discard(job.key)
                await self._reopen_code(code_row)
            return True
        finally:
            if not attached:
                self._jobs.release(job)

    def _auto_progress(
        self, gift_code: str, targets: Sequence[Any], expires_at: Any, job: RedemptionJob, *, live: bool = True
    ):
        """Progress callback of an automatic run: logs, and keeps a live announcement current.

        The announcement appears as soon as the code has worked for someone (so it is
        a real code) and enough of the run is left; it is then edited about once a
        minute and finally replaced by the summary. A re-run (``live=False``) only logs.
        """
        total = len(targets)
        state: dict[str, Any] = {"logged": 0, "announced_at": None}

        async def progress(results: list[dict], phase: str) -> None:
            processed = len(results)
            if processed - state["logged"] >= 50 or (processed == total and state["logged"] != total):
                state["logged"] = processed
                logger.info("Auto-redemption '%s': %s/%s players processed (%s)", gift_code, processed, total, phase)
            if not live:
                return
            remaining_seconds = (total - processed) * MIN_REQUEST_INTERVAL_SECONDS
            if remaining_seconds < self.LIVE_ANNOUNCE_MIN_REMAINING_SECONDS or not has_success(results):
                return
            now = time.monotonic()
            announced_at = state["announced_at"]
            if announced_at is not None and now - announced_at < self.LIVE_ANNOUNCE_INTERVAL_SECONDS:
                return
            state["announced_at"] = now
            try:
                await self._announce_auto_progress(
                    gift_code, targets, list(results), expires_at=expires_at, started_at=job.started_at
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
            disabled = await self._player_registry_service.disable_broken_players(player_ids)
            logger.warning(
                "Paused %s account(s) after repeated invalid-account failures (%s switched off): %s",
                len(failing),
                disabled,
                ", ".join(player_ids),
            )
        except Exception:
            logger.exception("Could not check for repeatedly failing accounts after '%s'", gift_code)
            return []
        try:
            await self._notify_disabled_accounts(failing, self._kingdoms_of(targets))
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
                "The owner replaces a wrong ID (Remove, then Add account) or presses **Resume** once it "
                "works under **My accounts** in `/gift`; managers do the same in `/giftadmin roster`."
            ),
            inline=False,
        )
        return embed

    async def _notify_disabled_accounts(self, failing: Sequence[Any], kingdoms: dict[str, Any]) -> None:
        """Tell each server's gift-code channel about its kingdom's paused accounts, and DM the operators."""
        all_settings: Sequence[Any] = ()
        if self._guild_configuration_service is not None:
            try:
                all_settings = await self._guild_configuration_service.list_all()
            except Exception:
                # The accounts are paused already: the operators must still hear about it.
                logger.exception("Could not load server settings to report paused accounts")
        if all_settings:
            for settings in all_settings:
                channel = self._guild_gift_channel(settings)
                kingdom, scope = self._server_scope(settings)
                mine = [
                    player for player in failing
                    if self._in_scope(kingdom, kingdoms.get(str(player.player_id)))
                ]
                if channel is None or not mine:
                    continue
                try:
                    await channel.send(embed=self._build_disabled_accounts_embed(mine, scope=f"({scope})"))
                except discord.HTTPException:
                    logger.warning("Could not report paused accounts to guild %s", settings.guild_id, exc_info=True)
        embed = self._build_disabled_accounts_embed(failing, scope="(every kingdom)")
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
    def _server_scope(settings: Any) -> tuple[Optional[str], str]:
        """Which accounts a server's summaries cover, and how that is worded.

        A server follows the kingdom chosen in its settings; a server without one
        follows the whole list.
        """
        kingdom = kingdom_key(getattr(settings, "kingdom_id", None))
        return kingdom, (f"kingdom {kingdom}" if kingdom else "every registered player")

    @staticmethod
    def _in_scope(kingdom: Optional[str], player_kingdom: Any) -> bool:
        return kingdom is None or kingdom_key(player_kingdom) == kingdom

    @staticmethod
    def _kingdoms_of(players: Sequence[Any] | None) -> dict[str, Any]:
        """Stored kingdom of each account, by Governor ID."""
        return {str(player.player_id): getattr(player, "kingdom", None) for player in players or ()}

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

    @staticmethod
    def _auto_card_style(results: list[dict[str, Any]], *, final: bool) -> tuple[str, Any]:
        """Title and accent color of the result card, matching the embed's headline."""
        if any(is_global_code_rejection(result) for result in results):
            return "Gift code partly redeemed", AMBER
        if not final:
            retrying = sum(1 for result in results if is_retryable_result(result))
            return (f"Redeeming · retrying {retrying}" if retrying else "Redeeming…"), BLUE
        if not has_success(results):
            return "No new rewards", MUTED
        return "Gift code redeemed", GOOD

    async def _render_auto_card(
        self,
        gift_code: str,
        results: list[dict[str, Any]],
        kingdoms: dict[str, Any] | None,
        *,
        final: bool,
        expires_at: Any = None,
    ) -> bytes | None:
        """The result card as PNG bytes, or None when it cannot be drawn (the text summary stays)."""
        if not results:
            return None
        try:
            title, accent = self._auto_card_style(results, final=final)
            stats = gift_card_renderer.build_redemption_stats(gift_code, results, kingdoms)
            return await asyncio.to_thread(
                gift_card_renderer.render_redemption_card,
                stats,
                title=title,
                accent=accent,
                expires_at=expires_at if isinstance(expires_at, datetime) else None,
            )
        except Exception:
            logger.warning("Could not draw the result card of gift code '%s'", gift_code, exc_info=True)
            return None

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

        A channel that a server picked in ``/settings`` gets that server's summary;
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
        kingdoms = self._kingdoms_of(targets)
        for settings in guild_settings:
            kingdom, scope = self._server_scope(settings)
            guild_targets = sum(1 for player_kingdom in kingdoms.values() if self._in_scope(kingdom, player_kingdom))
            if not guild_targets:
                continue
            channel = self._guild_gift_channel(settings)
            if channel is None:
                continue
            subset = [
                result for result in results
                if self._in_scope(kingdom, kingdoms.get(str(result.get("player_id"))))
            ]
            embed = self._build_auto_progress_embed(
                gift_code, guild_targets, subset, scope=scope, finish=finish, expires_at=expires_at
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
        players: Sequence[Any] | None = None,
        rerun: bool = False,
    ) -> None:
        """Global summary for legacy channels, a summary of its kingdom for each configured server.

        A summary is posted where it contains a new success, and wherever a live
        announcement of this code already stands (it must not stay "redeeming"). A
        pass that leaves the code pending (``final=False``) posts the summary right
        away; later passes of the same code edit that message instead of posting
        another one. Message IDs are stored, so this also holds across restarts.

        ``players`` (the run's targets) supply the stored kingdom of each account for
        the result card's breakdown.

        A ``rerun`` (a finished code run again for accounts added later) only updates
        the summaries that already exist; the servers of a manager who asked for the
        re-run get a fresh one.
        """
        kingdoms = self._kingdoms_of(players)
        try:
            await self._send_auto_redemption_announcement(
                gift_code=gift_code,
                total_players=len(results),
                results=results,
                final=final,
                expires_at=expires_at,
                note=note,
                kingdoms=kingdoms,
                rerun=rerun,
            )
        except Exception:
            logger.exception("Could not post the global gift-code announcement for '%s'", gift_code)
        try:
            await self._announce_to_guild_channels(
                gift_code, results, final=final, expires_at=expires_at, note=note, kingdoms=kingdoms, rerun=rerun
            )
        except Exception:
            logger.exception("Could not post the servers' gift-code announcements for '%s'", gift_code)
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
        self,
        gift_code: str,
        channel_id: int,
        channel: Any,
        embed: discord.Embed,
        card: bytes | None = None,
        *,
        update_only: bool = False,
    ) -> str:
        """Edit this code's earlier summary in the channel, or post one; returns what happened.

        With ``update_only`` nothing new is posted: a channel without an editable
        summary of this code is left alone ("skipped").

        ``card`` is the result picture: it replaces the text "Results" field where the
        bot may attach files. Without one, a picture left by an earlier summary is removed.
        """
        if card is not None and not bot_can_attach(channel):
            card = None
        if card is not None:
            embed = embed.copy()
            for index, embed_field in enumerate(embed.fields):
                if embed_field.name == "Results":
                    embed.remove_field(index)
                    break
            embed.set_image(url=f"attachment://{gift_card_renderer.CARD_FILENAME}")

        def attachments() -> list[discord.File]:
            # A File can be sent once, so each attempt gets its own.
            if card is None:
                return []
            return [discord.File(BytesIO(card), filename=gift_card_renderer.CARD_FILENAME)]

        posted = await self._load_announcements(gift_code)
        previous = posted.get(channel_id)
        if previous is not None:
            try:
                await previous.edit(embed=embed, attachments=attachments())
                return "updated"
            except discord.HTTPException:
                # The first summary was deleted (or cannot be edited any more): post the
                # update as a new message rather than losing it.
                posted.pop(channel_id, None)
        if update_only:
            return "skipped"
        files = attachments()
        message = await (channel.send(embed=embed, files=files) if files else channel.send(embed=embed))
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
        kingdoms: dict[str, Any] | None = None,
        rerun: bool = False,
    ) -> None:
        """Post the global summary to the legacy ``AUTO_REDEEM_CHANNELS``."""
        channel_ids = getattr(self._config, "auto_redeem_channels", None) or set()
        if not channel_ids:
            return
        existing = await self._load_announcements(gift_code)
        announce_new = has_success(results) and not rerun
        if not announce_new and not existing:
            return

        embed = self._build_auto_redemption_embed(
            gift_code, total_players, results, final=final, expires_at=expires_at, note=note
        )
        channels = [
            (channel_id, channel)
            for channel_id, channel in self._legacy_announcement_channels(await self._guild_gift_settings())
            if announce_new or channel_id in existing
        ]
        card = (
            await self._render_auto_card(gift_code, results, kingdoms, final=final, expires_at=expires_at)
            if channels
            else None
        )
        for channel_id, channel in channels:
            try:
                action = await self._post_or_update_announcement(
                    gift_code, channel_id, channel, embed, card, update_only=rerun
                )
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
        kingdoms: dict[str, Any] | None = None,
        rerun: bool = False,
    ) -> None:
        """Post each configured server a summary of its kingdom's results."""
        settings_list = await self._guild_gift_settings()
        if not settings_list:
            return
        existing = await self._load_announcements(gift_code)
        # Servers whose manager asked for this re-run: they get a summary whatever it found.
        fresh_guilds = self._rerun_guilds.get(CodeJobGuard.key(gift_code), set()) if rerun else set()
        announce_new = has_success(results) and not rerun
        if not announce_new and not existing and not fresh_guilds:
            return
        if not kingdoms:
            # A caller without the run's targets: read the accounts' kingdoms from the list.
            entries = await self._player_registry_service.get_entries(
                [str(result["player_id"]) for result in results if result.get("player_id") is not None]
            )
            kingdoms = {player_id: entry.kingdom for player_id, entry in entries.items()}
        for settings in settings_list:
            kingdom, scope = self._server_scope(settings)
            subset = [
                result for result in results
                if self._in_scope(kingdom, kingdoms.get(str(result.get("player_id"))))
            ]
            fresh = settings.guild_id in fresh_guilds
            if (
                not fresh
                and settings.gift_code_channel_id not in existing
                and (rerun or not has_success(subset))
            ):
                continue
            channel = self._guild_gift_channel(settings)
            if channel is None:
                continue
            embed = self._build_auto_redemption_embed(
                gift_code, len(subset), subset, scope=scope, final=final, expires_at=expires_at,
                note=note or (RERUN_NOTE if fresh else None),
            )
            card = await self._render_auto_card(gift_code, subset, kingdoms, final=final, expires_at=expires_at)
            try:
                action = await self._post_or_update_announcement(
                    gift_code, settings.gift_code_channel_id, channel, embed, card,
                    update_only=rerun and not fresh,
                )
                logger.info("Announced gift code %s to guild %s (%s)", gift_code, settings.guild_id, action)
            except Exception as e:
                logger.error("Failed to announce gift code %s to guild %s: %s", gift_code, settings.guild_id, e)

    # --------------------------------------------------------- active codes

    async def _handle_active_codes(self, interaction: discord.Interaction) -> None:
        """Privately list the codes that work right now, as copyable lines."""
        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            response = await self._gift_code_service.get_available_gift_codes()

            if not response.get("success"):
                embed = self._build_status_embed(
                    title="❌ Could Not Fetch Gift Codes",
                    description="The gift code list is unavailable right now. Try again in a few minutes.",
                    color=EmbedColors.ERROR,
                )
                embed.add_field(name="Details", value=str(response.get("message", "Unknown error"))[:1024], inline=False)
                await interaction.followup.send(embed=embed, ephemeral=True)
                return

            codes = [code for code in response.get("data", []) if isinstance(code, dict) and code.get("code")]
            self._remember_active_codes(codes)

            if not codes:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="📋 No Active Gift Codes",
                        description="No currently active gift codes were found.",
                        color=EmbedColors.INFO,
                    ),
                    ephemeral=True,
                )
                return

            shown = codes[: self.ACTIVE_CODES_LISTED]
            await interaction.followup.send(
                # Plain inline-code lines are easy to copy on mobile.
                content="\n".join(f"`{str(code['code']).replace('`', '')[:100]}`" for code in shown),
                embed=self._build_active_codes_embed(shown, total=len(codes)),
                ephemeral=True,
            )

        except Exception as e:
            logger.error(f"Error listing gift codes: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Unexpected Error",
                    description="Could not show the gift codes. Try again in a minute.",
                    color=EmbedColors.ERROR,
                ),
                ephemeral=True,
            )

    @staticmethod
    def _build_active_codes_embed(codes: Sequence[dict], *, total: int) -> discord.Embed:
        embed = discord.Embed(
            title="🎁 Active Gift Codes",
            color=EmbedColors.INFO,
            description=(
                "Accounts on the gift-code list get these automatically; nothing to do.\n" + REDEEM_HOW_TO
            ),
        )
        for code in codes:
            expiry = "Expiration unavailable"
            if code.get("expiresAt"):
                try:
                    dt = datetime.fromisoformat(code["expiresAt"].replace("Z", "+00:00"))
                    expiry = f"Expires <t:{int(dt.timestamp())}:R>"
                except (ValueError, TypeError, AttributeError):
                    pass
            embed.add_field(name=str(code["code"])[:256], value=expiry, inline=False)
        if total > len(codes):
            embed.set_footer(text=f"Showing {len(codes)} of {total} codes")
        return embed

    # ------------------------------------------------------------------ panel

    async def _reject_banned(self, interaction: discord.Interaction) -> bool:
        """Buttons bypass the command tree's ban check, so panels repeat it; True when rejected."""
        if not self._access.is_banned(interaction.user.id):
            return False
        await send_denied(interaction, "You can't use this bot.")
        return True

    async def _handle_gift(self, interaction: discord.Interaction) -> None:
        """``/gift``: the gift-code panel, shown privately to whoever asked."""
        if interaction_guild_id(interaction) is None:
            await send_denied(interaction, "Use /gift in a server.")
            return
        await interaction.response.send_message(
            embed=build_panel_embed(self._max_self_registered), view=GiftPanelView(self), ephemeral=True
        )

    async def post_panel(self, channel: Any) -> None:
        """Post the permanent panel in ``channel``; raises ``discord.HTTPException`` when it cannot."""
        await channel.send(embed=build_panel_embed(self._max_self_registered), view=GiftPanelView(self))

    async def _handle_gift_panel(self, interaction: discord.Interaction) -> None:
        """Post the permanent panel; an unset gift-code channel becomes this channel."""
        if not await self._access.require_manager(interaction, action="post the gift-code panel"):
            return
        guild_id = interaction_guild_id(interaction)
        channel = getattr(interaction, "channel", None)
        if guild_id is None or channel is None or not hasattr(channel, "send"):
            await send_denied(interaction, "Use /giftadmin panel in a server text channel.")
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            await self.post_panel(channel)
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
                        "(no channel was set). Change it in `/settings`."
                    )
        except Exception:
            logger.warning("Could not set the gift-code channel of guild %s from /giftadmin panel", guild_id, exc_info=True)
        await interaction.followup.send(
            embed=self._build_status_embed(title="✅ Gift-Code Panel Posted", description=note, color=EmbedColors.SUCCESS),
            ephemeral=True,
        )

    # ------------------------------------------------------------ my accounts

    async def _handle_my_accounts(self, interaction: discord.Interaction) -> None:
        """The caller's own accounts with their latest results and the controls to manage them."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            entries = await self._player_registry_service.list_owned_entries(interaction.user.id)
            view = MyAccountsView(self, interaction.user.id, entries)
            embed = await view.build_embed()
        except Exception:
            logger.exception("Could not show the accounts of user %s", interaction.user.id)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Could Not Load Your Accounts",
                    description="Try again in a minute.",
                    color=EmbedColors.ERROR,
                ),
                ephemeral=True,
            )
            return
        view.message = await interaction.followup.send(embed=embed, view=view, ephemeral=True, wait=True)

    async def _build_my_accounts_embed(
        self, entries: Sequence[Any], *, selected_id: str | None = None
    ) -> discord.Embed:
        """The accounts with the result of the latest codes; ``selected_id`` marks the chosen one."""
        if not entries:
            return self._build_status_embed(
                title="🎁 My Accounts",
                description=(
                    "You have not added an account yet. Press **Add account** and enter your Governor ID: "
                    "new codes are then redeemed for you automatically, starting with the ones active now."
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
            description=f"You have {len(entries)} account(s) on the gift-code list.",
            color=EmbedColors.INFO,
        )
        for entry in shown:
            active = bool(entry.enabled)
            details = [f"`{entry.player_id}`"]
            if entry.kingdom:
                details.append(f"Kingdom {discord.utils.escape_markdown(str(entry.kingdom))[:20]}")
            details.append("✅ receiving codes" if active else "⏸️ paused")
            lines = [" · ".join(details)]
            lines.extend(
                f"`{code[:40]}` {outcomes.get((code, str(entry.player_id)), (0, '— no result yet'))[1]}"
                for code in codes
            )
            marker = "▸ " if selected_id is not None and str(entry.player_id) == selected_id else ""
            embed.add_field(
                name=(marker + discord.utils.escape_markdown(str(entry.player_name or "Unnamed player")))[:256],
                value="\n".join(lines)[:EMBED_FIELD_LIMIT],
                inline=False,
            )
        footer = "Paused account? Check the Governor ID in game, then press Resume."
        if len(entries) > len(shown):
            footer = (
                f"Showing {len(shown)} of {len(entries)} accounts; managers find the rest in /giftadmin roster. "
                + footer
            )
        embed.set_footer(text=footer)
        return embed

    async def _toggle_own_account(self, interaction: discord.Interaction, view: MyAccountsView) -> None:
        """Pause or resume the account chosen in "My accounts"."""
        entry = view.selected
        if entry is None:
            await interaction.response.send_message("Choose an account first.", ephemeral=True)
            return
        if not await self._may_manage_entry(interaction, entry):
            await send_denied(interaction, self.TOGGLE_DENIED)
            return
        # Acknowledge before the database write (Discord's 3-second deadline).
        await interaction.response.defer()
        if await self._player_registry_service.get_entry(entry.player_id) is None:
            await view.reload()
            await interaction.followup.send("This account is no longer on the gift-code list.", ephemeral=True)
            return
        new_state = not entry.enabled
        await self._player_registry_service.set_players_enabled([entry.player_id], new_state)
        entry.enabled = new_state
        if new_state:
            # A resumed account gets the codes that are active right now.
            await self._catch_up_new_accounts()
        view.refresh()
        await interaction.edit_original_response(embed=await view.build_embed(), view=view)

    # ------------------------------------------------------------ enter a code

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

    def _job_busy_embed(self, job: RedemptionJob) -> discord.Embed:
        if job.kind == JOB_AUTO:
            description = (
                f"`{job.gift_code}` is being redeemed for every account on the list right now. "
                "The summary is posted in the gift-code channel when it finishes."
            )
        else:
            description = f"Somebody else is checking `{job.gift_code}` right now. Try again in a moment."
        return self._build_status_embed(title="⏳ Already in Progress", description=description, color=EmbedColors.WARNING)

    async def _gift_channel_id(self, guild_id: int) -> int | None:
        """The server's gift-code channel, or None when none is set (or it cannot be read)."""
        service = self._guild_configuration_service
        if service is None:
            return None
        try:
            settings = await service.get_settings(guild_id)
        except Exception:
            logger.warning("Could not load the settings of guild %s", guild_id, exc_info=True)
            return None
        channel_id = getattr(settings, "gift_code_channel_id", None)
        return int(channel_id) if channel_id else None

    async def _summary_place(self, guild_id: int) -> str:
        """Where a member of this server sees the summary of a run, as a sentence."""
        channel_id = await self._gift_channel_id(guild_id)
        if channel_id:
            return f"The summary is posted in <#{channel_id}>."
        return (
            "This server has no gift-code channel, so no summary is posted here; a server manager can "
            "pick one in `/settings`. Your own result is under **My accounts**."
        )

    async def _handle_enter_code(self, interaction: discord.Interaction, raw_code: str) -> None:
        """One way to hand the bot a code (the panel's **Submit a code** and ``/giftadmin redeem``).

        A code the bot does not know is checked with up to ``PROBE_ACCOUNTS`` accounts
        and, when it works, queued for everyone. A code it already redeemed is run
        again when a server manager enters it: only accounts without it are tried.
        """
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
            is_manager = await self._access.can_manage(interaction)
            running = self._jobs.get(gift_code)
            if running is not None:
                await interaction.followup.send(embed=self._job_busy_embed(running), ephemeral=True)
                return
            known = await self._gift_code_service.get_gift_code(gift_code)
            if known is not None:
                await self._handle_known_code(interaction, known, guild_id, is_manager, reply)
                return
            if not is_manager:
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
                    "No account is on the gift-code list yet. Press **Add my ID** in `/gift` first.",
                    EmbedColors.INFO,
                )
                return
            # The submitter's own account is tried first: they hear at once that it worked.
            owned_ids = {
                str(entry.player_id) for entry in await self._player_registry_service.list_owned_entries(interaction.user.id)
            }
            probe_players = sorted(targets, key=lambda player: str(player.player_id) not in owned_ids)[
                : self.PROBE_ACCOUNTS
            ]

            if self._closing:
                # Shutdown began during the awaits above; a probe now would use closed clients.
                await reply("⏳ Restarting", "The bot is restarting. Try again in a minute.", EmbedColors.WARNING)
                return
            job = RedemptionJob(gift_code=gift_code, kind=JOB_PROBE, guild_id=guild_id, requester_id=interaction.user.id)
            existing = self._jobs.claim(job)
            if existing is not None:
                await interaction.followup.send(embed=self._job_busy_embed(existing), ephemeral=True)
                return
            try:
                confirmed, probe_player, result = False, probe_players[0], {}
                for probe_player in probe_players:
                    result = await self._redemption.probe_code(
                        gift_code=gift_code,
                        player=probe_player,
                        actor_user_id=interaction.user.id,
                        guild_id=guild_id,
                        channel_id=getattr(getattr(interaction, "channel", None), "id", None),
                    )
                    confirmed = (
                        result.get("status_category") in (STATUS_SUCCESS, STATUS_ALREADY_REDEEMED)
                        or str(result.get("error_code") or "").upper() in CODE_VALID_ERROR_CODES
                    )
                    # A rejected code or an outage says the same for every account.
                    if confirmed or is_global_code_rejection(result) or is_retryable_result(result):
                        break
            finally:
                self._jobs.release(job)

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
            if not confirmed:
                await reply(
                    "⚠️ Could Not Check the Code",
                    f"The test accounts could not use `{safe_code}` ({message}), so the code is not confirmed "
                    "and was not queued. Check the spelling and try again.",
                    EmbedColors.WARNING,
                )
                return

            is_new, _ = await self._gift_code_service.add_submitted_gift_code(gift_code)
            logger.info(
                "User %s entered gift code '%s' in guild %s (new=%s, probe=%s)",
                interaction.user.id, gift_code, guild_id, is_new,
                result.get("error_code") or result.get("status_category"),
            )
            self._ensure_pending_worker()
            own_note = ""
            if str(probe_player.player_id) in owned_ids and result.get("status_category") == STATUS_SUCCESS:
                own_note = " Your account already received it."
            await reply(
                "✅ Code Accepted — Thank You!",
                f"`{safe_code}` works.{own_note} It is now being redeemed for {len(targets)} account(s). "
                + await self._summary_place(guild_id),
                EmbedColors.SUCCESS,
            )
        except Exception:
            logger.exception("Could not process the entered gift code '%s'", gift_code)
            await reply("❌ Could Not Check the Code", "Something went wrong. Try again in a minute.", EmbedColors.ERROR)

    async def _handle_known_code(
        self, interaction: discord.Interaction, code_row: Any, guild_id: int, is_manager: bool, reply
    ) -> None:
        """A code the bot already has: members are told so, a manager runs it again."""
        gift_code = str(code_row.code)
        safe_code = gift_code.replace("`", "")
        if getattr(code_row, "auto_redeem_status", None) == AUTO_REDEEM_PENDING:
            await reply(
                "ℹ️ The Bot Already Has This Code",
                f"`{safe_code}` is being redeemed for everyone on the list right now. "
                "Your own result is under **My accounts** in `/gift`.",
                EmbedColors.INFO,
            )
            return
        if not is_manager:
            await reply(
                "ℹ️ The Bot Already Has This Code",
                f"`{safe_code}` is already redeemed for everyone on the list. "
                "Your own result is under **My accounts** in `/gift`.",
                EmbedColors.INFO,
            )
            return

        targets = await self._player_registry_service.get_auto_redeem_targets()
        redeemed = await self._gift_code_service.get_redeemed_players(None, gift_code)
        missing = [player for player in targets if str(player.player_id) not in redeemed]
        if not missing:
            await reply(
                "✅ Everyone Already Has This Code",
                f"All {len(targets)} enabled account(s) on the gift-code list have `{safe_code}`.",
                EmbedColors.SUCCESS,
            )
            return
        if self._closing or not await self._reopen_code(code_row):
            await reply(
                "⚠️ Could Not Run the Code Again",
                "The code could not be queued right now. Try again in a minute.",
                EmbedColors.WARNING,
            )
            return
        key = CodeJobGuard.key(gift_code)
        # The first pass also retries accounts whose earlier failure looked final (a
        # fixed Governor ID, a Town Center that has grown since).
        self._retry_failed.add(key)
        self._rerun_guilds.setdefault(key, set()).add(guild_id)
        channel_id = await self._gift_channel_id(guild_id)
        if channel_id:
            # This server gets a fresh summary instead of an edit of the original one.
            (await self._load_announcements(gift_code)).pop(channel_id, None)
        self._ensure_pending_worker()
        logger.info("User %s reopened gift code '%s' from guild %s", interaction.user.id, gift_code, guild_id)
        minutes = max(1, round(len(missing) * MIN_REQUEST_INTERVAL_SECONDS / 60))
        await reply(
            "🎁 Running the Code Again",
            f"`{safe_code}` is being redeemed for the {len(missing)} of {len(targets)} enabled account(s) "
            f"that do not have it yet — about {minutes} minute(s). " + await self._summary_place(guild_id),
            EmbedColors.SUCCESS,
        )

    @staticmethod
    def _estimate_finish(total: int, results: list[dict], started_at: float | None) -> int | None:
        """Unix time the run should finish, from observed speed and the 2 s request pacing."""
        remaining = total - len(results)
        if remaining <= 0 or started_at is None:
            return None
        attempted = sum(1 for result in results if result.get("redemption_attempted"))
        elapsed = max(0.0, time.time() - started_at)
        per_player = elapsed / attempted if attempted >= 3 else 0.0
        per_player = max(per_player, MIN_REQUEST_INTERVAL_SECONDS)
        return int(time.time() + remaining * per_player)

    # --------------------------------------------------------------- rosters

    def _build_player_lines(self, players: Sequence[Any]) -> List[str]:
        """Format bounded, labeled player rows; missing data stays unavailable."""
        lines = []
        for player in players:
            status = "Enabled" if player.enabled else "Disabled"
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
        # Three characters are always a typed tag (tags such as "777" are digits too);
        # alliance IDs from the autocomplete list are much longer.
        if value.isdigit() and len(value) != 3:
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

    # ------------------------------------------------------------ add accounts

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

    @staticmethod
    def _limit_hint(max_owned: Optional[int]) -> str:
        return f"You can add up to {max_owned} players yourself. Ask a server manager to add more."

    async def _server_kingdom(self, guild_id: Optional[int]) -> Optional[str]:
        """The kingdom chosen in the server's settings, as profiles store it."""
        if guild_id is None or self._guild_configuration_service is None:
            return None
        settings = await self._guild_configuration_service.get_settings(guild_id)
        return kingdom_key(getattr(settings, "kingdom_id", None))

    async def _may_manage_entry(self, interaction: discord.Interaction, entry: Any) -> bool:
        """Who may remove or switch an account: its owner, its kingdom's managers, operators.

        A server manages the roster it sees: the accounts of the kingdom chosen in its
        settings. Managers of other servers cannot touch them.
        """
        if getattr(entry, "added_by_user_id", None) == interaction.user.id:
            return True
        if self._access.is_super_admin(interaction.user.id):
            return True
        if not await self._access.can_manage(interaction):
            return False
        kingdom = await self._server_kingdom(interaction_guild_id(interaction))
        return kingdom is not None and kingdom_key(getattr(entry, "kingdom", None)) == kingdom

    async def _handle_add_player_slash(self, interaction: discord.Interaction, player_ids: str):
        """Add Governor IDs to the shared gift-code list, within the member's own limit."""
        guild_id = interaction_guild_id(interaction)
        if guild_id is None:
            await send_denied(interaction, "Add Governor IDs in a server.")
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

            # Skip lookups for players already listed and for players over the member's
            # limit; the registration transaction below re-checks both.
            existing = await self._player_registry_service.get_entries(candidates)
            owned = await self._player_registry_service.count_owned(interaction.user.id) if max_owned is not None else 0
            capacity = len(candidates) if max_owned is None else max(0, max_owned - owned)
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

            outcome = RegistrationOutcome()
            if profiles:
                outcome = await self._player_registry_service.register_players(
                    profiles,
                    added_by_user_id=interaction.user.id,
                    max_owned=max_owned,
                )
            names = {profile["player_id"]: profile.get("player_name") for profile in profiles}
            kingdoms = sorted(
                {str(profile["kingdom"]) for profile in profiles
                 if profile["player_id"] in outcome.added and profile.get("kingdom")}
            )
            catching_up = bool(outcome.added) and await self._catch_up_new_accounts() > 0
            embed = self._build_add_player_embed(
                outcome=outcome,
                names=names,
                already_ids=already_ids + outcome.already_registered,
                over_limit=over_limit + outcome.over_limit,
                limit_hint=self._limit_hint(max_owned),
                not_found=not_found,
                unchecked=unchecked,
                invalid_ids=invalid_ids,
                ignored_ids=ignored_ids,
                owned_after=(owned + len(outcome.added)) if max_owned is not None else None,
                max_owned=max_owned,
                kingdoms=kingdoms,
                catching_up=catching_up,
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            logger.info(
                "User %s added %s players to the gift-code list from guild %s",
                interaction.user.id, len(outcome.added), guild_id,
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
        kingdoms: Sequence[str] = (),
        catching_up: bool = False,
    ) -> discord.Embed:
        problems = bool(over_limit or not_found or unchecked or invalid_ids or ignored_ids)
        if outcome.added:
            title = f"✅ Added {len(outcome.added)} Player(s)"
            color = EmbedColors.WARNING if problems else EmbedColors.SUCCESS
        elif already_ids and not problems:
            title, color = "ℹ️ Already on the Gift-Code List", EmbedColors.INFO
        else:
            title, color = "❌ No Players Added", EmbedColors.ERROR
        description = "New gift codes are now redeemed for them automatically." if outcome.added else ""
        if catching_up:
            description += (
                "\nThe codes active right now are being redeemed for them too: "
                "check **My accounts** in a few minutes."
            )
        if kingdoms:
            listed = ", ".join(f"`{kingdom[:10]}`" for kingdom in kingdoms[:5])
            description += f"\nManagers of servers set to kingdom {listed} see them in their roster."
        if owned_after is not None and max_owned is not None:
            description += f"\nYou have added {owned_after}/{max_owned} players."
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
        add_list("Already on the list", list(dict.fromkeys(already_ids)))
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

    # ----------------------------------------------------------- /giftadmin import

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
                        f"The alliance list for kingdom `{kid}` was still loading. Run `/giftadmin import` again in a "
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
        """Import all fid-bearing alliance members into the gift-code list (managers)."""
        if defer_response:
            await interaction.response.defer(thinking=True)

        guild_id = interaction_guild_id(interaction)
        if guild_id is None:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Server Required",
                    description="Run `/giftadmin import` in a server.",
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
                        # The member is in this kingdom's alliance now: a stored kingdom
                        # from before a transfer would keep the account in the old roster.
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
                outcome = await self._player_registry_service.register_players(
                    profiles,
                    added_by_user_id=interaction.user.id,
                    enable_existing=True,
                )

            alliance_name = (
                self._extract_alliance_name(payload) or alternate_alliance_name or alliance_label or f"Alliance {aid}"
            )
            safe_name = discord.utils.escape_markdown(str(alliance_name))[:100]
            description = (
                f"Roster imported from **{safe_name}** in kingdom `{kid}`.\n"
                f"Managers of servers set to kingdom `{kid}` see these players in `/giftadmin roster`."
            )
            if outcome.added and await self._catch_up_new_accounts() > 0:
                description += "\nThe codes active right now are being redeemed for the new accounts too."
            if outcome.added:
                title = f"✅ Added {len(outcome.added)} Alliance Member(s)"
                color = EmbedColors.SUCCESS
            elif outcome.already_registered:
                title, color = "ℹ️ Alliance Already on the Gift-Code List", EmbedColors.INFO
            elif not profiles and skipped_members:
                title, color = "⚠️ No Governor IDs Available", EmbedColors.WARNING
                description = (
                    f"The roster for **{safe_name}** in kingdom `{kid}` contains internal IDs, "
                    "but no connected source supplied Governor IDs. Ask a bot operator to enable "
                    "the alliance roster source, or add Governor IDs with **Add my ID** in `/gift`."
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
                    name=f"Already on the list ({len(outcome.already_registered)})",
                    value=f"Kept as they are{reenabled}.",
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

    # ------------------------------------------------------ remove an account

    REMOVE_DENIED = (
        "You can only remove players you added. Managers of a server can also remove the players "
        "of that server's kingdom (set in `/settings`)."
    )
    TOGGLE_DENIED = (
        "You can only pause or resume players you added. Managers of a server can also "
        "switch the players of that server's kingdom (set in `/settings`)."
    )

    async def _request_remove(self, interaction: discord.Interaction, entry: Any, *, on_removed=None) -> None:
        """Ask privately before an account is taken off the list (from "My accounts" or the roster).

        ``on_removed`` is awaited once the account is gone, so the panel that asked redraws.
        """
        if not await self._may_manage_entry(interaction, entry):
            await send_denied(interaction, self.REMOVE_DENIED)
            return
        view = RemovePlayerConfirmView(self, interaction.user.id, entry, on_removed)
        await interaction.response.send_message(
            embed=self._build_status_embed(
                title="Remove Player?",
                description=(
                    f"Remove {_player_label(entry.player_id, entry.player_name)} from the gift-code list? "
                    "The list is shared by every server: gift codes are no longer redeemed for this account."
                ),
                color=EmbedColors.WARNING,
            ),
            view=view,
            ephemeral=True,
        )
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            logger.debug("Could not load the remove confirmation message", exc_info=True)

    async def _confirm_remove_player(self, interaction: discord.Interaction, view: RemovePlayerConfirmView) -> None:
        entry = view.entry
        if not await self._may_manage_entry(interaction, entry):
            await send_denied(interaction, self.REMOVE_DENIED)
            return
        view.stop()
        # Acknowledge before the database write (Discord's 3-second deadline).
        await interaction.response.defer()
        outcome = await self._player_registry_service.remove_player(entry.player_id)
        if not outcome.removed:
            embed = self._build_status_embed(
                title="Already Removed",
                description=f"{_player_label(entry.player_id, entry.player_name)} is no longer on the gift-code list.",
                color=EmbedColors.INFO,
            )
        else:
            embed = self._build_status_embed(
                title="✅ Player Removed",
                description=f"{_player_label(entry.player_id, entry.player_name)} was removed from the gift-code list.",
                color=EmbedColors.SUCCESS,
            )
            logger.info("Player %s removed from the gift-code list by %s", entry.player_id, interaction.user.id)
        await interaction.edit_original_response(embed=embed, view=None)
        if view.on_removed is not None:
            try:
                await view.on_removed()
            except Exception:
                logger.warning("Could not redraw the panel after removing %s", entry.player_id, exc_info=True)

    # ------------------------------------------------------ /giftadmin roster

    async def _handle_list_players_slash(self, interaction: discord.Interaction, kingdom: Optional[int] = None):
        """Managers: the accounts of this server's kingdom (operators: of any kingdom)."""
        guild_id = interaction_guild_id(interaction)
        if guild_id is None:
            await send_denied(
                interaction, "Use /giftadmin roster in a server: it lists the players of that server's kingdom."
            )
            return
        if not await self._access.require_manager(interaction, action="manage the gift-code roster"):
            return
        is_operator = self._access.is_super_admin(interaction.user.id)
        if kingdom is not None and not is_operator:
            await send_denied(
                interaction,
                "Only bot operators can open another kingdom's roster. Leave `kingdom` empty for this server's kingdom.",
            )
            return
        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            roster_kingdom = kingdom_key(kingdom) or await self._server_kingdom(guild_id)
            if roster_kingdom is None:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="📋 Which Kingdom?",
                        description=(
                            "The roster lists every account of this server's kingdom on the gift-code list, "
                            "and this server has no kingdom yet. Set it in `/settings`.\n"
                            "Your own accounts are under **My accounts** in `/gift`."
                        ),
                        color=EmbedColors.INFO,
                    ),
                    ephemeral=True,
                )
                return

            roster = await self._player_registry_service.list_kingdom_roster(roster_kingdom)
            if not roster:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="📋 No Players Yet",
                        description=(
                            f"No account of kingdom `{roster_kingdom}` is on the gift-code list. Add Governor IDs "
                            "with **Add my ID** in `/gift` or import an alliance with `/giftadmin import`."
                        ),
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
                kingdom=roster_kingdom,
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
        """Enable/disable the selected account: the roster's managers, or the owner of the account."""
        entry = view.selected_player
        if entry is None:
            await interaction.response.send_message("Select a player first.", ephemeral=True)
            return
        # Checked on every click, not from the open panel: the server's kingdom or the
        # member's role may have changed since the roster was opened.
        if not await self._may_manage_entry(interaction, entry):
            await send_denied(interaction, self.TOGGLE_DENIED)
            return

        # Acknowledge before the database write. A write slower than Discord's 3-second
        # deadline used to fail the click while the change was saved; the button kept
        # its old label, so clicking it again silently undid the change.
        await interaction.response.defer()
        new_state = not entry.enabled
        entry_on_list = await self._player_registry_service.get_entry(entry.player_id)
        if entry_on_list is None:
            await interaction.followup.send(
                "This player is no longer on the gift-code list. Run /giftadmin roster again.", ephemeral=True
            )
            return
        await self._player_registry_service.set_players_enabled([entry.player_id], new_state)
        entry.enabled = new_state
        if new_state:
            # An account switched back on gets the codes that are active right now.
            await self._catch_up_new_accounts()
        view.refresh(keep_selection=True)
        await interaction.edit_original_response(embed=view.build_embed(), view=view)

    async def _send_roster_health(self, interaction: discord.Interaction, view: PlayerListPaginationView) -> None:
        if not await self._access.require_manager(interaction, action="view roster health"):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            roster = await self._player_registry_service.list_kingdom_roster(view.kingdom)
            report = await self._redemption.build_roster_health(roster, code_limit=self.ROSTER_HEALTH_CODES)
        except Exception:
            logger.exception("Could not build the roster health report for kingdom %s", view.kingdom)
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
            health_view = RosterHealthView(self, interaction.user.id, report.failing_players)
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
            embed.set_footer(text="Disable these to stop spending redemption requests on them.")
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
        # Acknowledge before the database reads and the write (Discord's 3-second deadline).
        await interaction.response.defer()
        # The report was built for the kingdom this server had then: disable only the
        # accounts this manager may still manage.
        entries = await self._player_registry_service.get_entries([str(player.player_id) for player in view.failing])
        allowed = [
            player_id for player_id, entry in entries.items() if await self._may_manage_entry(interaction, entry)
        ]
        changed = await self._player_registry_service.set_players_enabled(allowed, False)
        view.disable_button.disabled = True
        view.stop()
        await interaction.edit_original_response(view=view)
        await interaction.followup.send(
            embed=self._build_status_embed(
                title="✅ Players Disabled",
                description=(
                    f"Disabled {changed} player(s) on the gift-code list. "
                    "Re-enable them any time in `/giftadmin roster`."
                ),
                color=EmbedColors.SUCCESS,
            ),
            ephemeral=True,
        )

    @staticmethod
    def _build_status_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
        """Build a consistent status embed for command responses."""
        return build_status_embed(title=title, description=description, color=color)
