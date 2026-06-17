import asyncio
import socket
import logging
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config.bot_config import BotConfig
from services.gift_code_service import IGiftCodeService
from services.player_info_service import IPlayerInfoService
from services.interaction_tracking_service import InteractionTrackingService
from services.player_registry_service import PlayerRegistryService
from services.kingshot_data_service import KingshotDataService

logger = logging.getLogger(__name__)


class PlayerListPaginationView(discord.ui.View):
    """Single-message pagination for registered player list embeds."""

    def __init__(
        self,
        pages: List[List[str]],
        total_players: int,
        enabled_count: int,
        disabled_count: int,
        author_id: int,
        timeout: float = 180.0,
    ):
        super().__init__(timeout=timeout)
        self.pages = pages
        self.total_players = total_players
        self.enabled_count = enabled_count
        self.disabled_count = disabled_count
        self.author_id = author_id
        self.current_page = 0
        self.message: Optional[discord.Message] = None
        self._update_button_state()

    def _update_button_state(self) -> None:
        is_first = self.current_page == 0
        is_last = self.current_page >= len(self.pages) - 1
        self.prev_button.disabled = is_first
        self.next_button.disabled = is_last

    def build_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="📋 Player Profiles",
            description=(
                f"**Total:** {self.total_players} | **Enabled:** {self.enabled_count} | "
                f"**Disabled:** {self.disabled_count}\n"
                f"**Page:** {self.current_page + 1}/{len(self.pages)}"
            ),
            color=discord.Color.blue(),
        )
        embed.add_field(name="Players", value="\n".join(self.pages[self.current_page]), inline=False)
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the command user can control this pagination.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="←", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page -= 1
        self._update_button_state()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="→", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_page += 1
        self._update_button_state()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self) -> None:
        self.prev_button.disabled = True
        self.next_button.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except Exception:
                logger.debug("Failed to disable pagination buttons after timeout", exc_info=True)


