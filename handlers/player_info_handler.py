import logging

import discord
from discord import app_commands
from discord.ext import commands

from services.player_info_service import IPlayerInfoService
from services.interaction_tracking_service import InteractionTrackingService

logger = logging.getLogger(__name__)


class PlayerInfoHandler:
    """Handles player info Discord commands."""

    def __init__(
        self,
        player_info_service: IPlayerInfoService,
        bot: commands.Bot,
        interaction_tracking_service: InteractionTrackingService | None = None,
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
        logger.info("PlayerInfoHandler initialized")

    def register_commands(self):
        """Register all player info commands with the bot."""

        @self._bot.tree.command(name="stats", description="Fetch and display player statistics")
        @app_commands.describe(player_id="The player ID to look up")
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

            # Add profile photo if available
            if "profilePhoto" in player_data and player_data["profilePhoto"]:
                embed.set_thumbnail(url=player_data["profilePhoto"])

            embed.set_footer(text="Data from kingshot.net API • Use /addplayer to include this player in auto-redeem")

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
