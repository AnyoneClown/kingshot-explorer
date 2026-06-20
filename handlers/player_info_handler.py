import logging
import struct
import zlib
from io import BytesIO
from pathlib import Path
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

    HERO_IMAGE_DIR = Path(__file__).resolve().parents[1] / "images" / "heroes"
    HERO_GEAR_IMAGE_DIR = Path(__file__).resolve().parents[1] / "images" / "exclusive_weapons"
    ARENA_HERO_MAX_WIDTH = 76
    ARENA_HERO_MAX_HEIGHT = 134
    ARENA_GEAR_ICON_SIZE = 56
    ARENA_STAR_ICON_SIZE = 20
    MYSTIC_TRIAL_BOARD_TYPE = 20
    SCOUT_BOARD_TYPE = MYSTIC_TRIAL_BOARD_TYPE
    SCOUT_DEFAULT_LIMIT = 5
    SCOUT_MAX_LIMIT = 15

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
        default_scout_limit = self.SCOUT_DEFAULT_LIMIT

        @self._bot.tree.command(name="stats", description="Fetch and display player statistics")
        @app_commands.describe(player_id="Governor ID / player ID to look up")
        async def get_player_stats(interaction: discord.Interaction, player_id: str):
            """Fetch and display player statistics."""
            await self._handle_player_stats_slash(interaction, player_id)

        @self._bot.tree.command(name="scout", description="Scout top Mystic Trial players in a kingdom")
        @app_commands.describe(
            kingdom_number="Kingdom number to scout",
            limit="Number of leaderboard players to scout, default 5, max 15",
        )
        async def scout_kingdom(
            interaction: discord.Interaction,
            kingdom_number: int,
            limit: int = default_scout_limit,
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

            formatted_stats = self._player_info_service.format_player_stats(player_data)
            player_name = player_data.get("name", f"Player {player_id}")
            mystic_trial = await self._get_mystic_trial(player_data, ks_data)
            embed = self._build_stats_embed(
                player_id=player_id,
                player_name=player_name,
                player_data=player_data,
                ks_data=ks_data,
                mystic_trial=mystic_trial,
                description=formatted_stats,
            )

            hero_file = await self._get_arena_loadout_image(player_data, ks_data)
            if hero_file:
                embed.set_image(url="attachment://arena_loadout.png")
                await interaction.followup.send(embed=embed, file=hero_file)
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
                    color=discord.Color.red(),
                )
            )

    async def _handle_scout_slash(self, interaction: discord.Interaction, kingdom_number: int, limit: int):
        """Handle the scout command."""
        await interaction.response.defer(thinking=True)

        user_info = f"{interaction.user.name}#{interaction.user.discriminator} (ID: {interaction.user.id})"
        guild_info = f"{interaction.guild.name} (ID: {interaction.guild.id})" if interaction.guild else "DM"

        if kingdom_number <= 0:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Invalid Kingdom Number",
                    description="Kingdom number must be a positive integer.",
                    color=discord.Color.orange(),
                )
            )
            return

        if limit < 1:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Invalid Limit",
                    description="Limit must be at least 1.",
                    color=discord.Color.orange(),
                )
            )
            return

        limit = min(limit, self.SCOUT_MAX_LIMIT)

        if self._kingshot_data_service is None:
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ KingShot Data API Not Configured",
                    description="Scout requires the KingShot Data API service.",
                    color=discord.Color.red(),
                )
            )
            return

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
                    color=discord.Color.red(),
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
                        color=discord.Color.orange(),
                    )
                )
                return

            profiles_by_fid: dict[str, dict[str, Any] | None] = {}
            for entry in entries:
                fid = self._extract_entry_fid(entry)
                if fid is None:
                    continue
                profiles_by_fid[str(fid)] = await self._get_kingshot_data_player(str(fid))

            for entry in entries:
                fid = self._extract_entry_fid(entry)
                profile = profiles_by_fid.get(str(fid)) if fid is not None else None
                data = profile or entry
                player_data = self._build_player_data_from_kingshot(data, entry, kingdom_number)
                player_id = str(player_data.get("playerId") or fid or "")
                player_name = player_data.get("name") or f"Player {player_id or '?'}"
                mystic_trial = await self._get_mystic_trial(player_data, data)
                embed = self._build_stats_embed(
                    player_id=player_id,
                    player_name=player_name,
                    player_data=player_data,
                    ks_data=data,
                    mystic_trial=mystic_trial,
                    description=self._format_kingshot_profile_summary(player_data),
                )

                hero_file = await self._get_arena_loadout_image(player_data, data)
                if hero_file:
                    embed.set_image(url="attachment://arena_loadout.png")
                    await interaction.followup.send(embed=embed, file=hero_file)
                else:
                    await interaction.followup.send(embed=embed)

            logger.info("Successfully sent scout report for kingdom %s", kingdom_number)

        except Exception as e:
            logger.error("Error handling scout command for kingdom %s: %s", kingdom_number, e, exc_info=True)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="❌ Unexpected Error",
                    description="An error occurred while scouting the kingdom. Please try again later.",
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

        result = await self._kingshot_data_service.search_leaderboard(self.MYSTIC_TRIAL_BOARD_TYPE, uid, kid)
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
        description: str,
        mystic_trial: dict[str, Any] | None = None,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"📊 {player_name}",
            description=description,
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
        embed.add_field(name="Power", value=cls._format_power(ks_data), inline=True)
        embed.add_field(name="VIP Level", value=cls._format_vip(ks_data), inline=True)
        embed.add_field(name="Alliance", value=cls._format_alliance(ks_data), inline=True)
        embed.add_field(name="Mystic Trial", value=cls._format_mystic_trial(mystic_trial), inline=True)

        if "profilePhoto" in player_data and player_data["profilePhoto"]:
            embed.set_thumbnail(url=player_data["profilePhoto"])

        return embed

    async def _get_arena_loadout_image(
        self,
        player_data: dict[str, Any],
        ks_data: dict[str, Any] | None,
    ) -> Any | None:
        if self._kingshot_data_service is None:
            return None

        uid = self._extract_player_uid(player_data, ks_data)
        if uid is None:
            return None

        result = await self._kingshot_data_service.get_arena(uid)
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

        loadouts = []
        for hero in sorted(arena_data["heroes"], key=self._arena_hero_sort_key):
            if not isinstance(hero, dict):
                continue

            hero_id = hero.get("id")
            if hero_id in (None, ""):
                continue

            image_path = self.HERO_IMAGE_DIR / f"{hero_id}.png"
            if not image_path.is_file():
                logger.warning("Arena hero image missing for hero id %s at %s", hero_id, image_path)
                continue

            loadouts.append(
                {
                    "hero_path": image_path,
                    "star": self._coerce_level(hero.get("star")),
                    "exclusive_item": self._extract_hero_exclusive_item(hero),
                    "gear_items": self._extract_hero_gear_items(hero),
                }
            )

        if not loadouts:
            return None

        try:
            strip_png = self._build_hero_loadout_png(loadouts)
        except Exception as exc:
            logger.warning("Failed to build arena hero loadout image: %s", exc, exc_info=True)
            return None

        return discord.File(BytesIO(strip_png), filename="arena_loadout.png")

    @staticmethod
    def _arena_hero_sort_key(hero: Any) -> tuple[int, int]:
        if not isinstance(hero, dict):
            return (999, 0)
        try:
            slot = int(hero.get("slot") or hero.get("pos") or 999)
        except (TypeError, ValueError):
            slot = 999
        try:
            hero_id = int(hero.get("id") or 0)
        except (TypeError, ValueError):
            hero_id = 0
        return (slot, hero_id)

    def _extract_hero_exclusive_item(self, hero: dict[str, Any]) -> dict[str, Any] | None:
        eid = hero.get("exclusive_equip")
        if eid in (None, ""):
            return None

        image_path = self.HERO_GEAR_IMAGE_DIR / f"{eid}.png"
        if not image_path.is_file():
            logger.warning("Arena exclusive weapon image missing for equipment id %s at %s", eid, image_path)
            return None

        return {
            "path": image_path,
            "lv": self._coerce_level(hero.get("exclusive_equip_lv")),
        }

    def _extract_hero_gear_items(self, hero: dict[str, Any]) -> list[dict[str, Any]]:
        equipment = hero.get("equipment")
        if not isinstance(equipment, list):
            return []

        gear_items = []
        for item in sorted((item for item in equipment if isinstance(item, dict)), key=self._equipment_sort_key):
            eid = item.get("eid")
            if eid in (None, ""):
                continue

            image_path = self.HERO_GEAR_IMAGE_DIR / f"{eid}.png"
            if not image_path.is_file():
                logger.warning("Arena gear image missing for equipment id %s at %s", eid, image_path)
                continue

            gear_items.append(
                {
                    "path": image_path,
                    "slv": self._coerce_level(item.get("slv")),
                    "rlv": self._coerce_level(item.get("rlv")),
                }
            )

        return gear_items[:4]

    @staticmethod
    def _coerce_level(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _equipment_sort_key(item: dict[str, Any]) -> tuple[int, int]:
        try:
            sid = int(item.get("sid") or 999)
        except (TypeError, ValueError):
            sid = 999
        try:
            eid = int(item.get("eid") or 0)
        except (TypeError, ValueError):
            eid = 0
        return (sid, eid)

    @classmethod
    def _build_hero_loadout_png(cls, loadouts: list[dict[str, Any]]) -> bytes:
        hero_images = [
            cls._resize_rgba_fit(
                cls._read_rgba_png(loadout["hero_path"]),
                cls.ARENA_HERO_MAX_WIDTH,
                cls.ARENA_HERO_MAX_HEIGHT,
            )
            for loadout in loadouts
        ]
        star_images = [cls._build_star_row_image(loadout.get("star", 0)) for loadout in loadouts]
        exclusive_images = [
            (
                cls._annotate_exclusive_image(
                    cls._resize_rgba_nearest(
                        cls._read_rgba_png(item["path"]),
                        cls.ARENA_GEAR_ICON_SIZE,
                        cls.ARENA_GEAR_ICON_SIZE,
                    ),
                    item["lv"],
                )
                if (item := loadout.get("exclusive_item"))
                else None
            )
            for loadout in loadouts
        ]
        gear_images = [
            [
                cls._annotate_gear_image(
                    cls._resize_rgba_nearest(
                        cls._read_rgba_png(item["path"]),
                        cls.ARENA_GEAR_ICON_SIZE,
                        cls.ARENA_GEAR_ICON_SIZE,
                    ),
                    item["slv"],
                    item["rlv"],
                )
                for item in loadout["gear_items"]
            ]
            for loadout in loadouts
        ]

        column_spacing = 10
        gear_spacing = 5
        star_top_gap = 6
        exclusive_top_gap = 8
        gear_top_gap = 8
        max_hero_height = max(image["height"] for image in hero_images)
        column_widths = []
        for hero_image, star_image, exclusive_image, hero_gears in zip(
            hero_images,
            star_images,
            exclusive_images,
            gear_images,
            strict=True,
        ):
            gear_width = 0
            if hero_gears:
                gear_columns = min(2, len(hero_gears))
                gear_width = gear_columns * cls.ARENA_GEAR_ICON_SIZE + gear_spacing * (gear_columns - 1)
            star_width = star_image["width"] if star_image else 0
            exclusive_width = exclusive_image["width"] if exclusive_image else 0
            column_widths.append(max(hero_image["width"], star_width, exclusive_width, gear_width))

        width = sum(column_widths) + column_spacing * (len(column_widths) - 1)
        has_star = any(star_images)
        has_exclusive = any(exclusive_images)
        has_gear = any(gear_images)
        max_gear_rows = max(((len(gears) + 1) // 2 for gears in gear_images), default=0)
        gear_grid_height = (
            max_gear_rows * cls.ARENA_GEAR_ICON_SIZE + gear_spacing * (max_gear_rows - 1)
            if max_gear_rows
            else 0
        )
        height = max_hero_height
        if has_star:
            height += star_top_gap + cls.ARENA_STAR_ICON_SIZE
        if has_exclusive:
            height += exclusive_top_gap + cls.ARENA_GEAR_ICON_SIZE
        if has_gear:
            height += gear_top_gap + gear_grid_height
        canvas = bytearray(width * height * 4)

        offset_x = 0
        for column_width, hero_image, star_image, exclusive_image, hero_gears in zip(
            column_widths,
            hero_images,
            star_images,
            exclusive_images,
            gear_images,
            strict=True,
        ):
            hero_left = offset_x + (column_width - hero_image["width"]) // 2
            cls._paste_rgba(canvas, width, hero_image, hero_left, 0)

            gear_top = max_hero_height
            if has_star:
                if star_image:
                    star_left = offset_x + (column_width - star_image["width"]) // 2
                    cls._paste_rgba(canvas, width, star_image, star_left, max_hero_height + star_top_gap)
                gear_top += star_top_gap + cls.ARENA_STAR_ICON_SIZE

            if has_exclusive:
                if exclusive_image:
                    exclusive_left = offset_x + (column_width - exclusive_image["width"]) // 2
                    cls._paste_rgba(canvas, width, exclusive_image, exclusive_left, gear_top + exclusive_top_gap)
                gear_top += exclusive_top_gap + cls.ARENA_GEAR_ICON_SIZE

            if hero_gears:
                gear_columns = min(2, len(hero_gears))
                gear_width = gear_columns * cls.ARENA_GEAR_ICON_SIZE + gear_spacing * (gear_columns - 1)
                gear_left = offset_x + (column_width - gear_width) // 2
                gear_top += gear_top_gap
                for index, gear_image in enumerate(hero_gears):
                    column = index % gear_columns
                    row = index // gear_columns
                    left = gear_left + column * (cls.ARENA_GEAR_ICON_SIZE + gear_spacing)
                    top = gear_top + row * (cls.ARENA_GEAR_ICON_SIZE + gear_spacing)
                    cls._paste_rgba(canvas, width, gear_image, left, top)

            offset_x += column_width + column_spacing

        return cls._write_rgba_png(width, height, bytes(canvas))

    @staticmethod
    def _paste_rgba(canvas: bytearray, canvas_width: int, image: dict[str, Any], left: int, top: int) -> None:
        for row_index in range(image["height"]):
            src_start = row_index * image["width"] * 4
            src_end = src_start + image["width"] * 4
            dst_start = ((top + row_index) * canvas_width + left) * 4
            canvas[dst_start : dst_start + image["width"] * 4] = image["pixels"][src_start:src_end]

    @staticmethod
    def _resize_rgba_nearest(image: dict[str, Any], width: int, height: int) -> dict[str, Any]:
        source_width = image["width"]
        source_height = image["height"]
        source_pixels = image["pixels"]
        resized = bytearray(width * height * 4)

        for y in range(height):
            source_y = min(source_height - 1, y * source_height // height)
            for x in range(width):
                source_x = min(source_width - 1, x * source_width // width)
                src = (source_y * source_width + source_x) * 4
                dst = (y * width + x) * 4
                resized[dst : dst + 4] = source_pixels[src : src + 4]

        return {"width": width, "height": height, "pixels": bytes(resized)}

    @classmethod
    def _build_star_row_image(cls, star: int) -> dict[str, Any] | None:
        if star <= 0:
            return None

        gap = 2
        size = cls.ARENA_STAR_ICON_SIZE
        width = 5 * size + 4 * gap
        image = {"width": width, "height": size, "pixels": bytearray(width * size * 4)}
        parts = max(0, min(star, 30))
        for index in range(5):
            cls._draw_six_part_star(image, index * (size + gap), 0, max(0, min(parts - index * 6, 6)))
        image["pixels"] = bytes(image["pixels"])
        return image

    @classmethod
    def _draw_six_part_star(cls, image: dict[str, Any], left: int, top: int, filled_parts: int) -> None:
        filled = (255, 238, 142, 245)
        empty = (73, 86, 88, 180)
        outline = (116, 111, 70, 160)
        size = cls.ARENA_STAR_ICON_SIZE
        center_x = left + size // 2
        center_y = top + size // 2
        petals = (
            (center_x, top + 3),
            (left + size - 4, top + 6),
            (left + size - 4, top + size - 6),
            (center_x, top + size - 3),
            (left + 4, top + size - 6),
            (left + 4, top + 6),
        )
        for index, (x, y) in enumerate(petals):
            cls._draw_diamond(image, x, y, 4, filled if index < filled_parts else empty)
        cls._draw_diamond(image, center_x, center_y, 3, filled if filled_parts else empty)
        for x, y in petals:
            cls._draw_diamond_outline(image, x, y, 4, outline)

    @classmethod
    def _annotate_exclusive_image(cls, image: dict[str, Any], lv: int) -> dict[str, Any]:
        annotated = {
            "width": image["width"],
            "height": image["height"],
            "pixels": bytearray(image["pixels"]),
        }
        cls._draw_border(annotated, (245, 190, 68, 235))
        text = f"LV{lv}"
        text_width, text_height = cls._badge_size(text)
        cls._draw_badge(
            annotated,
            annotated["width"] - text_width - 1,
            annotated["height"] - text_height - 1,
            text,
            (88, 53, 15, 235),
            (255, 237, 176, 255),
        )
        annotated["pixels"] = bytes(annotated["pixels"])
        return annotated

    @classmethod
    def _annotate_gear_image(cls, image: dict[str, Any], slv: int, rlv: int) -> dict[str, Any]:
        annotated = {
            "width": image["width"],
            "height": image["height"],
            "pixels": bytearray(image["pixels"]),
        }
        if slv > 100:
            cls._tint_visible_pixels(annotated, (180, 24, 32, 255), opacity=0.42)
        general_text, general_background, general_foreground = cls._format_general_gear_level(slv)
        cls._draw_badge(annotated, 1, 1, general_text, general_background, general_foreground)
        mystery_text = f"LV{rlv}"
        mystery_width, mystery_height = cls._badge_size(mystery_text)
        cls._draw_badge(
            annotated,
            annotated["width"] - mystery_width - 1,
            annotated["height"] - mystery_height - 1,
            mystery_text,
            (62, 38, 86, 230),
            (232, 214, 255, 255),
        )
        annotated["pixels"] = bytes(annotated["pixels"])
        return annotated

    @classmethod
    def _draw_border(cls, image: dict[str, Any], color: tuple[int, int, int, int]) -> None:
        cls._draw_rect(image, 0, 0, image["width"], 2, color)
        cls._draw_rect(image, 0, image["height"] - 2, image["width"], 2, color)
        cls._draw_rect(image, 0, 0, 2, image["height"], color)
        cls._draw_rect(image, image["width"] - 2, 0, 2, image["height"], color)

    @staticmethod
    def _format_general_gear_level(slv: int) -> tuple[str, tuple[int, int, int, int], tuple[int, int, int, int]]:
        if slv > 100:
            return f"+{slv - 100}", (115, 28, 34, 235), (255, 235, 220, 255)
        return f"+{slv}", (35, 39, 52, 230), (255, 236, 160, 255)

    @staticmethod
    def _tint_visible_pixels(image: dict[str, Any], color: tuple[int, int, int, int], *, opacity: float) -> None:
        pixels = image["pixels"]
        tint_r, tint_g, tint_b, _ = color
        opacity = max(0.0, min(1.0, opacity))
        for offset in range(0, len(pixels), 4):
            alpha = pixels[offset + 3]
            if alpha == 0:
                continue
            pixels[offset] = round(pixels[offset] * (1 - opacity) + tint_r * opacity)
            pixels[offset + 1] = round(pixels[offset + 1] * (1 - opacity) + tint_g * opacity)
            pixels[offset + 2] = round(pixels[offset + 2] * (1 - opacity) + tint_b * opacity)

    @classmethod
    def _draw_badge(
        cls,
        image: dict[str, Any],
        x: int,
        y: int,
        text: str,
        background: tuple[int, int, int, int],
        foreground: tuple[int, int, int, int],
    ) -> None:
        width, height = cls._badge_size(text)
        cls._draw_rect(image, x, y, width, height, background)
        cls._draw_text(image, x + 2, y + 1, text, foreground, scale=3)

    @classmethod
    def _badge_size(cls, text: str) -> tuple[int, int]:
        scale = 3
        padding_x = 2
        padding_y = 1
        return (
            cls._text_width(text, scale=scale) + padding_x * 2,
            cls._text_height(scale=scale) + padding_y * 2,
        )

    @staticmethod
    def _draw_rect(image: dict[str, Any], x: int, y: int, width: int, height: int, color: tuple[int, int, int, int]) -> None:
        pixels = image["pixels"]
        image_width = image["width"]
        image_height = image["height"]
        for yy in range(max(0, y), min(image_height, y + height)):
            for xx in range(max(0, x), min(image_width, x + width)):
                offset = (yy * image_width + xx) * 4
                PlayerInfoHandler._blend_pixel(pixels, offset, color)

    @classmethod
    def _draw_diamond(cls, image: dict[str, Any], center_x: int, center_y: int, radius: int, color: tuple[int, int, int, int]) -> None:
        for y in range(center_y - radius, center_y + radius + 1):
            for x in range(center_x - radius, center_x + radius + 1):
                if abs(x - center_x) + abs(y - center_y) <= radius:
                    cls._draw_rect(image, x, y, 1, 1, color)

    @classmethod
    def _draw_diamond_outline(
        cls,
        image: dict[str, Any],
        center_x: int,
        center_y: int,
        radius: int,
        color: tuple[int, int, int, int],
    ) -> None:
        for y in range(center_y - radius, center_y + radius + 1):
            for x in range(center_x - radius, center_x + radius + 1):
                if abs(x - center_x) + abs(y - center_y) == radius:
                    cls._draw_rect(image, x, y, 1, 1, color)

    @staticmethod
    def _draw_text(
        image: dict[str, Any],
        x: int,
        y: int,
        text: str,
        color: tuple[int, int, int, int],
        *,
        scale: int = 1,
    ) -> None:
        cursor = x
        for char in text.upper():
            glyph = PlayerInfoHandler._glyph(char)
            if glyph is None:
                cursor += 2 * scale
                continue
            for row_index, row in enumerate(glyph):
                for column_index, enabled in enumerate(row):
                    if not enabled:
                        continue
                    PlayerInfoHandler._draw_rect(
                        image,
                        cursor + column_index * scale,
                        y + row_index * scale,
                        scale,
                        scale,
                        color,
                    )
            cursor += (len(glyph[0]) + 1) * scale

    @staticmethod
    def _text_width(text: str, *, scale: int) -> int:
        width = 0
        for char in text.upper():
            glyph = PlayerInfoHandler._glyph(char)
            width += ((len(glyph[0]) if glyph else 1) + 1) * scale
        return max(0, width - scale)

    @staticmethod
    def _text_height(*, scale: int) -> int:
        return 5 * scale

    @staticmethod
    def _glyph(char: str) -> tuple[tuple[int, ...], ...] | None:
        glyphs = {
            "0": ((1, 1, 1), (1, 0, 1), (1, 0, 1), (1, 0, 1), (1, 1, 1)),
            "1": ((0, 1, 0), (1, 1, 0), (0, 1, 0), (0, 1, 0), (1, 1, 1)),
            "2": ((1, 1, 1), (0, 0, 1), (1, 1, 1), (1, 0, 0), (1, 1, 1)),
            "3": ((1, 1, 1), (0, 0, 1), (0, 1, 1), (0, 0, 1), (1, 1, 1)),
            "4": ((1, 0, 1), (1, 0, 1), (1, 1, 1), (0, 0, 1), (0, 0, 1)),
            "5": ((1, 1, 1), (1, 0, 0), (1, 1, 1), (0, 0, 1), (1, 1, 1)),
            "6": ((1, 1, 1), (1, 0, 0), (1, 1, 1), (1, 0, 1), (1, 1, 1)),
            "7": ((1, 1, 1), (0, 0, 1), (0, 1, 0), (0, 1, 0), (0, 1, 0)),
            "8": ((1, 1, 1), (1, 0, 1), (1, 1, 1), (1, 0, 1), (1, 1, 1)),
            "9": ((1, 1, 1), (1, 0, 1), (1, 1, 1), (0, 0, 1), (1, 1, 1)),
            "L": ((1, 0, 0), (1, 0, 0), (1, 0, 0), (1, 0, 0), (1, 1, 1)),
            "M": ((1, 0, 1), (1, 1, 1), (1, 1, 1), (1, 0, 1), (1, 0, 1)),
            "V": ((1, 0, 1), (1, 0, 1), (1, 0, 1), (1, 0, 1), (0, 1, 0)),
            "+": ((0, 0, 0), (0, 1, 0), (1, 1, 1), (0, 1, 0), (0, 0, 0)),
        }
        return glyphs.get(char)

    @staticmethod
    def _blend_pixel(pixels: bytearray, offset: int, color: tuple[int, int, int, int]) -> None:
        src_r, src_g, src_b, src_a = color
        if src_a == 255:
            pixels[offset : offset + 4] = bytes(color)
            return

        dst_r, dst_g, dst_b, dst_a = pixels[offset : offset + 4]
        alpha = src_a / 255
        inv_alpha = 1 - alpha
        out_a = src_a + dst_a * inv_alpha
        if out_a <= 0:
            pixels[offset : offset + 4] = b"\x00\x00\x00\x00"
            return

        pixels[offset] = round((src_r * src_a + dst_r * dst_a * inv_alpha) / out_a)
        pixels[offset + 1] = round((src_g * src_a + dst_g * dst_a * inv_alpha) / out_a)
        pixels[offset + 2] = round((src_b * src_a + dst_b * dst_a * inv_alpha) / out_a)
        pixels[offset + 3] = round(out_a)

    @classmethod
    def _resize_rgba_fit(cls, image: dict[str, Any], max_width: int, max_height: int) -> dict[str, Any]:
        source_width = image["width"]
        source_height = image["height"]
        scale = min(max_width / source_width, max_height / source_height, 1)
        width = max(1, round(source_width * scale))
        height = max(1, round(source_height * scale))
        if width == source_width and height == source_height:
            return image
        return cls._resize_rgba_nearest(image, width, height)

    @staticmethod
    def _read_rgba_png(path: Path) -> dict[str, Any]:
        data = path.read_bytes()
        if data[:8] != b"\x89PNG\r\n\x1a\n":
            raise ValueError(f"{path} is not a PNG")

        pos = 8
        width = height = None
        idat = bytearray()
        while pos < len(data):
            length = struct.unpack(">I", data[pos : pos + 4])[0]
            chunk_type = data[pos + 4 : pos + 8]
            chunk_data = data[pos + 8 : pos + 8 + length]
            pos += 12 + length

            if chunk_type == b"IHDR":
                width, height, bit_depth, color_type, compression, filter_method, interlace = struct.unpack(
                    ">IIBBBBB",
                    chunk_data,
                )
                if (bit_depth, color_type, compression, filter_method, interlace) != (8, 6, 0, 0, 0):
                    raise ValueError(f"{path} must be non-interlaced 8-bit RGBA PNG")
            elif chunk_type == b"IDAT":
                idat.extend(chunk_data)
            elif chunk_type == b"IEND":
                break

        if width is None or height is None:
            raise ValueError(f"{path} is missing IHDR")

        raw = zlib.decompress(bytes(idat))
        stride = width * 4
        pixels = bytearray()
        previous = bytearray(stride)
        source = 0

        for _ in range(height):
            filter_type = raw[source]
            source += 1
            row = bytearray(raw[source : source + stride])
            source += stride
            PlayerInfoHandler._unfilter_png_row(row, previous, filter_type, 4)
            pixels.extend(row)
            previous = row

        return {"width": width, "height": height, "pixels": bytes(pixels)}

    @staticmethod
    def _unfilter_png_row(row: bytearray, previous: bytearray, filter_type: int, bytes_per_pixel: int) -> None:
        if filter_type == 0:
            return
        if filter_type == 1:
            for index in range(len(row)):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                row[index] = (row[index] + left) & 0xFF
            return
        if filter_type == 2:
            for index in range(len(row)):
                row[index] = (row[index] + previous[index]) & 0xFF
            return
        if filter_type == 3:
            for index in range(len(row)):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                up = previous[index]
                row[index] = (row[index] + ((left + up) // 2)) & 0xFF
            return
        if filter_type == 4:
            for index in range(len(row)):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                up = previous[index]
                up_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                row[index] = (row[index] + PlayerInfoHandler._paeth_predictor(left, up, up_left)) & 0xFF
            return
        raise ValueError(f"Unsupported PNG filter type {filter_type}")

    @staticmethod
    def _paeth_predictor(left: int, up: int, up_left: int) -> int:
        estimate = left + up - up_left
        distance_left = abs(estimate - left)
        distance_up = abs(estimate - up)
        distance_up_left = abs(estimate - up_left)
        if distance_left <= distance_up and distance_left <= distance_up_left:
            return left
        if distance_up <= distance_up_left:
            return up
        return up_left

    @staticmethod
    def _write_rgba_png(width: int, height: int, pixels: bytes) -> bytes:
        raw = bytearray()
        stride = width * 4
        for row_index in range(height):
            raw.append(0)
            start = row_index * stride
            raw.extend(pixels[start : start + stride])

        def chunk(chunk_type: bytes, chunk_data: bytes) -> bytes:
            checksum = zlib.crc32(chunk_type)
            checksum = zlib.crc32(chunk_data, checksum)
            return (
                struct.pack(">I", len(chunk_data))
                + chunk_type
                + chunk_data
                + struct.pack(">I", checksum & 0xFFFFFFFF)
            )

        return (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw)))
            + chunk(b"IEND", b"")
        )

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
        fid = data.get("fid") or entry.get("fid") or entry.get("playerId")
        uid = data.get("uid") or entry.get("uid")
        player = entry.get("player")
        if uid is None and isinstance(player, dict):
            uid = player.get("uid")
        return {
            "name": data.get("name") or entry.get("name"),
            "playerId": str(fid) if fid is not None else "N/A",
            "playerUid": str(uid) if uid is not None else None,
            "level": data.get("stove_lv") or data.get("castle_level") or data.get("lv"),
            "kingdom": data.get("kid") or entry.get("kid") or kingdom_number,
        }

    @staticmethod
    def _format_kingshot_profile_summary(player_data: dict[str, Any]) -> str:
        lines = []
        if player_data.get("name"):
            lines.append(f"👤 **Name:** {player_data['name']}")
        if player_data.get("playerId"):
            lines.append(f"🆔 **ID:** {player_data['playerId']}")
        if player_data.get("level"):
            lines.append(f"🏰 **Castle Level:** Level {player_data['level']}")
        if player_data.get("kingdom"):
            lines.append(f"🌍 **Kingdom:** {player_data['kingdom']}")
        return "\n".join(lines) or "No data available"

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
    def _format_mystic_trial(cls, mystic_trial: dict[str, Any] | None) -> str:
        if not mystic_trial:
            return "Kingdom Rank: N/A\nScore: N/A"

        entry = mystic_trial.get("entry")
        if not isinstance(entry, dict):
            entry = {}

        rank = entry.get("rank") or mystic_trial.get("rank")
        score = entry.get("score") if entry.get("score") is not None else mystic_trial.get("score")
        rank_text = f"#{cls._format_number(rank)}" if rank is not None else "N/A"
        score_text = cls._format_number(score) if score is not None else "N/A"
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