class GiftCodeHandler:
    """Handles gift code redemption Discord commands."""

    STATUS_SUCCESS = "success"
    STATUS_ALREADY_REDEEMED = "already_redeemed"
    STATUS_API_REJECTED = "api_rejected"
    STATUS_INVALID_ID = "invalid_id"
    REDEEM_MAX_RETRIES = 2
    REDEEM_RETRY_DELAY_SECONDS = 1.0
    REDEEM_RETRY_MAX_DELAY_SECONDS = 30.0
    REDEEM_RATE_LIMIT_DELAY_SECONDS = 8.0
    REDEEM_RATE_LIMIT_MAX_DELAY_SECONDS = 45.0
    REDEEM_CONCURRENCY = 3

    def __init__(
        self,
        gift_code_service: IGiftCodeService,
        player_info_service: IPlayerInfoService,
        bot: commands.Bot,
        config: BotConfig,
        interaction_tracking_service: InteractionTrackingService | None = None,
        player_registry_service: PlayerRegistryService | None = None,
        kingshot_data_service: KingshotDataService | None = None,
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
        self._polling_loop = None
        self._poll_backoff_until: datetime | None = None
        self._poll_backoff_delta = timedelta(minutes=2)
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
            color=discord.Color.orange(),
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    def register_commands(self):
        """Register all gift code commands with the bot."""

        @self._bot.tree.command(name="redeem", description="Redeem a gift code for all registered players")
        @app_commands.describe(gift_code="The gift code to redeem (e.g., KINGSHOTXMAS)")
        async def redeem_gift_code(interaction: discord.Interaction, gift_code: str):
            """Redeem a gift code for all registered players."""
            await self._handle_redeem_gift_code_slash(interaction, gift_code)

        @self._bot.tree.command(name="addplayer", description="Add one or more players to gift code redemption list")
        @app_commands.describe(player_ids="The player ID(s) to add, comma-separated")
        async def add_player(interaction: discord.Interaction, player_ids: str):
            """Add a player to gift code list using API name."""
            await self._handle_add_player_slash(interaction, player_ids)

        @self._bot.tree.command(name="addalliance", description="Add all alliance members to gift code redemption list")
        @app_commands.describe(
            aid="Alliance ID",
            kid="Kingdom ID",
        )
        async def add_alliance(interaction: discord.Interaction, aid: str, kid: int):
            """Add alliance roster members to the gift code list."""
            await self._handle_add_alliance_slash(interaction, aid.strip(), kid)

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
            logger.info("Polling for new gift codes...")

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

                        success_count = len(
                            [result for result in results if result.get("status_category") == self.STATUS_SUCCESS]
                        )
                        already_redeemed_count = len(
                            [
                                result
                                for result in results
                                if result.get("status_category") == self.STATUS_ALREADY_REDEEMED
                            ]
                        )
                        api_rejected_count = len(
                            [
                                result
                                for result in results
                                if result.get("status_category") == self.STATUS_API_REJECTED
                            ]
                        )
                        invalid_id_count = len(
                            [result for result in results if result.get("status_category") == self.STATUS_INVALID_ID]
                        )

                        # Send Discord announcement if channels are configured
                        if self._config.auto_redeem_channels:
                            embed = discord.Embed(
                                title="🎁 New Gift Code Found!",
                                description="Auto-redemption triggered for newly discovered gift code.",
                                color=discord.Color.brand_green(),
                            )
                            embed.add_field(name="Gift Code", value=f"`{new_code}`", inline=False)
                            embed.add_field(
                                name="Auto-Redeem Status",
                                value=(
                                    f"✅ **Success**: {success_count}\n"
                                    f"🔄 **Already Claimed**: {already_redeemed_count}\n"
                                    f"🚫 **API Rejected**: {api_rejected_count}\n"
                                    f"🆔 **Invalid ID**: {invalid_id_count}\n"
                                    f"👥 **Total Players**: {len(registered_players)}"
                                ),
                                inline=False,
                            )
                            embed.set_footer(text="Check in-game mail for successfully redeemed codes!")

                            for channel_id in self._config.auto_redeem_channels:
                                channel = self._bot.get_channel(channel_id)
                                if channel and isinstance(channel, discord.TextChannel):
                                    try:
                                        await channel.send(embed=embed)
                                        logger.info(f"Announced gift code {new_code} in channel {channel_id}")
                                    except Exception as e:
                                        logger.error(
                                            f"Failed to send gift code announcement to channel {channel_id}: {e}"
                                        )
                                else:
                                    logger.warning(
                                        f"Configured auto-redeem channel {channel_id} not found or is not a text channel"
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

    async def _handle_list_gift_codes_slash(self, interaction: discord.Interaction):
        """Handle listing available gift codes."""
        await interaction.response.defer(thinking=True)

        try:
            response = await self._gift_code_service.get_available_gift_codes()

            if not response.get("success"):
                embed = self._build_status_embed(
                    title="❌ Could Not Fetch Gift Codes",
                    description="The gift code list could not be retrieved.",
                    color=discord.Color.red(),
                )
                embed.add_field(name="Details", value=str(response.get("message", "Unknown error")), inline=False)
                await interaction.followup.send(embed=embed)
                return

            codes = response.get("data", [])

            if not codes:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="📋 No Active Gift Codes",
                        description="No currently active gift codes were found.",
                        color=discord.Color.blue(),
                    )
                )
                return

            embed = discord.Embed(
                title="🎁 Active Gift Codes",
                description="List of available gift codes to redeem",
                color=discord.Color.green(),
            )

            for code in codes:
                code_str = code.get("code", "UNKNOWN")
                expires_at = code.get("expiresAt")

                value = "`" + code_str + "`"
                if expires_at:
                    try:
                        # Attempt to parse ISO string and format
                        dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                        value += f"\nExpires: <t:{int(dt.timestamp())}:R>"
                    except ValueError:
                        value += f"\nExpires: {expires_at}"
                else:
                    value += "\nNo expiration"

                embed.add_field(name="Gift Code", value=value, inline=False)

            embed.set_footer(text="Use /redeem to run manual redemptions or wait for auto-redeem")
            await interaction.followup.send(embed=embed)

        except Exception as e:
            logger.error(f"Error listing gift codes: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Unexpected Error",
                    description="An unexpected error occurred while fetching gift codes.",
                    color=discord.Color.red(),
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

        # Get all registered players
        try:
            await self._tracking_service.track_user(
                session=None,
                user_id=interaction.user.id,
                username=interaction.user.name,
                discriminator=interaction.user.discriminator,
                display_name=interaction.user.display_name,
            )

            registered_players = await self._player_registry_service.get_registered_players(enabled_only=True)

            if not registered_players:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="📭 No Enabled Players",
                        description="Use `/addplayer <player_id>` to enable at least one player before redeeming.",
                        color=discord.Color.orange(),
                    )
                )
                return

            results = await self._run_bulk_redemption(
                gift_code=gift_code,
                registered_players=registered_players,
                actor_user_id=interaction.user.id,
                guild_id=interaction.guild.id if interaction.guild else None,
                channel_id=interaction.channel.id if interaction.channel else None,
            )

            # Format and send results
            await self._send_redemption_results_slash(interaction, gift_code, results)

        except Exception as e:
            logger.error(f"Error in bulk redemption: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Redemption Failed",
                    description="An unexpected error occurred while processing redemption. Please try again later.",
                    color=discord.Color.red(),
                )
            )

    async def _send_redemption_results_slash(
        self,
        interaction: discord.Interaction,
        gift_code: str,
        results: List[Dict],
    ):
        """Send formatted redemption results."""
        success_results = [r for r in results if r.get("status_category") == self.STATUS_SUCCESS]
        already_redeemed_results = [r for r in results if r.get("status_category") == self.STATUS_ALREADY_REDEEMED]
        api_rejected_results = [r for r in results if r.get("status_category") == self.STATUS_API_REJECTED]
        invalid_id_results = [r for r in results if r.get("status_category") == self.STATUS_INVALID_ID]

        success_count = len(success_results)
        already_redeemed_count = len(already_redeemed_results)
        api_rejected_count = len(api_rejected_results)
        invalid_id_count = len(invalid_id_results)
        total_count = len(results)

        # Create embed
        if success_count == total_count:
            color = discord.Color.green()
            title = "✅ All Gift Codes Redeemed Successfully!"
        elif success_count > 0:
            color = discord.Color.gold()
            title = "⚠️ Gift Code Redemption Completed"
        else:
            color = discord.Color.red()
            title = "❌ All Gift Code Redemptions Failed"

        embed = discord.Embed(
            title=title,
            description=f"**Gift Code:** `{gift_code}`\n"
            f"**✅ Success:** {success_count}/{total_count}\n"
            f"**🔄 Already Redeemed:** {already_redeemed_count}/{total_count}\n"
            f"**🚫 API Rejected:** {api_rejected_count}/{total_count}\n"
            f"**🆔 Invalid ID:** {invalid_id_count}/{total_count}",
            color=color,
        )

        if success_results:
            embed.add_field(
                name="✅ Success",
                value=self._format_result_lines(success_results, "✅"),
                inline=False,
            )

        if already_redeemed_results:
            embed.add_field(
                name="🔄 Already Redeemed",
                value=self._format_result_lines(already_redeemed_results, "🔄"),
                inline=False,
            )

        if api_rejected_results:
            embed.add_field(
                name="🚫 API Rejected",
                value=self._format_result_lines(api_rejected_results, "🚫"),
                inline=False,
            )

        if invalid_id_results:
            embed.add_field(
                name="🆔 Invalid ID",
                value=self._format_result_lines(invalid_id_results, "🆔"),
                inline=False,
            )

        embed.set_footer(
            text=(
                f"🎮 Check in-game mail for successful claims • "
                f"Retry policy: {self.REDEEM_MAX_RETRIES} transient retries; "
                "429 retries until the rate limit clears"
            )
        )

        await interaction.followup.send(embed=embed)
        logger.info(
            "Bulk redemption completed: success=%s, already_redeemed=%s, api_rejected=%s, invalid_id=%s",
            success_count,
            already_redeemed_count,
            api_rejected_count,
            invalid_id_count,
        )

    def _categorize_redemption_status(self, result: Dict) -> str:
        """Map API/database redemption result into a single status category."""
        if result.get("success", False):
            return self.STATUS_SUCCESS

        if (
            result.get("already_redeemed", False)
            or result.get("already_redeemed_by_api", False)
            or result.get("error_code") in {"ALREADY_REDEEMED", "ALREADY_REDEEMED_BY_API"}
        ):
            return self.STATUS_ALREADY_REDEEMED

        if result.get("error_code") in {"INVALID_ID", "INVALID_USER_ID", "INVALID_PLAYER_ID"}:
            return self.STATUS_INVALID_ID

        return self.STATUS_API_REJECTED

    def _is_retryable_redemption_result(self, result: Dict) -> bool:
        """Return whether a failed redemption looks transient enough to retry."""
        if self._categorize_redemption_status(result) != self.STATUS_API_REJECTED:
            return False

        raw_code = result.get("error_code")
        if raw_code is None and isinstance(result.get("error_details"), dict):
            raw_code = result["error_details"].get("err_code")

        code = str(raw_code or "").upper()
        retryable_codes = {"API_ERROR", "UNEXPECTED_ERROR", "UNKNOWN_ERROR", "429", "500", "502", "503", "504"}
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
        if code == "429":
            return True

        message = str(result.get("message") or "").lower()
        return (
            "429" in message
            or "rate limit" in message
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
    ) -> list[dict[str, Any]]:
        """Redeem one code for many players with one prefetch and bulk persistence."""
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

        semaphore = asyncio.Semaphore(self.REDEEM_CONCURRENCY)

        async def redeem_one(index: int, player: Any, player_id_int: int) -> tuple[int, dict[str, Any]]:
            result = await self._redeem_with_retries(
                player_id_int=player_id_int,
                gift_code=gift_code,
                player_id_for_logs=str(player.player_id),
                semaphore=semaphore,
            )

            player_profile = result.get("player_profile") or {}
            normalized_result = {
                "player_id": str(player.player_id),
                "player_name": player_profile.get("name") or player.player_name,
                "success": result.get("success", False),
                "message": result.get("message", "Unknown error"),
                "error_code": result.get("error_code"),
                "already_redeemed": result.get("already_redeemed", False),
                "already_redeemed_by_api": result.get("already_redeemed_by_api", False),
                "status_category": self._categorize_redemption_status(result),
                "player_profile": result.get("player_profile"),
                "attempts": result.get("attempts"),
                "retries": result.get("retries", 0),
                "should_log": True,
            }
            return index, normalized_result

        if pending:
            indexed_results.extend(await asyncio.gather(*(redeem_one(*item) for item in pending)))

        results = [result for _, result in sorted(indexed_results, key=lambda item: item[0])]
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
        gift_code: str,
        player_id_for_logs: str,
        semaphore: asyncio.Semaphore | None = None,
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
                if semaphore is None:
                    last_result = await self._gift_code_service.redeem_gift_code_remote(player_id_int, gift_code)
                else:
                    async with semaphore:
                        last_result = await self._gift_code_service.redeem_gift_code_remote(player_id_int, gift_code)
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
            max_attempts_label = "unbounded" if is_rate_limited else str(standard_max_attempts)

            if not self._is_retryable_redemption_result(last_result) or (
                not is_rate_limited and attempt >= standard_max_attempts
            ):
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
                max_attempts_label,
                player_id_for_logs,
                gift_code,
                retry_delay,
                last_result.get("error_code"),
                last_result.get("message"),
            )
            await asyncio.sleep(retry_delay)

    def _format_result_lines(self, records: List[Dict], emoji: str, limit: int = 10) -> str:
        """Render result records for embed fields with deterministic truncation."""
        lines = []
        for record in records[:limit]:
            player_display = record.get("player_name") or record.get("player_id")
            message = record.get("message", "No details")
            retry_count = int(record.get("retries", 0) or 0)
            retry_suffix = f" (retried {retry_count}x)" if retry_count > 0 else ""
            lines.append(f"{emoji} `{record['player_id']}` - {player_display}{retry_suffix}\n   └─ {message}")

        if len(records) > limit:
            lines.append(f"*... and {len(records) - limit} more*")

        return "\n".join(lines)

    def _build_player_lines(self, players: List) -> List[str]:
        """Format registered players for paginated display."""
        lines = []
        for player in players:
            status = "✅" if player.enabled else "⛔"
            line = f"{status} `{player.player_id}`"
            if player.player_name:
                line += f" - {player.player_name}"
            meta_parts = []
            if getattr(player, "kingdom", None):
                meta_parts.append(f"K:{player.kingdom}")
            if getattr(player, "castle_level", None):
                meta_parts.append(f"CL:{player.castle_level}")
            if meta_parts:
                line += f" ({' | '.join(meta_parts)})"
            lines.append(line)
        return lines

    def _chunk_lines(self, lines: List[str], page_size: int) -> List[List[str]]:
        """Split lines into fixed-size pages."""
        return [lines[idx : idx + page_size] for idx in range(0, len(lines), page_size)]

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
        for key in ("name", "nickname", "playerName", "player_name"):
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
        for key in ("castleLevel", "castle", "stove_lv", "level", "lv"):
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
                        color=discord.Color.red(),
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
                    color=discord.Color.red(),
                )
                await interaction.followup.send(embed=embed)
                return

            embed = discord.Embed(
                title=f"✅ {len(added_players)} Player(s) Added Successfully",
                description="Player profiles saved and enabled for gift code redemption.",
                color=discord.Color.green(),
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
                    color=discord.Color.red(),
                )
            )

    async def _handle_add_alliance_slash(self, interaction: discord.Interaction, aid: str, kid: int):
        """Handle adding all fid-bearing alliance roster members to the redemption list."""
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
                    return uid, self._extract_player_payload(profile_result.get("data"))

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

            alliance_name = self._extract_alliance_name(payload) or f"Alliance {aid}"
            embed = discord.Embed(
                title=f"✅ Added {len(added_players)} Alliance Member(s)",
                description=f"Roster imported from **{alliance_name}** in kingdom `{kid}`.",
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
                        color=discord.Color.red(),
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
                        color=discord.Color.orange(),
                    )
                )
                return

            # Proceed with removal
            removed = await self._player_registry_service.remove_registered_player(player_id)

            if removed:
                embed = discord.Embed(
                    title="✅ Player Removed",
                    description=f"Player `{player_id}` has been removed from the gift code redemption list.",
                    color=discord.Color.green(),
                )
                await interaction.followup.send(embed=embed)
                logger.info(f"Player {player_id} removed by {interaction.user.id} (admin={is_admin})")
            else:
                await interaction.followup.send(
                    embed=self._build_status_embed(
                        title="❌ Player Not Found",
                        description=f"Player `{player_id}` is not in the player list.",
                        color=discord.Color.red(),
                    )
                )

        except Exception as e:
            logger.error(f"Error removing player {player_id}: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Could Not Remove Player",
                    description="An error occurred while removing the player.",
                    color=discord.Color.red(),
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
                        color=discord.Color.blue(),
                    ),
                    ephemeral=True,
                )
                return

            enabled_players = [p for p in all_players if p.enabled]
            disabled_players = [p for p in all_players if not p.enabled]
            ordered_players = enabled_players + disabled_players
            player_lines = self._build_player_lines(ordered_players)
            pages = self._chunk_lines(player_lines, page_size=20)

            view = PlayerListPaginationView(
                pages=pages,
                total_players=len(all_players),
                enabled_count=len(enabled_players),
                disabled_count=len(disabled_players),
                author_id=interaction.user.id,
            )
            message = await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)
            view.message = message

        except Exception as e:
            logger.error(f"Error listing players: {e}", exc_info=True)
            await interaction.followup.send(
                embed=self._build_status_embed(
                    title="❌ Could Not List Players",
                    description="An error occurred while retrieving the player list.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )

    @staticmethod
    def _build_status_embed(title: str, description: str, color: discord.Color) -> discord.Embed:
        """Build a consistent status embed for command responses."""
        return discord.Embed(title=title, description=description, color=color)
