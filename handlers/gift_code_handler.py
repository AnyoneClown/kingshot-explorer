import asyncio
import socket
import logging
import random
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config.bot_config import BotConfig
from handlers.ui import EmbedColors, OwnedView, build_status_embed, send_ui_error
from services.gift_code_service import IGiftCodeService
from services.player_info_service import IPlayerInfoService
from services.interaction_tracking_service import InteractionTrackingService
from services.player_registry_service import PlayerRegistryService
from services.kingshot_data_service import KingshotDataService
from services.mightpulse_service import (
    MightPulseAllianceDirectory,
    MightPulseService,
    MightPulseUnavailableError,
)

logger = logging.getLogger(__name__)


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
    """Search and filter registered players in a single message."""

    PAGE_SIZE = 10

    def __init__(self, handler: "GiftCodeHandler", players: list[Any], author_id: int):
        super().__init__(author_id)
        self.handler = handler
        self.players = players
        self.query = ""
        self.status = "all"
        self.current_page = 0
        self.selected_player = None
        self.refresh()

    def refresh(self) -> None:
        query = self.query.casefold()
        self.filtered_players = [
            player for player in self.players
            if (self.status == "all" or bool(player.enabled) == (self.status == "enabled"))
            and (query in str(player.player_id).casefold()
                 or query in str(player.player_name or "").casefold())
        ]
        self.page_count = max(1, (len(self.filtered_players) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.current_page = min(max(0, self.current_page), self.page_count - 1)
        start = self.current_page * self.PAGE_SIZE
        page = self.filtered_players[start:start + self.PAGE_SIZE]
        self.selected_player = page[0] if page else None
        self.player_select.options = [
            discord.SelectOption(
                label=f"{player.player_name or 'Unnamed player'} · {player.player_id}"[:100],
                value=str(index), default=index == 0,
            ) for index, player in enumerate(page)
        ] or [discord.SelectOption(label="No matching players", value="none")]
        self.player_select.disabled = not page
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = self.current_page == self.page_count - 1
        self.clear_button.disabled = not self.query
        self.view_player.disabled = not page or self.handler._bot.tree.get_command("stats") is None
        for option in self.status_select.options:
            option.default = option.value == self.status

    def build_embed(self) -> discord.Embed:
        enabled = sum(bool(player.enabled) for player in self.players)
        lines = self.handler._build_player_lines(self.filtered_players[
            self.current_page * self.PAGE_SIZE:(self.current_page + 1) * self.PAGE_SIZE
        ])
        description = (
            f"**{len(self.filtered_players)} matches** · {len(self.players)} total · "
            f"{enabled} enabled · {len(self.players) - enabled} disabled\n"
            f"Filter: **{self.status.title()}**"
        )
        if self.query:
            description += f" · Search: {discord.utils.escape_markdown(self.query)}"
        embed = discord.Embed(
            title="📋 Player Profiles", color=EmbedColors.INFO,
            description=description + "\n\n" + ("\n".join(lines) or "No players match. Clear the search or change the filter."),
        )
        embed.set_footer(text=f"Page {self.current_page + 1}/{self.page_count} · Select a player, then View player")
        return embed

    @discord.ui.select(placeholder="Select a player", row=0)
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        index = int(select.values[0])
        self.selected_player = self.filtered_players[self.current_page * self.PAGE_SIZE + index]
        for option in select.options:
            option.default = option.value == select.values[0]
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


class GiftCodesView(OwnedView):
    PAGE_SIZE = 10

    def __init__(self, handler: "GiftCodeHandler", codes: list[dict], author_id: int, can_redeem: bool):
        super().__init__(author_id)
        self.handler = handler
        self.codes = codes
        self.current_page = 0
        self.selected_index = 0
        if not can_redeem:
            self.remove_item(self.redeem_button)
        self.refresh()

    def refresh(self):
        start = self.current_page * self.PAGE_SIZE
        self.selected_index = start
        self.code_select.options = [
            discord.SelectOption(label=str(code["code"])[:100], value=str(index), default=index == start)
            for index, code in enumerate(self.codes[start:start + self.PAGE_SIZE], start)
        ]
        self.prev_button.disabled = self.current_page == 0
        self.next_button.disabled = start + self.PAGE_SIZE >= len(self.codes)

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(title="🎁 Active Gift Codes", color=EmbedColors.INFO,
                              description="Select a code to redeem for all enabled players. Manual redemption requires a bot admin.")
        start = self.current_page * self.PAGE_SIZE
        for code in self.codes[start:start + self.PAGE_SIZE]:
            expiry = "Expiration unavailable"
            if code.get("expiresAt"):
                try:
                    dt = datetime.fromisoformat(code["expiresAt"].replace("Z", "+00:00"))
                    expiry = f"Expires <t:{int(dt.timestamp())}:R>"
                except (ValueError, TypeError):
                    pass
            embed.add_field(name=str(code["code"])[:256], value=expiry, inline=False)
        embed.set_footer(text=f"Page {self.current_page + 1}/{(len(self.codes) + self.PAGE_SIZE - 1) // self.PAGE_SIZE} · Auto-redemption runs when new codes appear")
        return embed

    @discord.ui.select(placeholder="Select a gift code", row=0)
    async def code_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_index = int(select.values[0])
        for option in select.options:
            option.default = option.value == select.values[0]
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=1)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = max(0, self.current_page - 1)
        self.refresh()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page = min((len(self.codes) - 1) // self.PAGE_SIZE, self.current_page + 1)
        self.refresh()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Redeem selected code", style=discord.ButtonStyle.primary, row=1)
    async def redeem_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handler._handle_redeem_gift_code_slash(interaction, str(self.codes[self.selected_index]["code"]))


class RedemptionResultsView(OwnedView):
    PAGE_SIZE = 4

    def __init__(self, summary: discord.Embed, issues: list[dict], author_id: int):
        super().__init__(author_id)
        self.summary = summary
        self.issues = issues
        self.current_page = 0
        self.expanded = False
        self.refresh()

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
            status = "Skipped" if result.get("status_category") == "skipped" else "Failed"
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

    STATUS_SUCCESS = "success"
    STATUS_ALREADY_REDEEMED = "already_redeemed"
    STATUS_API_REJECTED = "api_rejected"
    STATUS_INVALID_ID = "invalid_id"
    STATUS_SKIPPED = "skipped"
    REDEEM_MAX_RETRIES = 2
    REDEEM_RATE_LIMIT_MAX_RETRIES = 3
    REDEEM_RETRY_DELAY_SECONDS = 1.0
    REDEEM_RETRY_MAX_DELAY_SECONDS = 30.0
    REDEEM_RATE_LIMIT_DELAY_SECONDS = 60.0
    REDEEM_RATE_LIMIT_MAX_DELAY_SECONDS = 60.0
    REDEEM_CONCURRENCY = 3
    ALLIANCE_CACHE_TTL_SECONDS = 10 * 60
    ALLIANCE_AUTOCOMPLETE_WAIT_SECONDS = 2.4
    ALLIANCE_AUTOCOMPLETE_MAX_CHOICES = 15
    ALLIANCE_KID_PREWARM_DEBOUNCE_SECONDS = 0.35
    ALLIANCE_CHOICE_NAME_MAX_LENGTH = 100
    ALLIANCE_COMPLETION_RETRY_SECONDS = 30.0

    def __init__(
        self,
        gift_code_service: IGiftCodeService,
        player_info_service: IPlayerInfoService,
        bot: commands.Bot,
        config: BotConfig,
        interaction_tracking_service: InteractionTrackingService | None = None,
        player_registry_service: PlayerRegistryService | None = None,
        kingshot_data_service: KingshotDataService | None = None,
        mightpulse_service: MightPulseService | None = None,
        alliance_directory_service: MightPulseAllianceDirectory | None = None,
    ):
        """
        Initialize gift code handler.

        Args:
            gift_code_service: Service for redeeming gift codes
            player_info_service: Service for validating player existence
            bot: Discord bot instance
            config: Bot configuration
        """
        self._gift_code_service = gift_code_service
        self._player_info_service = player_info_service
        self._bot = bot
        self._config = config
        self._tracking_service = interaction_tracking_service or InteractionTrackingService()
        self._player_registry_service = player_registry_service or PlayerRegistryService()
        self._kingshot_data_service = kingshot_data_service
        self._mightpulse_service = mightpulse_service
        self._alliance_directory_service = alliance_directory_service
        self._polling_loop = None
        self._poll_backoff_until: datetime | None = None
        self._poll_backoff_delta = timedelta(minutes=2)
        self._manual_redemption_start_lock = asyncio.Lock()
        self._manual_redemption_task: asyncio.Task[None] | None = None
        self._manual_redemption_code: str | None = None
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
        logger.info("GiftCodeHandler initialized")

    def _can_poll(self) -> bool:
        return self._poll_backoff_until is None or datetime.now(timezone.utc) >= self._poll_backoff_until

    def _mark_poll_backoff(self, error: Exception) -> None:
        self._poll_backoff_until = datetime.now(timezone.utc) + self._poll_backoff_delta
        logger.warning("Gift code polling will retry after %s because of networking error: %s", self._poll_backoff_delta, error)

    def _is_bot_admin(self, interaction: discord.Interaction) -> bool:
        return int(interaction.user.id) in self._config.admin_user_ids

    async def _send_admin_only_response(self, interaction: discord.Interaction) -> None:
        embed = self._build_status_embed(
            title="⛔ Admin Only",
            description="Only configured bot admins can use this command.",
            color=EmbedColors.WARNING,
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    def register_commands(self):
        """Register all gift code commands with the bot."""

        @self._bot.tree.command(name="redeem", description="Start a gift-code redemption job")
        @app_commands.describe(gift_code="The gift code to redeem (e.g., KINGSHOTXMAS)")
        async def redeem_gift_code(interaction: discord.Interaction, gift_code: str):
            """Redeem a gift code for all registered players."""
            await self._handle_redeem_gift_code_slash(interaction, gift_code)

        @self._bot.tree.command(name="addplayer", description="Add one or more players to gift code redemption list")
        @app_commands.describe(player_ids="The player ID(s) to add, comma-separated")
        async def add_player(interaction: discord.Interaction, player_ids: str):
            """Add a player to gift code list using API name."""
            await self._handle_add_player_slash(interaction, player_ids)

        @self._bot.tree.command(name="addalliance", description="Add an alliance to gift code auto-redemption")
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

        @self._bot.tree.command(name="removeplayer", description="Remove a player from gift code redemption list")
        @app_commands.describe(player_id="The player ID to remove")
        async def remove_player(interaction: discord.Interaction, player_id: str):
            """Remove a player from gift code redemption list."""
            await self._handle_remove_player_slash(interaction, player_id)

        @self._bot.tree.command(name="listplayers", description="List all known players and redemption status")
        async def list_players(interaction: discord.Interaction):
            """List all known players and redemption status."""
            await self._handle_list_players_slash(interaction)

        @self._bot.tree.command(name="playerlist", description="Alias for /listplayers")
        async def player_list_alias(interaction: discord.Interaction):
            """Alias command for listing all players."""
            await self._handle_list_players_slash(interaction)

        @self._bot.tree.command(name="giftcodes", description="List available gift codes")
        async def list_giftcodes(interaction: discord.Interaction):
            """List available gift codes from the API."""
            await self._handle_list_gift_codes_slash(interaction)

    def start_polling_task(self):
        """Start the background task that checks for new gift codes."""
        if self._polling_loop and self._polling_loop.is_running():
            logger.info("Gift code polling task already running; skipping duplicate start")
            return

        @tasks.loop(minutes=1)
        async def poll_gift_codes():
            """Check for new gift codes and redeem them for all users."""
            logger.debug("Polling for new gift codes...")

            if not self._can_poll():
                logger.debug("Skipping gift-code polling due temporary network error backoff window")
                return

            try:
                # Fetch available codes from 3rd party API
                response = await self._gift_code_service.get_available_gift_codes()
                if not response.get("success"):
                    logger.warning(f"Failed to poll gift codes: {response.get('message')}")
                    return

                codes = response.get("data", [])
                if not codes:
                    return

                # Check which codes are new
                new_codes_found = []

                for row in codes:
                    code_id = row.get("id")
                    code_str = row.get("code")

                    if not code_id or not code_str:
                        continue

                    # Parse dates
                    created_at_api = row.get("createdAt")
                    expires_at = row.get("expiresAt")

                    try:
                        # Parse ISO format datetime strings
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

                        if is_new:
                            new_codes_found.append(code_str)

                    except ValueError as e:
                        logger.error(f"Error parsing date for gift code {code_str}: {e}")

                # If we found new codes, redeem them!
                if new_codes_found:
                    logger.info(f"Found {len(new_codes_found)} new gift codes. Starting auto-redemption...")

                    # Ensure the bot user exists in the database to satisfy the foreign key constraint
                    bot_user = self._bot.user
                    bot_user_id = bot_user.id if bot_user else 0
                    bot_username = bot_user.name if bot_user else "System Bot"
                    bot_discriminator = getattr(bot_user, "discriminator", "0000") if bot_user else "0000"
                    bot_display_name = getattr(bot_user, "display_name", "System Bot") if bot_user else "System Bot"

                    await self._tracking_service.track_user(
                        session=None,
                        user_id=bot_user_id,
                        username=bot_username,
                        discriminator=bot_discriminator,
                        display_name=bot_display_name,
                    )

                    # Get all enabled players
                    registered_players = await self._player_registry_service.get_registered_players(enabled_only=True)

                    if not registered_players:
                        logger.info("No registered players to auto-redeem for.")
                        return

                    # Redeem each code for each player
                    for new_code in new_codes_found:
                        logger.info(
                            f"Auto-redeeming code '{new_code}' for {len(registered_players)} players..."
                        )

                        results = await self._run_bulk_redemption(
                            gift_code=new_code,
                            registered_players=registered_players,
                            actor_user_id=bot_user_id,
                            guild_id=None,
                            channel_id=None,
                        )

                        await self._send_auto_redemption_announcement(
                            gift_code=new_code,
                            total_players=len(registered_players),
                            results=results,
                        )

            except socket.gaierror as e:
                self._mark_poll_backoff(e)
                logger.error(f"Error in poll_gift_codes background task: {e}")
            except Exception as e:
                logger.error(f"Error in poll_gift_codes background task: {e}")

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

    async def _send_auto_redemption_announcement(
        self,
        *,
        gift_code: str,
        total_players: int,
        results: list[dict[str, Any]],
    ) -> None:
        """Announce the final auto-redemption outcome, including rejected codes."""
        success_count = sum(
            result.get("status_category") == self.STATUS_SUCCESS for result in results
        )
        if not self._config.auto_redeem_channels:
            return

        already_redeemed_count = sum(
            result.get("status_category") == self.STATUS_ALREADY_REDEEMED
            for result in results
        )
        api_rejected_count = sum(
            result.get("status_category") == self.STATUS_API_REJECTED for result in results
        )
        invalid_id_count = sum(
            result.get("status_category") == self.STATUS_INVALID_ID for result in results
        )
        skipped_count = sum(
            result.get("status_category") == self.STATUS_SKIPPED for result in results
        )

        global_code_error = next(
            (result for result in results if self._is_global_code_rejection(result)),
            None,
        )
        if global_code_error is not None:
            title = "❌ Gift Code Rejected"
            description = str(global_code_error.get("message") or "The gift code was rejected.")
            color = EmbedColors.ERROR
        elif total_players and already_redeemed_count == total_players:
            title = "✅ Everyone Has Already Claimed This Code"
            description = "Auto-redemption checked every enabled player; no new claims were needed."
            color = EmbedColors.SUCCESS
        elif success_count > 0:
            title = "🎁 New Gift Code Found!"
            description = "Auto-redemption completed for a newly discovered gift code."
            color = EmbedColors.SUCCESS
        else:
            title = "❌ Auto-Redemption Failed"
            description = "Auto-redemption completed without a successful claim."
            color = EmbedColors.ERROR

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
        )
        embed.add_field(name="Gift Code", value=f"`{gift_code}`", inline=False)
        embed.add_field(
            name="Auto-Redeem Status",
            value=(
                f"✅ **Success**: {success_count}\n"
                f"🔄 **Already Claimed**: {already_redeemed_count}\n"
                f"🚫 **API Rejected**: {api_rejected_count}\n"
                f"🆔 **Invalid ID**: {invalid_id_count}\n"
                f"⏭️ **Skipped**: {skipped_count}\n"
                f"👥 **Total Players**: {total_players}"
            ),
            inline=False,
        )
        embed.set_footer(text="Check in-game mail for successfully redeemed codes!")

        for channel_id in self._config.auto_redeem_channels:
            channel = self._bot.get_channel(channel_id)
            if channel and isinstance(channel, discord.TextChannel):
                try:
                    await channel.send(embed=embed)
                    logger.info("Announced gift code %s in channel %s", gift_code, channel_id)
                except Exception as e:
                    logger.error(
                        "Failed to send gift code announcement to channel %s: %s",
                        channel_id,
                        e,
                    )
            else:
                logger.warning(
                    "Configured auto-redeem channel %s not found or is not a text channel",
                    channel_id,
                )

    async def _handle_list_gift_codes_slash(self, interaction: discord.Interaction):
        """Handle listing available gift codes."""
        await interaction.response.defer(thinking=True)

        try:
            response = await self._gift_code_service.get_available_gift_codes()

            if not response.get("success"):
                embed = self._build_status_embed(
                    title="❌ Could Not Fetch Gift Codes",
                    description="The gift code list could not be retrieved.",
                    color=EmbedColors.ERROR,
                )
                embed.add_field(name="Details", value=str(response.get("message", "Unknown error")), inline=False)
                await interaction.followup.send(embed=embed)
                return

            codes = [code for code in response.get("data", []) if isinstance(code, dict) and code.get("code")]

            if not codes:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="📋 No Active Gift Codes",
                        description="No currently active gift codes were found.",
                        color=EmbedColors.INFO,
                    )
                )
                return

            view = GiftCodesView(self, codes, interaction.user.id, self._is_bot_admin(interaction))
            view.message = await interaction.followup.send(embed=view.build_embed(), view=view, wait=True)

        except Exception as e:
            logger.error(f"Error listing gift codes: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Unexpected Error",
                    description="An unexpected error occurred while fetching gift codes.",
                    color=EmbedColors.ERROR,
                )
            )

    async def _handle_redeem_gift_code_slash(self, interaction: discord.Interaction, gift_code: str):
        """
        Handle the redeem command for all registered players.

        Args:
            interaction: Discord interaction
            gift_code: The gift code to redeem
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

        if not self._is_bot_admin(interaction):
            await self._send_admin_only_response(interaction)
            return

        user_info = f"{interaction.user.name}#{interaction.user.discriminator} (ID: {interaction.user.id})"
        guild_info = f"{interaction.guild.name} (ID: {interaction.guild.id})" if interaction.guild else "DM"

        logger.info(f"Bulk redeem command for code '{gift_code}' requested by {user_info} in {guild_info}")

        channel = interaction.channel
        if channel is None or not hasattr(channel, "send"):
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Channel Unavailable",
                    description="The bot cannot post the redemption job result in this channel.",
                    color=EmbedColors.ERROR,
                ),
                ephemeral=True,
            )
            return

        gift_code = gift_code.strip()
        if not gift_code or len(gift_code) > 200:
            await interaction.followup.send("Enter a gift code between 1 and 200 characters.", ephemeral=True)
            return
        async with self._manual_redemption_start_lock:
            active_task = self._manual_redemption_task
            if active_task is not None and not active_task.done():
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="⏳ Redemption Job Already Running",
                        description=(
                            f"The bot is already redeeming `{self._manual_redemption_code}`. "
                            "Wait for its channel summary before starting another job."
                        ),
                        color=EmbedColors.WARNING,
                    ),
                    ephemeral=True,
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
                registered_players = await self._player_registry_service.get_registered_players(
                    enabled_only=True
                )
            except Exception as exc:
                logger.error("Could not prepare the manual redemption job: %s", exc, exc_info=True)
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="❌ Redemption Job Not Started",
                        description="The bot could not load the enabled player list.",
                        color=EmbedColors.ERROR,
                    ),
                    ephemeral=True,
                )
                return

            if not registered_players:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="📭 No Enabled Players",
                        description="Use `/addplayer <player_id>` to enable at least one player before redeeming.",
                        color=EmbedColors.WARNING,
                    ),
                    ephemeral=True,
                )
                return

            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="🎁 Redemption Job Started",
                    description=(
                        f"Started redeeming `{gift_code}` for {len(registered_players)} enabled players.\n"
                        "Watch the progress card in this channel; it becomes the final summary when the job finishes."
                    ),
                    color=EmbedColors.SUCCESS,
                ),
                ephemeral=True,
            )

            task = asyncio.create_task(
                self._run_manual_redemption_job(
                    gift_code=gift_code,
                    registered_players=registered_players,
                    actor_user_id=interaction.user.id,
                    guild_id=interaction.guild.id if interaction.guild else None,
                    channel=channel,
                ),
                name="manual-gift-code-redemption",
            )
            self._manual_redemption_task = task
            self._manual_redemption_code = gift_code
            task.add_done_callback(self._manual_redemption_finished)

    def _manual_redemption_finished(self, task: asyncio.Task[None]) -> None:
        """Release the single manual-job slot and surface unexpected task failures."""
        if self._manual_redemption_task is task:
            self._manual_redemption_task = None
            self._manual_redemption_code = None

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

    async def _run_manual_redemption_job(
        self,
        *,
        gift_code: str,
        registered_players: list[Any],
        actor_user_id: int,
        guild_id: int | None,
        channel: Any,
    ) -> None:
        """Update a normal message so long jobs survive interaction token expiry."""
        message = None
        update_lock = asyncio.Lock()
        last_update = 0.0
        last_phase = ""

        async def progress(results: list[dict], phase: str) -> None:
            nonlocal last_update, last_phase
            if message is None:
                return
            async with update_lock:
                now = asyncio.get_running_loop().time()
                if phase == last_phase and now - last_update < 5:
                    return
                last_update, last_phase = now, phase
                try:
                    await message.edit(embed=self._build_redemption_progress(
                        gift_code, len(registered_players), results, phase,
                    ))
                except Exception:
                    logger.warning("Could not update redemption progress", exc_info=True)

        try:
            message = await channel.send(embed=self._build_redemption_progress(
                gift_code, len(registered_players), [], "Checking prior claims",
            ))
            results = await self._run_bulk_redemption(
                gift_code=gift_code,
                registered_players=registered_players,
                actor_user_id=actor_user_id,
                guild_id=guild_id,
                channel_id=getattr(channel, "id", None),
                progress=progress,
            )
            await self._send_redemption_results_to_channel(
                channel=channel,
                requester_user_id=actor_user_id,
                gift_code=gift_code,
                results=results,
                message=message,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Error in manual redemption job: %s", exc, exc_info=True)
            try:
                payload = dict(
                    content=f"<@{actor_user_id}>",
                    embed=self._build_status_embed(
                        title="❌ Redemption Job Failed",
                        description=(
                            f"The background redemption job for `{gift_code}` stopped unexpectedly. "
                            "Some players may have received rewards; check in-game mail before retrying."
                        ),
                        color=EmbedColors.ERROR,
                    ),
                )
                if message is not None:
                    try:
                        await message.edit(**payload, view=None)
                        return
                    except discord.HTTPException:
                        pass
                await channel.send(**payload)
            except Exception as response_error:
                logger.error("Could not post the manual redemption failure: %s", response_error, exc_info=True)

    def _build_redemption_progress(
        self, gift_code: str, total: int, results: list[dict], phase: str,
    ) -> discord.Embed:
        return discord.Embed(
            title="🎁 Redemption in Progress",
            description=(f"**Gift code:** `{gift_code}`\n**Processed:** {len(results)}/{total} players\n"
                         f"**Status:** {phase}\n\n" + self._redemption_counts(results)),
            color=EmbedColors.INFO,
        )

    def _redemption_counts(self, results: list[dict]) -> str:
        labels = (
            (self.STATUS_SUCCESS, "✅ Success"),
            (self.STATUS_ALREADY_REDEEMED, "🔄 Already Claimed"),
            (self.STATUS_API_REJECTED, "🚫 API Rejected"),
            (self.STATUS_INVALID_ID, "🆔 Player/Kingdom Error"),
            (self.STATUS_SKIPPED, "⏭️ Skipped"),
        )
        return "\n".join(
            f"**{label}:** {sum(result.get('status_category') == status for result in results)}"
            for status, label in labels
        )

    async def _send_redemption_results_to_channel(
        self,
        channel: Any,
        requester_user_id: int,
        gift_code: str,
        results: List[Dict],
        message: discord.Message | None = None,
    ) -> None:
        """Replace the progress card with a compact summary and paginated issues."""
        success_count = sum(result.get("status_category") == self.STATUS_SUCCESS for result in results)
        already_count = sum(result.get("status_category") == self.STATUS_ALREADY_REDEEMED for result in results)
        issues = [result for result in results if result.get("status_category") not in {
            self.STATUS_SUCCESS, self.STATUS_ALREADY_REDEEMED,
        }]
        global_error = next((result for result in results if self._is_global_code_rejection(result)), None)
        if global_error is not None:
            title, color = "❌ Gift Code Rejected", EmbedColors.ERROR
        elif results and already_count == len(results):
            title, color = "✅ Everyone Has Already Claimed This Code", EmbedColors.SUCCESS
        elif results and success_count == len(results):
            title, color = "✅ All Gift Codes Redeemed Successfully!", EmbedColors.SUCCESS
        elif not issues:
            title, color = "✅ Gift Code Redemption Completed", EmbedColors.SUCCESS
        elif success_count + already_count:
            title, color = "⚠️ Gift Code Redemption Completed", EmbedColors.WARNING
        else:
            title, color = "❌ All Gift Code Redemptions Failed", EmbedColors.ERROR
        embed = discord.Embed(
            title=title, color=color,
            description=f"**Gift code:** `{gift_code}`\n**Processed:** {len(results)} players\n\n" + self._redemption_counts(results),
        )
        if global_error is not None:
            embed.add_field(name="Code rejected", value=str(global_error.get("message") or "The code is unavailable.")[:1024], inline=False)
        embed.set_footer(text="Check in-game mail for successful claims." + (" Open Details to review failed or skipped players." if issues else ""))
        view = RedemptionResultsView(embed, issues, requester_user_id) if issues else None
        payload = dict(content=f"<@{requester_user_id}>", embed=embed, view=view)
        if message is not None:
            try:
                await message.edit(**payload)
            except discord.HTTPException:
                message = None
        if message is None:
            message = await channel.send(**payload)
        if view is not None:
            view.message = message
        logger.info("Bulk redemption completed: success=%s, already_claimed=%s, issues=%s", success_count, already_count, len(issues))

    def _categorize_redemption_status(self, result: Dict) -> str:
        """Map API/database redemption result into a single status category."""
        if result.get("skipped", False):
            return self.STATUS_SKIPPED

        if result.get("success", False):
            return self.STATUS_SUCCESS

        if (
            result.get("already_redeemed", False)
            or result.get("already_redeemed_by_api", False)
            or result.get("error_code") in {"ALREADY_REDEEMED", "ALREADY_REDEEMED_BY_API"}
        ):
            return self.STATUS_ALREADY_REDEEMED

        if result.get("error_code") in {
            "INVALID_ID",
            "INVALID_USER_ID",
            "INVALID_PLAYER_ID",
            "KINGDOM_MISMATCH",
            "MISSING_KINGDOM",
            "PLAYER_LOOKUP_FAILED",
        }:
            return self.STATUS_INVALID_ID

        return self.STATUS_API_REJECTED

    @staticmethod
    def _is_global_code_rejection(result: Dict[str, Any]) -> bool:
        """Return whether one response proves the code is unusable for every player."""
        if result.get("global_code_error") is True:
            return True

        return result.get("error_code") in {
            "GIFT_CODE_EXPIRED",
            "GIFT_CODE_NOT_FOUND",
            "GIFT_CODE_CLAIM_LIMIT_REACHED",
        }

    def _is_retryable_redemption_result(self, result: Dict) -> bool:
        """Return whether a failed redemption looks transient enough to retry."""
        if self._categorize_redemption_status(result) != self.STATUS_API_REJECTED:
            return False

        raw_code = result.get("error_code")
        if raw_code is None and isinstance(result.get("error_details"), dict):
            raw_code = result["error_details"].get("err_code")

        code = str(raw_code or "").upper()
        retryable_codes = {
            "API_ERROR",
            "UNEXPECTED_ERROR",
            "UNKNOWN_ERROR",
            "TIMEOUT_RETRY",
            "RATE_LIMITED",
            "40019",
            "429",
            "500",
            "502",
            "503",
            "504",
        }
        if code in retryable_codes:
            return True

        try:
            numeric_code = int(code)
            if numeric_code == 429 or numeric_code >= 500:
                return True
        except ValueError:
            pass

        message = str(result.get("message") or "").lower()
        retryable_phrases = (
            "rate limit",
            "too frequent",
            "too many requests",
            "timeout",
            "timed out",
            "temporar",
            "network",
            "not login",
            "http error 429",
            "http error 5",
            "max retries",
        )
        return any(phrase in message for phrase in retryable_phrases)

    @staticmethod
    def _is_rate_limited_redemption_result(result: Dict) -> bool:
        """Return whether the upstream explicitly rate-limited this redemption."""
        raw_code = result.get("error_code")
        if raw_code is None and isinstance(result.get("error_details"), dict):
            raw_code = result["error_details"].get("err_code")

        code = str(raw_code or "").upper()
        if code in {"RATE_LIMITED", "40019", "429"}:
            return True

        message = str(result.get("message") or "").lower()
        return (
            "429" in message
            or "rate limit" in message
            or "too frequent" in message
            or "too many requests" in message
        )

    @staticmethod
    def _already_redeemed_result(player: Any, gift_code: str) -> Dict[str, Any]:
        return {
            "player_id": str(player.player_id),
            "player_name": player.player_name,
            "success": False,
            "message": f"Gift code `{gift_code}` was already redeemed for this player.",
            "error_code": "ALREADY_REDEEMED",
            "already_redeemed": True,
            "status_category": GiftCodeHandler.STATUS_ALREADY_REDEEMED,
            "should_log": False,
        }

    @staticmethod
    def _invalid_player_id_result(player: Any) -> Dict[str, Any]:
        return {
            "player_id": str(player.player_id),
            "player_name": player.player_name,
            "success": False,
            "message": "Invalid player ID format",
            "error_code": "INVALID_ID",
            "status_category": GiftCodeHandler.STATUS_INVALID_ID,
            "should_log": False,
        }

    @staticmethod
    def _player_lookup_failure_result(player: Any, message: str) -> Dict[str, Any]:
        return {
            "player_id": str(player.player_id),
            "player_name": getattr(player, "player_name", None),
            "success": False,
            "message": message,
            "error_code": "PLAYER_LOOKUP_FAILED",
            "status_category": GiftCodeHandler.STATUS_INVALID_ID,
            "should_log": False,
        }

    @staticmethod
    def _skipped_after_global_error_result(
        player: Any,
        global_error: Dict[str, Any],
    ) -> Dict[str, Any]:
        reason = str(global_error.get("message") or "The gift code was rejected.")
        return {
            "player_id": str(player.player_id),
            "player_name": getattr(player, "player_name", None),
            "success": False,
            "message": f"Skipped without an API request: {reason}",
            "error_code": "SKIPPED_GLOBAL_CODE_ERROR",
            "status_category": GiftCodeHandler.STATUS_SKIPPED,
            "skipped": True,
            "should_log": False,
        }

    @staticmethod
    def _cached_redemption_player(player: Any) -> dict[str, Any] | None:
        """Build a redemption profile from the registered player's database values."""
        player_id = str(player.player_id)
        cached_kingdom = getattr(player, "kingdom", None)
        if cached_kingdom in (None, "", 0, "0"):
            return None

        return {
            "playerId": player_id,
            "playerUid": getattr(player, "player_uid", None),
            "name": getattr(player, "player_name", None),
            "kingdom": str(cached_kingdom),
            "level": getattr(player, "castle_level", None),
        }

    async def _fetch_redemption_player_from_jeab(
        self,
        player: Any,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Fetch the current player profile from Jeab."""
        player_id = str(player.player_id)

        if self._kingshot_data_service is None:
            return None, self._player_lookup_failure_result(
                player,
                "Jeab player lookup is unavailable.",
            )

        try:
            response = await self._kingshot_data_service.get_player_by_fid(player_id)
            data = response.get("data") if isinstance(response, dict) else None
            if isinstance(response, dict) and response.get("success") is True and isinstance(data, dict):
                if data.get("error"):
                    return None, self._player_lookup_failure_result(
                        player,
                        f"Jeab could not resolve this player: {data['error']}",
                    )

                kingdom = data.get("kid")
                if kingdom not in (None, "", 0, "0"):
                    return {
                        "playerId": str(data.get("fid") or player_id),
                        "playerUid": str(data["uid"]) if data.get("uid") is not None else None,
                        "name": data.get("name") or getattr(player, "player_name", None),
                        "kingdom": str(kingdom),
                        "level": data.get("stove_lv") or data.get("lv"),
                    }, None

                lookup_error = "Jeab returned no kingdom for this player."
            else:
                lookup_error = str(
                    (response.get("error_message") if isinstance(response, dict) else None)
                    or (response.get("message") if isinstance(response, dict) else None)
                    or "Jeab player lookup failed."
                )
        except Exception as exc:
            lookup_error = f"Jeab player lookup failed: {exc}"
            logger.warning(
                "Jeab player lookup crashed for player %s: %s",
                player_id,
                exc,
                exc_info=True,
            )

        return None, self._player_lookup_failure_result(
            player,
            lookup_error,
        )

    async def _resolve_redemption_player(
        self,
        player: Any,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Use the database kingdom, fetching Jeab only when it is missing."""
        cached_profile = self._cached_redemption_player(player)
        if cached_profile is not None:
            return cached_profile, None

        return await self._fetch_redemption_player_from_jeab(player)

    @staticmethod
    def _is_kingdom_mismatch_result(result: Dict[str, Any]) -> bool:
        """Return whether CenturyGame rejected the supplied player/kingdom pair."""
        raw_code = result.get("error_code")
        if isinstance(result.get("error_details"), dict):
            upstream_code = result["error_details"].get("err_code")
        else:
            upstream_code = None

        api_status = " ".join(str(result.get("api_status") or "").strip().rstrip(".").upper().split())
        return (
            str(raw_code or "").upper() == "KINGDOM_MISMATCH"
            or str(upstream_code or "") == "40020"
            or api_status == "USER INFO ERROR"
        )

    def _build_metadata_rows_from_result(
        self,
        player_id: str,
        redemption_result: Dict[str, Any],
        added_by_user_id: int,
    ) -> list[dict[str, Any]]:
        player_profile = redemption_result.get("player_profile")
        if not isinstance(player_profile, dict):
            return []

        resolved_player_id = str(player_profile.get("playerId") or player_id)
        resolved_player_uid = (
            str(player_profile.get("playerUid") or player_profile.get("uid"))
            if (player_profile.get("playerUid") or player_profile.get("uid")) is not None
            else None
        )
        resolved_name = player_profile.get("name")
        resolved_kingdom = str(player_profile.get("kingdom")) if player_profile.get("kingdom") is not None else None
        resolved_castle_level = (
            str(player_profile.get("level")) if player_profile.get("level") is not None else None
        )

        rows = [
            {
                "player_id": resolved_player_id,
                "player_uid": resolved_player_uid,
                "player_name": resolved_name,
                "kingdom": resolved_kingdom,
                "castle_level": resolved_castle_level,
                "added_by_user_id": added_by_user_id,
            }
        ]

        if resolved_player_id != str(player_id):
            rows.append(
                {
                    "player_id": str(player_id),
                    "player_name": resolved_name,
                    "kingdom": resolved_kingdom,
                    "castle_level": resolved_castle_level,
                }
            )

        return rows

    async def _persist_bulk_redemption_results(
        self,
        *,
        gift_code: str,
        results: list[dict[str, Any]],
        actor_user_id: int,
        guild_id: int | None,
        channel_id: int | None,
    ) -> None:
        metadata_rows: list[dict[str, Any]] = []
        log_rows: list[dict[str, Any]] = []

        for result in results:
            player_id = str(result["player_id"])
            metadata_rows.extend(
                self._build_metadata_rows_from_result(
                    player_id=player_id,
                    redemption_result=result,
                    added_by_user_id=actor_user_id,
                )
            )

            if result.get("should_log", True):
                log_rows.append(
                    {
                        "user_id": actor_user_id,
                        "player_id": player_id,
                        "gift_code": gift_code,
                        "success": result.get("success", False),
                        "response_message": result.get("message"),
                        "error_code": result.get("error_code"),
                        "guild_id": guild_id,
                        "channel_id": channel_id,
                    }
                )

        if metadata_rows:
            await self._tracking_service.sync_player_metadata_many(metadata_rows)

        if log_rows:
            await self._tracking_service.log_gift_code_redemptions_many(log_rows)

    async def _run_bulk_redemption(
        self,
        *,
        gift_code: str,
        registered_players: list[Any],
        actor_user_id: int,
        guild_id: int | None,
        channel_id: int | None,
        progress: Callable[[list[dict], str], Awaitable[None]] | None = None,
    ) -> list[dict[str, Any]]:
        """Redeem one code in bounded batches and stop on global code errors."""
        already_redeemed = await self._gift_code_service.get_redeemed_players(None, gift_code)
        indexed_results: list[tuple[int, dict[str, Any]]] = []
        pending: list[tuple[int, Any, int]] = []

        for index, player in enumerate(registered_players):
            player_id = str(player.player_id)
            if player_id in already_redeemed:
                indexed_results.append((index, self._already_redeemed_result(player, gift_code)))
                continue

            try:
                player_id_int = int(player_id)
            except ValueError:
                logger.error("Invalid player ID format during bulk redeem: %s", player_id)
                indexed_results.append((index, self._invalid_player_id_result(player)))
                continue

            pending.append((index, player, player_id_int))

        waiting: dict[str, float] = {}

        async def report(phase: str = "Redeeming players") -> None:
            if progress is not None:
                if waiting:
                    phase = f"Rate limited · retry <t:{int(max(waiting.values()))}:R> · {len(waiting)} waiting"
                await progress([result for _, result in indexed_results], phase)

        async def rate_limit_wait(player_id: str, delay: float) -> None:
            if delay:
                waiting[player_id] = datetime.now(timezone.utc).timestamp() + delay
            else:
                waiting.pop(player_id, None)
            await report()

        await report()

        async def redeem_one(index: int, player: Any, player_id_int: int) -> tuple[int, dict[str, Any]]:
            cached_profile = self._cached_redemption_player(player)
            player_profile, lookup_failure = await self._resolve_redemption_player(player)
            if lookup_failure is not None:
                return index, lookup_failure

            assert player_profile is not None
            result = await self._redeem_with_retries(
                player_id_int=player_id_int,
                kingdom_id=str(player_profile["kingdom"]),
                gift_code=gift_code,
                player_id_for_logs=str(player.player_id),
                rate_limit_wait=rate_limit_wait,
            )

            # A transfer makes the database kingdom stale. Only refresh Jeab after
            # CenturyGame rejects a cached player/kingdom pair, then retry once with
            # the newly reported kingdom.
            if cached_profile is not None and self._is_kingdom_mismatch_result(result):
                refreshed_profile, refresh_failure = await self._fetch_redemption_player_from_jeab(player)
                if refreshed_profile is not None:
                    cached_kingdom = str(player_profile["kingdom"])
                    refreshed_kingdom = str(refreshed_profile["kingdom"])
                    player_profile = refreshed_profile

                    if refreshed_kingdom != cached_kingdom:
                        logger.info(
                            "Player %s moved from kingdom %s to %s; retrying gift code '%s' once",
                            player.player_id,
                            cached_kingdom,
                            refreshed_kingdom,
                            gift_code,
                        )
                        first_attempts = int(result.get("attempts") or 1)
                        result = await self._redeem_with_retries(
                            player_id_int=player_id_int,
                            kingdom_id=refreshed_kingdom,
                            gift_code=gift_code,
                            player_id_for_logs=str(player.player_id),
                            rate_limit_wait=rate_limit_wait,
                        )
                        total_attempts = first_attempts + int(result.get("attempts") or 1)
                        result = {
                            **result,
                            "attempts": total_attempts,
                            "retries": max(0, total_attempts - 1),
                            "kingdom_refreshed": True,
                            "previous_kingdom": cached_kingdom,
                            "refreshed_kingdom": refreshed_kingdom,
                        }
                    else:
                        logger.warning(
                            "CenturyGame rejected kingdom %s for player %s, but Jeab returned the same kingdom",
                            cached_kingdom,
                            player.player_id,
                        )
                else:
                    logger.warning(
                        "CenturyGame rejected cached kingdom %s for player %s and Jeab refresh failed: %s",
                        player_profile["kingdom"],
                        player.player_id,
                        refresh_failure.get("message") if refresh_failure else "unknown error",
                    )

            normalized_result = {
                "player_id": str(player.player_id),
                "player_name": player_profile.get("name") or getattr(player, "player_name", None),
                "success": result.get("success", False),
                "message": result.get("message", "Unknown error"),
                "error_code": result.get("error_code"),
                "api_status": result.get("api_status"),
                "global_code_error": result.get("global_code_error", False),
                "already_redeemed": result.get("already_redeemed", False),
                "already_redeemed_by_api": result.get("already_redeemed_by_api", False),
                "status_category": self._categorize_redemption_status(result),
                "player_profile": player_profile,
                "attempts": result.get("attempts"),
                "retries": result.get("retries", 0),
                "redemption_attempted": True,
                "should_log": True,
            }
            return index, normalized_result

        async def redeem_and_report(item: tuple[int, Any, int]) -> tuple[int, dict[str, Any]]:
            result = await redeem_one(*item)
            indexed_results.append(result)
            await report()
            return result

        next_pending_index = 0
        global_error: dict[str, Any] | None = None

        # Probe one usable player before scheduling bulk work. A bad, expired, or
        # exhausted code therefore costs one redemption request, not 500+.
        while next_pending_index < len(pending):
            probe_result = await redeem_and_report(pending[next_pending_index])
            next_pending_index += 1

            if self._is_global_code_rejection(probe_result[1]):
                global_error = probe_result[1]
                break
            if probe_result[1].get("redemption_attempted"):
                break

        if global_error is None:
            while next_pending_index < len(pending):
                batch = pending[next_pending_index : next_pending_index + self.REDEEM_CONCURRENCY]
                batch_results = await asyncio.gather(*(redeem_and_report(item) for item in batch))
                next_pending_index += len(batch)

                global_error = next(
                    (result for _, result in batch_results if self._is_global_code_rejection(result)),
                    None,
                )
                if global_error is not None:
                    break

        if global_error is not None and next_pending_index < len(pending):
            skipped_count = len(pending) - next_pending_index
            logger.warning(
                "Stopping bulk redemption for code '%s' after global rejection %s; skipping %s queued players",
                gift_code,
                global_error.get("error_code"),
                skipped_count,
            )
            indexed_results.extend(
                (index, self._skipped_after_global_error_result(player, global_error))
                for index, player, _ in pending[next_pending_index:]
            )

        results = [result for _, result in sorted(indexed_results, key=lambda item: item[0])]
        await report("Saving redemption results")
        await self._persist_bulk_redemption_results(
            gift_code=gift_code,
            results=results,
            actor_user_id=actor_user_id,
            guild_id=guild_id,
            channel_id=channel_id,
        )
        return results

    async def _redeem_with_retries(
        self,
        player_id_int: int,
        kingdom_id: str,
        gift_code: str,
        player_id_for_logs: str,
        rate_limit_wait: Callable[[str, float], Awaitable[None]] | None = None,
    ) -> Dict:
        """Redeem a code with retry for transient/API failures only."""
        standard_max_attempts = self.REDEEM_MAX_RETRIES + 1
        last_result: Dict = {
            "success": False,
            "message": "Unexpected error occurred",
            "error_code": "UNEXPECTED_ERROR",
        }

        attempt = 0
        while True:
            attempt += 1
            try:
                last_result = await self._gift_code_service.redeem_gift_code_remote(
                    player_id_int,
                    gift_code,
                    kingdom_id=kingdom_id,
                )
            except Exception as exc:
                logger.error(
                    "Redeem attempt %s/%s crashed for player %s and code '%s': %s",
                    attempt,
                    standard_max_attempts,
                    player_id_for_logs,
                    gift_code,
                    exc,
                    exc_info=True,
                )
                last_result = {
                    "success": False,
                    "message": "Unexpected error occurred",
                    "error_code": "UNEXPECTED_ERROR",
                }

            is_rate_limited = self._is_rate_limited_redemption_result(last_result)
            max_attempts = (
                self.REDEEM_RATE_LIMIT_MAX_RETRIES + 1
                if is_rate_limited
                else standard_max_attempts
            )

            if not self._is_retryable_redemption_result(last_result) or attempt >= max_attempts:
                normalized_result = dict(last_result)
                normalized_result.setdefault("attempts", attempt)
                normalized_result.setdefault("retries", max(0, attempt - 1))
                return normalized_result

            if is_rate_limited:
                retry_delay = min(
                    self.REDEEM_RATE_LIMIT_MAX_DELAY_SECONDS,
                    self.REDEEM_RATE_LIMIT_DELAY_SECONDS * attempt,
                )
                retry_delay += random.uniform(1.0, 4.0)
            else:
                retry_delay = min(
                    self.REDEEM_RETRY_MAX_DELAY_SECONDS,
                    self.REDEEM_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)),
                )
                retry_delay += random.uniform(0, 0.5)

            logger.warning(
                "Redeem attempt %s/%s failed for player %s and code '%s' with retryable status. "
                "Retrying in %.1fs (error_code=%s, message=%s)",
                attempt,
                max_attempts,
                player_id_for_logs,
                gift_code,
                retry_delay,
                last_result.get("error_code"),
                last_result.get("message"),
            )
            if is_rate_limited and rate_limit_wait is not None:
                await rate_limit_wait(player_id_for_logs, retry_delay)
            try:
                await asyncio.sleep(retry_delay)
            finally:
                if is_rate_limited and rate_limit_wait is not None:
                    await rate_limit_wait(player_id_for_logs, 0)

    def _build_player_lines(self, players: List) -> List[str]:
        """Format bounded, labeled player rows; missing data stays unavailable."""
        lines = []
        for player in players:
            status = "Enabled" if player.enabled else "Disabled"
            name = discord.utils.escape_markdown(str(player.player_name or "Unnamed player")[:80])
            kingdom = getattr(player, "kingdom", None) or "Unavailable"
            castle = getattr(player, "castle_level", None)
            castle = "Unavailable" if castle in (None, "") else castle
            lines.append(f"**{name}** · `{player.player_id}` · {status}\nKingdom: {kingdom} · Castle: {castle}")
        return lines

    def _chunk_lines(self, lines: List[str], page_size: int) -> List[List[str]]:
        """Split lines into fixed-size pages."""
        return [lines[idx : idx + page_size] for idx in range(0, len(lines), page_size)]

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
        if not alliances:
            pending = (
                self._alliance_refresh_tasks.get(normalized_kid)
                or self._alliance_completion_tasks.get(normalized_kid)
            )
            if pending is not None and not pending.done():
                return [app_commands.Choice(
                    name=f"Loading kingdom {normalized_kid} alliances… reopen this field shortly",
                    value=f"loading:{normalized_kid}",
                )]
            return []

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

    async def _sync_player_metadata_from_lookup(self, player_id: str, player_info: Optional[Dict]) -> None:
        """Refresh registered player metadata when a player lookup succeeds."""
        if not player_info:
            return

        resolved_player_id = str(player_info.get("playerId") or player_id)
        resolved_player_uid = (
            str(player_info.get("playerUid") or player_info.get("uid"))
            if (player_info.get("playerUid") or player_info.get("uid")) is not None
            else None
        )
        resolved_name = player_info.get("name")
        resolved_kingdom = str(player_info.get("kingdom")) if player_info.get("kingdom") is not None else None
        resolved_castle_level = (
            str(player_info.get("levelRenderedDetailed") or player_info.get("level"))
            if (player_info.get("levelRenderedDetailed") or player_info.get("level") is not None)
            else None
        )

        await self._tracking_service.sync_player_metadata(
            player_id=resolved_player_id,
            player_uid=resolved_player_uid,
            player_name=resolved_name,
            kingdom=resolved_kingdom,
            castle_level=resolved_castle_level,
        )

    async def _sync_player_metadata_from_redemption_result(self, player_id: str, redemption_result: Dict, added_by_user_id: int) -> None:
        """Refresh player metadata from redeem response and upsert when needed."""
        player_profile = redemption_result.get("player_profile")
        if not isinstance(player_profile, dict):
            return

        resolved_player_id = str(player_profile.get("playerId") or player_id)
        resolved_player_uid = (
            str(player_profile.get("playerUid") or player_profile.get("uid"))
            if (player_profile.get("playerUid") or player_profile.get("uid")) is not None
            else None
        )
        resolved_name = player_profile.get("name")
        resolved_kingdom = str(player_profile.get("kingdom")) if player_profile.get("kingdom") is not None else None
        resolved_castle_level = (
            str(player_profile.get("level")) if player_profile.get("level") is not None else None
        )

        await self._tracking_service.sync_player_metadata(
            player_id=resolved_player_id,
            player_uid=resolved_player_uid,
            player_name=resolved_name,
            kingdom=resolved_kingdom,
            castle_level=resolved_castle_level,
            added_by_user_id=added_by_user_id,
        )

        # If an old/non-canonical player ID exists in the table, keep it refreshed too.
        if resolved_player_id != str(player_id):
            await self._tracking_service.sync_player_metadata(
                player_id=str(player_id),
                player_name=resolved_name,
                kingdom=resolved_kingdom,
                castle_level=resolved_castle_level,
            )

    async def _handle_add_player_slash(self, interaction: discord.Interaction, player_ids: str):
        """Handle adding one or more players to the redemption list."""
        await interaction.response.defer(thinking=True)

        try:
            # Parse multiple comma-separated IDs
            import re

            raw_ids = [pid.strip() for pid in re.split(r'[,\s]+', player_ids) if pid.strip()]
            if not raw_ids:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="❌ Invalid Input",
                        description="No valid player IDs provided.",
                        color=EmbedColors.ERROR,
                    )
                )
                return

            await self._tracking_service.track_user(
                session=None,
                user_id=interaction.user.id,
                username=interaction.user.name,
                discriminator=interaction.user.discriminator,
                display_name=interaction.user.display_name,
            )

            added_players = []
            not_found_players = []

            for pid in raw_ids:
                # Validate player exists via PlayerInfoService
                player_info = await self._player_info_service.get_player_info(pid)
                if player_info is None:
                    not_found_players.append(pid)
                    logger.warning(f"Attempt to add non-existent player ID {pid}")
                    continue

                await self._sync_player_metadata_from_lookup(pid, player_info)

                # Use API-provided name only
                resolved_player_id = str(player_info.get("playerId") or pid)
                resolved_player_uid = (
                    str(player_info.get("playerUid") or player_info.get("uid"))
                    if (player_info.get("playerUid") or player_info.get("uid")) is not None
                    else None
                )
                resolved_name = player_info.get("name")
                resolved_kingdom = str(player_info.get("kingdom")) if player_info.get("kingdom") is not None else None
                resolved_castle_level = (
                    str(player_info.get("levelRenderedDetailed") or player_info.get("level"))
                    if (player_info.get("levelRenderedDetailed") or player_info.get("level") is not None)
                    else None
                )

                await self._player_registry_service.add_registered_player(
                    player_id=resolved_player_id,
                    added_by_user_id=interaction.user.id,
                    player_uid=resolved_player_uid,
                    player_name=resolved_name,
                    kingdom=resolved_kingdom,
                    castle_level=resolved_castle_level,
                    enabled=True,
                )

                added_players.append(f"`{resolved_player_id}`" + (f" ({resolved_name})" if resolved_name else ""))
                logger.info(f"Player {resolved_player_id} added by {interaction.user.id}")

            # Build final response embed
            if not added_players and not_found_players:
                embed = discord.Embed(
                    title="❌ Players Not Found",
                    description=f"Could not find any of the provided player IDs: {', '.join('`' + p + '`' for p in not_found_players)}\nPlease verify the IDs in-game and try again.",
                    color=EmbedColors.ERROR,
                )
                await interaction.followup.send(embed=embed)
                return

            embed = discord.Embed(
                title=f"✅ {len(added_players)} Player(s) Added Successfully",
                description="Player profiles saved and enabled for gift code redemption.",
                color=EmbedColors.SUCCESS,
            )
            
            # Use chunks so we don't hit max limit of discord fields
            chunk_size = 10
            for i in range(0, len(added_players), chunk_size):
                chunk = added_players[i:i+chunk_size]
                embed.add_field(name=f"Added Players ({i+1}-{i+len(chunk)})", value="\n".join(chunk), inline=False)
                
            if not_found_players:
                embed.add_field(
                    name="❌ Not Found",
                    value=", ".join(not_found_players),
                    inline=False
                )

            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Error adding players {player_ids}: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Could Not Add Players",
                    description="An error occurred while adding the players. Please check logs.",
                    color=EmbedColors.ERROR,
                )
            )

    async def _handle_add_alliance_choice_slash(
        self,
        interaction: discord.Interaction,
        kid: int,
        alliance: str,
    ) -> None:
        """Validate a native autocomplete selection, then import its roster."""
        await interaction.response.defer(thinking=True)

        if isinstance(kid, bool) or kid <= 0:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Invalid Kingdom",
                    description="Enter a positive kingdom ID.",
                    color=discord.Color.red(),
                )
            )
            return

        if self._kingshot_data_service is None:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ KingShot Data API Not Configured",
                    description="Alliance discovery requires the KingShot Data API service.",
                    color=discord.Color.red(),
                )
            )
            return

        selection = alliance.strip() if isinstance(alliance, str) else ""
        if selection == f"loading:{kid}":
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="Alliances Still Loading",
                    description="Reopen the alliance field in a moment to choose from the list.",
                    color=discord.Color.orange(),
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
                            color=discord.Color.orange(),
                        )
                    )
                    return
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="❌ Could Not Inspect Kingdom",
                        description="Could not load alliances from the KingShot Data API.",
                        color=discord.Color.red(),
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
                    color=discord.Color.orange(),
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
                    color=discord.Color.red(),
                )
            )
            return

        aid = str(selected.get("aid", "")).strip()
        if not aid.isdigit() or int(aid) <= 0:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Invalid Alliance",
                    description="The selected alliance does not have a valid internal ID.",
                    color=discord.Color.red(),
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
        """Handle adding all fid-bearing alliance roster members to the redemption list."""
        if defer_response:
            await interaction.response.defer(thinking=True)

        if not aid or kid <= 0:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Invalid Input",
                    description="`aid` and positive `kid` are required.",
                    color=discord.Color.red(),
                )
            )
            return

        if self._kingshot_data_service is None:
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ KingShot Data API Not Configured",
                    description="Alliance import requires the KingShot Data API service.",
                    color=discord.Color.red(),
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
                        description=result.get("error_message", "KingShot Data API request failed."),
                        color=discord.Color.red(),
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
                        color=discord.Color.orange(),
                    )
                )
                return

            added_players: List[str] = []
            skipped_members: List[str] = []
            seen_fids: set[str] = set()
            cached_uid_count = 0
            resolved_uid_count = 0
            alternate_uid_count = 0
            players_to_add: List[Dict[str, Any]] = []
            cached_enabled_fids: set[str] = set()
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
                        if getattr(cached_player, "enabled", False):
                            cached_enabled_fids.add(fid)
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

                if fid not in cached_enabled_fids:
                    players_to_add.append(
                        {
                            "player_id": fid,
                            "added_by_user_id": interaction.user.id,
                            "player_uid": member_uid,
                            "player_name": member_name,
                            "kingdom": member_kingdom,
                            "castle_level": castle_level,
                            "enabled": True,
                        }
                    )
                added_players.append(f"`{fid}`" + (f" ({member_name})" if member_name else ""))

            if players_to_add:
                await self._player_registry_service.add_registered_players(players_to_add)

            alliance_name = self._extract_alliance_name(payload) or alternate_alliance_name or alliance_label or f"Alliance {aid}"
            if added_players:
                title = f"✅ Added {len(added_players)} Alliance Member(s)"
                description = f"Roster imported from **{alliance_name}** in kingdom `{kid}`."
            else:
                title = "⚠️ No Governor IDs Available"
                description = (
                    f"The roster for **{alliance_name}** in kingdom `{kid}` contains internal IDs, "
                    "but no connected source supplied Governor IDs. Ask a bot admin to enable "
                    "the alliance roster source, or use `/addplayer` with Governor IDs."
                )
            embed = discord.Embed(
                title=title,
                description=description,
                color=discord.Color.green() if added_players else discord.Color.orange(),
            )

            for idx, chunk in enumerate(self._chunk_lines(added_players, 10), start=1):
                embed.add_field(name=f"Added Players ({idx})", value="\n".join(chunk), inline=False)

            if skipped_members:
                skipped_preview = ", ".join(f"`{value}`" for value in skipped_members[:20])
                if len(skipped_members) > 20:
                    skipped_preview += f", ...and {len(skipped_members) - 20} more"
                embed.add_field(
                    name="Skipped",
                    value=(
                        f"{len(skipped_members)} member(s) could not be mapped to a `fid`, "
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
                    description="An error occurred while importing the alliance roster. Please check logs.",
                    color=discord.Color.red(),
                )
            )

    async def _handle_remove_player_slash(self, interaction: discord.Interaction, player_id: str):
        """Handle removing a player from the redemption list."""
        await interaction.response.defer(thinking=True)

        try:
            # Fetch player to check ownership
            player = await self._player_registry_service.get_registered_player(player_id)

            if not player:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="❌ Player Not Found",
                        description=f"Player `{player_id}` is not in the player list.",
                        color=EmbedColors.ERROR,
                    )
                )
                return

            # Determine admin status (guild context only)
            is_admin = False
            if interaction.guild and interaction.user.guild_permissions:
                is_admin = bool(interaction.user.guild_permissions.administrator)

            # Check ownership or admin rights
            if player.added_by_user_id != interaction.user.id and not is_admin:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="⛔ Permission Denied",
                        description="You can only remove players you added, unless you are a server admin.",
                        color=EmbedColors.WARNING,
                    )
                )
                return

            # Proceed with removal
            removed = await self._player_registry_service.remove_registered_player(player_id)

            if removed:
                embed = discord.Embed(
                    title="✅ Player Removed",
                    description=f"Player `{player_id}` has been removed from the gift code redemption list.",
                    color=EmbedColors.SUCCESS,
                )
                await interaction.followup.send(embed=embed)
                logger.info(f"Player {player_id} removed by {interaction.user.id} (admin={is_admin})")
            else:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="❌ Player Not Found",
                        description=f"Player `{player_id}` is not in the player list.",
                        color=EmbedColors.ERROR,
                    )
                )

        except Exception as e:
            logger.error(f"Error removing player {player_id}: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Could Not Remove Player",
                    description="An error occurred while removing the player.",
                    color=EmbedColors.ERROR,
                )
            )

    async def _handle_list_players_slash(self, interaction: discord.Interaction):
        """Handle listing all registered players."""
        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            all_players = await self._player_registry_service.get_registered_players(enabled_only=False)

            if not all_players:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="📋 No Players Found",
                        description="No player profiles are available yet.",
                        color=EmbedColors.INFO,
                    ),
                    ephemeral=True,
                )
                return

            ordered_players = sorted(all_players, key=lambda player: not player.enabled)
            view = PlayerListPaginationView(self, ordered_players, interaction.user.id)
            view.message = await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True, wait=True)

        except Exception as e:
            logger.error(f"Error listing players: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Could Not List Players",
                    description="An error occurred while retrieving the player list.",
                    color=EmbedColors.ERROR,
                ),
                ephemeral=True,
            )

    @staticmethod
    def _build_status_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
        """Build a consistent status embed for command responses."""
        return build_status_embed(title=title, description=description, color=color)
