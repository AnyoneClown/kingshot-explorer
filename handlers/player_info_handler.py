import asyncio
import logging
from io import BytesIO
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from services import player_card_renderer as card_renderer
from services.player_info_service import IPlayerInfoService, PlayerLookupUnavailable
from services.interaction_tracking_service import InteractionTrackingService
from services.kingshot_data_service import KingshotDataService
from handlers.ui import EmbedColors, OwnedView

logger = logging.getLogger(__name__)

MAX_KINGDOM_NUMBER = 99999
SCOUT_DEFAULT_LIMIT = 5
SCOUT_MAX_LIMIT = 15
# Public browse panels stay usable for a typical channel discussion.
BROWSE_PANEL_TIMEOUT_SECONDS = 900


class ScoutReportView(OwnedView):
    """Browse a kingdom's scout results in one message."""

    def __init__(self, handler, kingdom_number: int, entries: list[dict[str, Any]], author_id: int):
        super().__init__(author_id, timeout=BROWSE_PANEL_TIMEOUT_SECONDS)
        self.handler = handler
        self.kingdom_number = kingdom_number
        self.entries = entries
        self.current_player = 0
        self.showing_summary = True
        self._loading = False
        self._update_controls()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        if self._loading:
            await interaction.response.send_message("Loading this player. Try again in a moment.", ephemeral=True)
            return False
        return True

    def _update_controls(self):
        self.previous_button.disabled = self.current_player == 0
        self.next_button.disabled = self.current_player == len(self.entries) - 1
        self.summary_button.disabled = self.showing_summary
        self.player_select.options = []
        for index, entry in enumerate(self.entries):
            player = self.handler._build_player_data_from_kingshot(entry.get("player") or entry, entry, self.kingdom_number)
            self.player_select.options.append(discord.SelectOption(
                label=f"#{entry.get('rank', index + 1)} {player.get('name') or 'Player'}"[:100],
                value=str(index),
                description=f"ID: {player['playerId']}"[:100],
                default=index == self.current_player and not self.showing_summary,
            ))

    def build_summary(self) -> discord.Embed:
        lines = []
        for index, entry in enumerate(self.entries):
            player = self.handler._build_player_data_from_kingshot(entry.get("player") or entry, entry, self.kingdom_number)
            name = discord.utils.escape_markdown(str(player.get("name") or "Unknown player")[:80])
            rank = str(entry.get("rank", index + 1))[:12]
            score = self.handler._format_number(entry["score"])[:40] if entry.get("score") is not None else "Unavailable"
            lines.append(f"**#{rank} {name}** · {score}")
        embed = discord.Embed(
            title=f"🔎 Scout Report · Kingdom {self.kingdom_number}",
            description="**Mystic Trial leaderboard**\n" + "\n".join(lines),
            color=EmbedColors.INFO,
        )
        embed.set_footer(
            text=(
                f"{len(self.entries)} players · Pick a player to open their profile in this public message · "
                "Scores are Mystic Trial scores"
            )
        )
        return embed

    async def _show_player(self, interaction: discord.Interaction):
        self._loading = True
        try:
            await interaction.response.defer()
            entry = self.entries[self.current_player]
            profile = await self.handler._resolve_scout_player(entry)
            data = profile or entry
            player_data = self.handler._build_player_data_from_kingshot(data, entry, self.kingdom_number)
            embed = self.handler._build_stats_embed(
                player_id=player_data["playerId"],
                player_name=player_data.get("name") or "Unknown player",
                player_data=player_data,
                ks_data=data,
                mystic_trial={"entry": entry},
                description=f"Mystic Trial · Rank #{entry.get('rank', self.current_player + 1)}",
            )
            heroes = await self.handler._fetch_arena_heroes(self.handler._extract_player_uid(player_data, data))
            card_file = await self.handler._attach_player_card(embed, heroes, profile)
            self.showing_summary = False
            self._update_controls()
            embed.set_footer(text=f"Player {self.current_player + 1}/{len(self.entries)} · Unavailable means data was not returned")
            await interaction.edit_original_response(embed=embed, attachments=[card_file] if card_file else [], view=self)

        finally:
            self._loading = False

    @discord.ui.select(placeholder="Pick a player to view their profile", row=0)
    async def player_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.current_player = int(select.values[0])
        await self._show_player(interaction)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=1)
    async def previous_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_player = max(0, self.current_player - 1)
        await self._show_player(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.current_player = min(len(self.entries) - 1, self.current_player + 1)
        await self._show_player(interaction)

    @discord.ui.button(label="Summary", style=discord.ButtonStyle.secondary, row=1)
    async def summary_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.showing_summary = True
        self._update_controls()
        await interaction.response.edit_message(embed=self.build_summary(), attachments=[], view=self)


class PlayerInfoHandler:
    """Handles player info Discord commands."""

    # Static artwork used by the player card; tests point this at temporary folders.
    ASSET_DIRS = card_renderer.AssetDirs()
    PLAYER_CARD_FILENAME = "player_card.png"
    # Rendering is pure-Python CPU work in worker threads; keep it from piling up.
    RENDER_CONCURRENCY = 2
    MYSTIC_TRIAL_BOARD_TYPE = 20
    SCOUT_BOARD_TYPE = MYSTIC_TRIAL_BOARD_TYPE
    SCOUT_DEFAULT_LIMIT = SCOUT_DEFAULT_LIMIT
    SCOUT_MAX_LIMIT = SCOUT_MAX_LIMIT

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
        self._render_semaphore = asyncio.Semaphore(self.RENDER_CONCURRENCY)
        logger.info("PlayerInfoHandler initialized")

    def register_commands(self):
        """Register all player info commands with the bot."""

        @self._bot.tree.command(name="stats", description="Fetch and display player statistics")
        @app_commands.describe(player_id="Governor ID / player ID to look up")
        async def get_player_stats(interaction: discord.Interaction, player_id: str):
            """Fetch and display player statistics."""
            await self._handle_player_stats_slash(interaction, player_id)

        @self._bot.tree.command(name="scout", description="Scout top Mystic Trial players in a kingdom")
        @app_commands.describe(
            kingdom_number="Kingdom number to scout",
            limit=f"Number of leaderboard players to scout, default {SCOUT_DEFAULT_LIMIT}, max {SCOUT_MAX_LIMIT}",
        )
        async def scout_kingdom(
            interaction: discord.Interaction,
            kingdom_number: app_commands.Range[int, 1, MAX_KINGDOM_NUMBER],
            limit: app_commands.Range[int, 1, SCOUT_MAX_LIMIT] = SCOUT_DEFAULT_LIMIT,
        ):
            """Scout top Mystic Trial players in a kingdom."""
            await self._handle_scout_slash(interaction, kingdom_number, limit)

    async def _handle_player_stats_slash(self, interaction: discord.Interaction, player_id: str):
        """
        Handle the stats command (slash command).

        Args:
            interaction: Discord interaction
            player_id: The player ID to look up
        """
        player_id = str(player_id).strip()
        if not player_id.isdigit():
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⚠️ Invalid Governor ID",
                    description="Enter the numeric Governor ID shown on the in-game profile, for example `123456789`.",
                    color=EmbedColors.WARNING,
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        user_info = f"{interaction.user.name}#{interaction.user.discriminator} (ID: {interaction.user.id})"
        guild_info = f"{interaction.guild.name} (ID: {interaction.guild.id})" if interaction.guild else "DM"

        logger.info(f"Stats command for player {player_id} requested by {user_info} in {guild_info}")

        try:
            try:
                player_data = await self._player_info_service.get_player_info(player_id)
            except PlayerLookupUnavailable as exc:
                logger.warning("Player lookup unavailable for %s requested by %s: %s", player_id, user_info, exc)
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="⚠️ Lookup Service Unavailable",
                        description=(
                            "The player lookup service is unavailable right now, so this ID could not be checked.\n"
                            "Try again in a minute."
                        ),
                        color=EmbedColors.WARNING,
                    )
                )
                return

            if player_data is None:
                logger.warning(f"Player {player_id} not found for request by {user_info}")
                not_found_embed = discord.Embed(
                    title="❌ Player Not Found",
                    description=(
                        f"Could not find a player with ID `{player_id}`.\n"
                        "Please verify the ID in-game and try again."
                    ),
                    color=EmbedColors.ERROR,
                )
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

            player_name = player_data.get("name") or f"Player {player_id}"
            uid = self._extract_player_uid(player_data, None)
            # The profile (gear), Mystic Trial rank and Arena team are independent lookups.
            ks_data, mystic_trial, heroes = await asyncio.gather(
                self._get_kingshot_data_player(player_id, uid=uid),
                self._get_mystic_trial(player_data, None),
                self._fetch_arena_heroes(uid),
            )
            embed = self._build_stats_embed(
                player_id=player_id,
                player_name=player_name,
                player_data=player_data,
                ks_data=ks_data,
                mystic_trial=mystic_trial,
                description=None,
            )

            card_file = await self._attach_player_card(embed, heroes, ks_data)
            if card_file:
                await interaction.followup.send(embed=embed, file=card_file)
            else:
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
                    color=EmbedColors.ERROR,
                )
            )

    async def _handle_scout_slash(self, interaction: discord.Interaction, kingdom_number: int, limit: int):
        """Handle the scout command.

        Discord enforces the option ranges, but /help forms call this directly, so
        inputs are validated again before deferring and rejected privately.
        """
        if not 1 <= kingdom_number <= MAX_KINGDOM_NUMBER:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⚠️ Invalid Kingdom Number",
                    description=f"Kingdom number must be between 1 and {MAX_KINGDOM_NUMBER}.",
                    color=EmbedColors.WARNING,
                ),
                ephemeral=True,
            )
            return

        if limit < 1:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="⚠️ Invalid Limit",
                    description=f"Limit must be between 1 and {SCOUT_MAX_LIMIT}.",
                    color=EmbedColors.WARNING,
                ),
                ephemeral=True,
            )
            return

        limit = min(limit, SCOUT_MAX_LIMIT)

        if self._kingshot_data_service is None:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="❌ KingShot Data API Not Configured",
                    description="Scout requires the KingShot Data API service.",
                    color=EmbedColors.ERROR,
                ),
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)

        user_info = f"{interaction.user.name}#{interaction.user.discriminator} (ID: {interaction.user.id})"
        guild_info = f"{interaction.guild.name} (ID: {interaction.guild.id})" if interaction.guild else "DM"

        logger.info(
            "Scout command for kingdom %s limit %s requested by %s in %s",
            kingdom_number,
            limit,
            user_info,
            guild_info,
        )

        try:
            board_result = await self._kingshot_data_service.get_kingdom_board(
                self.SCOUT_BOARD_TYPE,
                kingdom_number,
                limit=limit,
                resolve=True,
            )
            if not board_result.get("success"):
                embed = discord.Embed(
                    title=f"❌ Could Not Scout Kingdom {kingdom_number}",
                    description=board_result.get("error_message", "KingShot Data API request failed."),
                    color=EmbedColors.ERROR,
                )
                embed.set_footer(text="Try again in a moment or verify the kingdom number")
                await interaction.followup.send(embed=embed)
                return

            board_data = board_result.get("data")
            entries = self._extract_leaderboard_entries(board_data)
            if not entries:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title=f"🔎 Scout Report - Kingdom {kingdom_number}",
                        description="No Mystic Trial leaderboard entries were returned for this kingdom.",
                        color=EmbedColors.WARNING,
                    )
                )
                return

            entries = entries[:limit]
            semaphore = asyncio.Semaphore(3)

            async def resolve_player(entry):
                async with semaphore:
                    await self._resolve_scout_player(entry)

            await asyncio.gather(*(resolve_player(entry) for entry in entries))
            view = ScoutReportView(self, kingdom_number, entries, interaction.user.id)
            view.message = await interaction.followup.send(embed=view.build_summary(), view=view, wait=True)

            logger.info("Successfully sent scout report for kingdom %s", kingdom_number)

        except Exception as e:
            logger.error("Error handling scout command for kingdom %s: %s", kingdom_number, e, exc_info=True)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Unexpected Error",
                    description="An error occurred while scouting the kingdom. Please try again later.",
                    color=EmbedColors.ERROR,
                )
            )

    async def _resolve_scout_player(self, entry: dict[str, Any]) -> dict[str, Any] | None:
        profile = entry.get("player")
        if isinstance(profile, dict) and profile and not profile.get("error"):
            return profile
        uid = self._extract_player_uid(entry, None)
        fid = self._extract_entry_fid(entry)
        profile = await self._get_kingshot_data_player(str(fid) if fid is not None else None, uid=uid)
        entry["player"] = profile
        return profile

    async def _get_kingshot_data_player(
        self, player_id: str | None = None, *, uid: str | None = None,
    ) -> dict[str, Any] | None:
        if self._kingshot_data_service is None or not (uid or player_id):
            return None

        try:
            result = (
                await self._kingshot_data_service.get_player(uid)
                if uid else await self._kingshot_data_service.get_player_by_fid(player_id)
            )
        except Exception:
            logger.warning("KingShot Data enrichment failed for player %s", uid or player_id, exc_info=True)
            return None
        if not result.get("success"):
            logger.warning(
                "KingShot Data enrichment failed for player %s: %s",
                uid or player_id,
                result.get("error_message") or result.get("error_code"),
            )
            return None

        data = result.get("data")
        if isinstance(data, dict) and not data.get("error"):
            return data
        return None

    async def _get_mystic_trial(
        self,
        player_data: dict[str, Any],
        ks_data: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if self._kingshot_data_service is None:
            return None

        uid = self._extract_player_uid(player_data, ks_data)
        kid = self._extract_player_kingdom(player_data, ks_data)
        if uid is None or kid is None:
            return None

        try:
            result = await self._kingshot_data_service.search_leaderboard(self.MYSTIC_TRIAL_BOARD_TYPE, uid, kid)
        except Exception:
            logger.warning("Mystic Trial enrichment failed for uid %s kid %s", uid, kid, exc_info=True)
            return None
        if not result.get("success"):
            logger.warning(
                "Mystic Trial enrichment failed for uid %s kid %s: %s",
                uid,
                kid,
                result.get("error_message") or result.get("error_code"),
            )
            return None

        data = result.get("data")
        return data if isinstance(data, dict) else None

    @classmethod
    def _build_stats_embed(
        cls,
        *,
        player_id: str,
        player_name: str,
        player_data: dict[str, Any],
        ks_data: dict[str, Any] | None,
        description: str | None,
        mystic_trial: dict[str, Any] | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"📊 {player_name}"[:256],
            description=description[:1024] if description else None,
            color=EmbedColors.INFO,
        )

        power_source = ks_data
        if not power_source and player_data.get("power") is not None:
            power_source = {"power": player_data["power"]}
        alliance_text = cls._format_alliance(ks_data)
        if alliance_text == "Unavailable" and player_data.get("allianceTag"):
            alliance_text = f"`[{player_data['allianceTag']}]`"

        embed.add_field(name="Power", value=cls._format_power(power_source), inline=True)
        embed.add_field(
            name="Kingdom",
            value=str(player_data.get("kingdom") if player_data.get("kingdom") is not None else "Unavailable"),
            inline=True,
        )
        embed.add_field(
            name="Castle Level",
            value=str(player_data.get("levelRenderedDetailed") or (player_data.get("level") if player_data.get("level") is not None else "Unavailable")),
            inline=True,
        )
        embed.add_field(name="Player ID", value=f"`{player_data.get('playerId') or player_id}`", inline=True)
        embed.add_field(name="VIP Level", value=cls._format_vip(ks_data), inline=True)
        embed.add_field(name="Alliance", value=alliance_text, inline=True)
        embed.add_field(name="Mystic Trial", value=cls._format_mystic_trial(mystic_trial), inline=True)

        photo = player_data.get("profilePhoto")
        # Discord rejects the whole message when a thumbnail URL is malformed.
        if isinstance(photo, str) and photo.startswith(("https://", "http://")):
            embed.set_thumbnail(url=photo)

        for index, field in enumerate(embed.fields):
            embed.set_field_at(index, name=field.name, value=str(field.value)[:512], inline=field.inline)
        return embed

    async def _fetch_arena_heroes(self, uid: str | None) -> list[Any] | None:
        """Arena defense heroes for ``uid``; ``None`` when unavailable."""
        if self._kingshot_data_service is None or uid is None:
            return None

        try:
            result = await self._kingshot_data_service.get_arena(uid)
        except Exception:
            logger.warning("KingShot arena enrichment failed for uid %s", uid, exc_info=True)
            return None
        if not result.get("success"):
            logger.warning(
                "KingShot arena enrichment failed for uid %s: %s",
                uid,
                result.get("error_message") or result.get("error_code"),
            )
            return None

        arena_data = result.get("data")
        if not isinstance(arena_data, dict) or not isinstance(arena_data.get("heroes"), list):
            return None
        return arena_data["heroes"]

    async def _render_player_card(
        self,
        heroes: list[Any] | None,
        gear: list[Any] | None,
    ) -> card_renderer.PlayerCard:
        """Render off the event loop; decoded artwork is cached by the renderer."""
        if not heroes and not gear:
            return card_renderer.PlayerCard(None)
        async with self._render_semaphore:
            try:
                return await asyncio.to_thread(
                    card_renderer.render_player_card,
                    list(heroes or []),
                    list(gear or []),
                    self.ASSET_DIRS,
                )
            except Exception as exc:
                logger.warning("Failed to build player card image: %s", exc, exc_info=True)
                return card_renderer.PlayerCard(None)

    async def _attach_player_card(
        self,
        embed: discord.Embed,
        heroes: list[Any] | None,
        ks_data: dict[str, Any] | None,
    ) -> discord.File | None:
        """Add the gear summary/arena fields and return the card attachment, if any."""
        gear = self._extract_gear(ks_data)
        card = await self._render_player_card(heroes, gear)
        embed.add_field(name="Governor Gear", value=self._format_gear_summary(ks_data, card.gear_drawn), inline=False)
        if not card.heroes_drawn:
            embed.add_field(name="Arena", value="Loadout unavailable", inline=False)
        if card.png is None:
            return None
        embed.set_image(url=f"attachment://{self.PLAYER_CARD_FILENAME}")
        return discord.File(BytesIO(card.png), filename=self.PLAYER_CARD_FILENAME)

    @staticmethod
    def _extract_gear(ks_data: dict[str, Any] | None) -> list[Any] | None:
        if not isinstance(ks_data, dict) or not isinstance(ks_data.get("gear"), list):
            return None
        return ks_data["gear"]

    @staticmethod
    def _format_gear_summary(ks_data: dict[str, Any] | None, gear_drawn: int) -> str:
        """Text companion to the gear strip; also covers hidden or unknown gear."""
        gear = PlayerInfoHandler._extract_gear(ks_data)
        if gear is None:
            return "Unavailable"
        slots = [item for item in gear if isinstance(item, dict)]
        if not slots:
            return "Equipment hidden"

        equipped = {
            item.get("slot")
            for item in slots
            if card_renderer.positive_int(item.get("equipid")) and card_renderer.positive_int(item.get("slot"))
        }
        if not equipped:
            return "No equipment equipped"
        charm_ids = [
            charm
            for item in slots
            if isinstance(item.get("charms"), list)
            for charm in item["charms"]
            if card_renderer.positive_int(charm)
        ]
        parts = [f"{len(equipped)}/{card_renderer.GEAR_SLOTS} slots equipped", f"{len(charm_ids)} charms"]
        levels = [level for level in map(card_renderer.charm_level, charm_ids) if level]
        if levels:
            parts.append(f"avg charm Lv {sum(levels) / len(levels):.1f}")
        text = " · ".join(parts)
        if gear_drawn == 0:
            text += "\nGear icons unavailable for these item IDs"
        return text

    @staticmethod
    def _extract_player_uid(player_data: dict[str, Any], ks_data: dict[str, Any] | None) -> str | None:
        for source in (ks_data, player_data):
            if not isinstance(source, dict):
                continue
            for key in ("uid", "playerUid", "player_uid"):
                value = source.get(key)
                if value not in (None, ""):
                    return str(value)
        return None

    @staticmethod
    def _extract_player_kingdom(player_data: dict[str, Any], ks_data: dict[str, Any] | None) -> str | None:
        for source in (ks_data, player_data):
            if not isinstance(source, dict):
                continue
            for key in ("kid", "kingdom", "kingdomId", "kingdom_id"):
                value = source.get(key)
                if value not in (None, ""):
                    return str(value)
        return None

    @staticmethod
    def _extract_leaderboard_entries(board_data: Any) -> list[dict[str, Any]]:
        if isinstance(board_data, dict) and isinstance(board_data.get("entries"), list):
            return [entry for entry in board_data["entries"] if isinstance(entry, dict)]
        if isinstance(board_data, list):
            return [entry for entry in board_data if isinstance(entry, dict)]
        return []

    @staticmethod
    def _extract_entry_fid(entry: dict[str, Any]) -> Any:
        for key in ("fid", "player_fid", "playerId", "player_id"):
            if entry.get(key) not in (None, ""):
                return entry[key]
        player = entry.get("player")
        if isinstance(player, dict):
            for key in ("fid", "playerId", "player_id"):
                if player.get(key) not in (None, ""):
                    return player[key]
        return None

    @staticmethod
    def _build_player_data_from_kingshot(
        data: dict[str, Any],
        entry: dict[str, Any],
        kingdom_number: int,
    ) -> dict[str, Any]:
        fid = data.get("fid") or PlayerInfoHandler._extract_entry_fid(entry)
        uid = data.get("uid") or entry.get("uid")
        player = entry.get("player")
        if uid is None and isinstance(player, dict):
            uid = player.get("uid")
        return {
            "name": data.get("name") or entry.get("name"),
            "playerId": str(fid) if fid is not None else "Unavailable",
            "playerUid": str(uid) if uid is not None else None,
            "level": next((data[key] for key in ("stove_lv", "castle_level", "lv") if data.get(key) is not None), None),
            "kingdom": data.get("kid") or entry.get("kid") or kingdom_number,
        }

    @classmethod
    def _format_power(cls, ks_data: dict[str, Any] | None) -> str:
        if not ks_data:
            return "Unavailable"
        power = ks_data.get("power")
        if power is None and isinstance(ks_data.get("stats"), dict):
            power = ks_data["stats"].get("8")
        return cls._format_number(power) if power is not None else "Unavailable"

    @staticmethod
    def _format_vip(ks_data: dict[str, Any] | None) -> str:
        if not ks_data:
            return "Unavailable"
        vip = ks_data.get("vip")
        if vip is None:
            return "Unavailable"
        if vip in (0, "0"):
            return "Hidden"
        return str(vip)

    @staticmethod
    def _format_alliance(ks_data: dict[str, Any] | None) -> str:
        if not ks_data or not isinstance(ks_data.get("alliance"), dict):
            return "Unavailable"
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
        return "Unavailable"

    @classmethod
    def _format_mystic_trial(cls, mystic_trial: dict[str, Any] | None) -> str:
        if not mystic_trial:
            return "Kingdom Rank: Unavailable\nScore: Unavailable"

        entry = mystic_trial.get("entry")
        if not isinstance(entry, dict):
            entry = {}

        rank = entry.get("rank") or mystic_trial.get("rank")
        score = entry.get("score") if entry.get("score") is not None else mystic_trial.get("score")
        rank_text = f"#{cls._format_number(rank)}" if rank is not None else "Unavailable"
        score_text = cls._format_number(score) if score is not None else "Unavailable"
        return f"Kingdom Rank: {rank_text}\nScore: {score_text}"

    @staticmethod
    def _format_number(value: Any) -> str:
        try:
            number = float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return str(value)
        if number.is_integer():
            return f"{int(number):,}"
        return f"{number:,.2f}"
