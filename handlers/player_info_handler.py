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
    SCOUT_BOARD_TYPE_POWER = 8
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

        @self._bot.tree.command(name="scout", description="Scout top power players in a kingdom")
        @app_commands.describe(
            kingdom_number="Kingdom number to scout",
            limit="Number of leaderboard players to scout, default 5, max 15",
        )
        async def scout_kingdom(
            interaction: discord.Interaction,
            kingdom_number: int,
            limit: int = default_scout_limit,
        ):
            """Scout top power players in a kingdom."""
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

            hero_file = await self._get_arena_hero_strip(player_data, ks_data)
            if hero_file:
                embed.set_image(url="attachment://arena_heroes.png")
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
                self.SCOUT_BOARD_TYPE_POWER,
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
                        description="No power leaderboard entries were returned for this kingdom.",
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

            embeds = []
            for entry in entries:
                fid = self._extract_entry_fid(entry)
                profile = profiles_by_fid.get(str(fid)) if fid is not None else None
                embeds.append(self._build_scout_player_embed(entry, profile, kingdom_number))

            await interaction.followup.send(
                content=f"🔎 Scout report for Kingdom {kingdom_number} • Top {len(embeds)} from leaderboard type 8",
                embeds=embeds,
            )
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

    async def _get_arena_hero_strip(
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

        image_paths = []
        for hero in arena_data["heroes"]:
            if not isinstance(hero, dict):
                continue

            hero_id = hero.get("id")
            if hero_id in (None, ""):
                continue

            image_path = self.HERO_IMAGE_DIR / f"{hero_id}.png"
            if not image_path.is_file():
                logger.warning("Arena hero image missing for hero id %s at %s", hero_id, image_path)
                continue

            image_paths.append(image_path)

        if not image_paths:
            return None

        try:
            strip_png = self._build_hero_strip_png(image_paths)
        except Exception as exc:
            logger.warning("Failed to build arena hero image strip: %s", exc, exc_info=True)
            return None

        return discord.File(BytesIO(strip_png), filename="arena_heroes.png")

    @classmethod
    def _build_hero_strip_png(cls, image_paths: list[Path]) -> bytes:
        images = [cls._read_rgba_png(path) for path in image_paths]
        spacing = 10
        width = sum(image["width"] for image in images) + spacing * (len(images) - 1)
        height = max(image["height"] for image in images)
        canvas = bytearray(width * height * 4)

        offset_x = 0
        for image in images:
            top = (height - image["height"]) // 2
            for row_index in range(image["height"]):
                src_start = row_index * image["width"] * 4
                src_end = src_start + image["width"] * 4
                dst_start = ((top + row_index) * width + offset_x) * 4
                canvas[dst_start : dst_start + image["width"] * 4] = image["pixels"][src_start:src_end]
            offset_x += image["width"] + spacing

        return cls._write_rgba_png(width, height, bytes(canvas))

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

    @classmethod
    def _build_scout_player_embed(
        cls,
        entry: dict[str, Any],
        profile: dict[str, Any] | None,
        kingdom_number: int,
    ) -> discord.Embed:
        data = profile or entry
        fid = cls._extract_entry_fid(entry) or data.get("fid")
        player_name = data.get("name") or entry.get("name") or f"Player {fid or '?'}"
        player_data = cls._build_player_data_from_kingshot(data, entry, kingdom_number)
        rank = entry.get("rank", "?")

        embed = discord.Embed(
            title=f"📊 #{rank} {player_name}",
            description=cls._format_kingshot_profile_summary(player_data),
            color=discord.Color.blue(),
        )
        embed.add_field(name="Player ID", value=f"`{player_data.get('playerId', fid or 'N/A')}`", inline=True)
        embed.add_field(name="Kingdom", value=str(player_data.get("kingdom", "N/A")), inline=True)
        embed.add_field(name="Castle Level", value=str(player_data.get("level") or "N/A"), inline=True)
        embed.add_field(name="Power", value=cls._format_power(data), inline=True)
        embed.add_field(name="VIP Level", value=cls._format_vip(data), inline=True)
        embed.add_field(name="Alliance", value=cls._format_alliance(data), inline=True)
        embed.add_field(name="Links", value=cls._format_data_links(str(fid or ""), player_data, data), inline=False)
        embed.set_footer(text="Data from kingshot.jeab.dev • Use /addplayer to include this player in auto-redeem")
        return embed

    @staticmethod
    def _build_player_data_from_kingshot(
        data: dict[str, Any],
        entry: dict[str, Any],
        kingdom_number: int,
    ) -> dict[str, Any]:
        fid = data.get("fid") or entry.get("fid") or entry.get("playerId")
        return {
            "name": data.get("name") or entry.get("name"),
            "playerId": str(fid) if fid is not None else "N/A",
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
