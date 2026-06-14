import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from services.player_info_service import IPlayerInfoService
from services.interaction_tracking_service import InteractionTrackingService
from services.kingshot_data_service import KingshotDataService

logger = logging.getLogger(__name__)


class PlayerInfoHandler:
    """Handles player info Discord commands."""

    def __init__(
        self,
        player_info_service: IPlayerInfoService,
        bot: commands.Bot,
        interaction_tracking_service: InteractionTrackingService | None = None,
        kingshot_data_service: KingshotDataService | None = None,
    ):
        """
        Initialize player info handler.

        Args:
            player_info_service: Service for fetching player information
            bot: Discord bot instance
        """
        self._player_info_service = player_info_service
        self._bot = bot
        self._interaction_tracking_service = interaction_tracking_service or InteractionTrackingService()
        self._kingshot_data_service = kingshot_data_service
        logger.info("PlayerInfoHandler initialized")

    def register_commands(self):
        """Register all player info commands with the bot."""

        @self._bot.tree.command(name="stats", description="Fetch and display player statistics")
        @app_commands.describe(player_id="Governor ID / player ID to look up")
        async def get_player_stats(interaction: discord.Interaction, player_id: str):
            """Fetch and display player statistics."""
            await self._handle_player_stats_slash(interaction, player_id)

    async def _handle_player_stats_slash(self, interaction: discord.Interaction, player_id: str):
        """
        Handle the stats command (slash command).

        Args:
            interaction: Discord interaction
            player_id: The player ID to look up
        """
        await interaction.response.defer(thinking=True)

        user_info = f"{interaction.user.name}#{interaction.user.discriminator} (ID: {interaction.user.id})"
        guild_info = f"{interaction.guild.name} (ID: {interaction.guild.id})" if interaction.guild else "DM"

        logger.info(f"Stats command for player {player_id} requested by {user_info} in {guild_info}")

        try:
            # Fetch player info
            player_data = await self._player_info_service.get_player_info(player_id)
            ks_data = await self._get_kingshot_data_player(player_id)

            if player_data is None:
                logger.warning(f"Player {player_id} not found for request by {user_info}")
                not_found_embed = discord.Embed(
                    title="❌ Player Not Found",
                    description=(
                        f"Could not find a player with ID `{player_id}`.\n"
                        "Please verify the ID in-game and try again."
                    ),
                    color=discord.Color.red(),
                )
                not_found_embed.set_footer(text="Tip: You can add a valid player later with /addplayer")
                await interaction.followup.send(embed=not_found_embed)

                # Track failed lookup in database
                try:
                    await self._interaction_tracking_service.track_player_lookup(
                        user_id=interaction.user.id,
                        player_id=player_id,
                        success=False,
                        username=interaction.user.name,
                        discriminator=interaction.user.discriminator,
                        display_name=interaction.user.display_name,
                    )
                except Exception as db_error:
                    logger.error(f"Database tracking error: {db_error}", exc_info=True)

                return

            # Format the response
            formatted_stats = self._player_info_service.format_player_stats(player_data)

            # Get player name for title
            player_name = player_data.get("name", f"Player {player_id}")

            # Create an embed for better presentation
            embed = discord.Embed(
                title=f"📊 {player_name}",
                description=formatted_stats,
                color=discord.Color.blue(),
            )

            embed.add_field(name="Player ID", value=f"`{player_data.get('playerId', player_id)}`", inline=True)
            embed.add_field(
                name="Kingdom",
                value=str(player_data.get("kingdom", "N/A")),
                inline=True,
            )
            embed.add_field(
                name="Castle Level",
                value=str(player_data.get("levelRenderedDetailed") or player_data.get("level") or "N/A"),
                inline=True,
            )
            embed.add_field(name="Power", value=self._format_power(ks_data), inline=True)
            embed.add_field(name="VIP Level", value=self._format_vip(ks_data), inline=True)
            embed.add_field(name="Alliance", value=self._format_alliance(ks_data), inline=True)

            # Add profile photo if available
            if "profilePhoto" in player_data and player_data["profilePhoto"]:
                embed.set_thumbnail(url=player_data["profilePhoto"])

            embed.add_field(
                name="Links",
                value=self._format_data_links(player_id, player_data, ks_data),
                inline=False,
            )
            embed.set_footer(text="Data from kingshot.jeab.dev • Use /addplayer to include this player in auto-redeem")

            await interaction.followup.send(embed=embed)
            logger.info(f"Successfully displayed stats for {player_name} (ID: {player_id}) to {user_info}")

            try:
                resolved_player_id = str(player_data.get("playerId") or player_id)
                resolved_kingdom = str(player_data.get("kingdom")) if player_data.get("kingdom") is not None else None
                resolved_castle_level = (
                    str(player_data.get("levelRenderedDetailed") or player_data.get("level"))
                    if (player_data.get("levelRenderedDetailed") or player_data.get("level") is not None)
                    else None
                )

                await self._interaction_tracking_service.track_player_lookup(
                    user_id=interaction.user.id,
                    player_id=resolved_player_id,
                    player_name=player_name,
                    kingdom=resolved_kingdom,
                    castle_level=resolved_castle_level,
                    success=True,
                    username=interaction.user.name,
                    discriminator=interaction.user.discriminator,
                    display_name=interaction.user.display_name,
                )

                # Update legacy non-canonical records only if they already exist.
                if resolved_player_id != str(player_id):
                    await self._interaction_tracking_service.sync_player_metadata(
                        player_id=str(player_id),
                        player_name=player_name,
                        kingdom=resolved_kingdom,
                        castle_level=resolved_castle_level,
                    )

                logger.debug(f"Tracked player stats request by user {interaction.user.id}")
            except Exception as db_error:
                logger.error(f"Database tracking error: {db_error}", exc_info=True)

        except Exception as e:
            logger.error(
                f"Error handling stats command for player {player_id} by {user_info}: {e}",
                exc_info=True,
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Unexpected Error",
                    description="An error occurred while fetching player stats. Please try again later.",
                    color=discord.Color.red(),
                )
            )

    async def _get_kingshot_data_player(self, player_id: str) -> dict[str, Any] | None:
        if self._kingshot_data_service is None:
            return None

        result = await self._kingshot_data_service.get_player_by_fid(player_id)
        if not result.get("success"):
            logger.warning(
                "KingShot Data enrichment failed for player %s: %s",
                player_id,
                result.get("error_message") or result.get("error_code"),
            )
            return None

        data = result.get("data")
        if isinstance(data, dict) and not data.get("error"):
            return data
        return None

    @classmethod
    def _format_power(cls, ks_data: dict[str, Any] | None) -> str:
        if not ks_data:
            return "N/A"
        power = ks_data.get("power")
        if power is None and isinstance(ks_data.get("stats"), dict):
            power = ks_data["stats"].get("8")
        return cls._format_number(power) if power is not None else "N/A"

    @staticmethod
    def _format_vip(ks_data: dict[str, Any] | None) -> str:
        if not ks_data:
            return "N/A"
        vip = ks_data.get("vip")
        if vip in (None, 0, "0"):
            return "Hidden"
        return str(vip)

    @staticmethod
    def _format_alliance(ks_data: dict[str, Any] | None) -> str:
        if not ks_data or not isinstance(ks_data.get("alliance"), dict):
            return "N/A"
        alliance = ks_data["alliance"]
        aid = alliance.get("aid")
        abbr = alliance.get("abbr")
        name = alliance.get("name")
        aid_text = f" (`{aid}`)" if aid is not None else ""
        if abbr and name:
            return f"`[{abbr}]` {name}{aid_text}"
        if abbr:
            return f"`[{abbr}]`{aid_text}"
        if name:
            return f"{name}{aid_text}"
        if aid is not None:
            return f"ID `{aid}`"
        return "N/A"

    @classmethod
    def _format_data_links(
        cls,
        player_id: str,
        player_data: dict[str, Any],
        ks_data: dict[str, Any] | None,
    ) -> str:
        player_link = f"[Player details](https://kingshot.jeab.dev/player/{player_id})"
        alliance_link = cls._format_alliance_link(player_data, ks_data)
        return f"{player_link}\n{alliance_link}"

    @staticmethod
    def _format_alliance_link(player_data: dict[str, Any], ks_data: dict[str, Any] | None) -> str:
        if not ks_data or not isinstance(ks_data.get("alliance"), dict):
            return "Alliance details: N/A"

        alliance = ks_data["alliance"]
        aid = alliance.get("aid")
        kingdom = ks_data.get("kid") or player_data.get("kingdom")
        if aid is None or kingdom is None:
            return "Alliance details: N/A"

        return f"[Alliance details](https://kingshot.jeab.dev/alliances/{kingdom}/{aid})"

    @staticmethod
    def _format_number(value: Any) -> str:
        try:
            number = float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return str(value)
        if number.is_integer():
            return f"{int(number):,}"
        return f"{number:,.2f}"
