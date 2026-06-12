import logging
from datetime import datetime, timezone

import discord
from discord.ext import commands

from config.bot_config import BotConfig
from services.database_health_service import DatabaseHealthService
from handlers.ui import EmbedColors, build_status_embed, status_value

logger = logging.getLogger(__name__)


class StatusHandler:
    """Handles operational status commands."""

    def __init__(
        self,
        bot: commands.Bot,
        config: BotConfig,
        database_health_service: DatabaseHealthService | None = None,
        *,
        event_handler,
        gift_code_handler,
        started_at: datetime,
    ):
        self._bot = bot
        self._config = config
        self._event_handler = event_handler
        self._gift_code_handler = gift_code_handler
        self._started_at = started_at
        self._database_health_service = database_health_service or DatabaseHealthService()
        logger.info("StatusHandler initialized")

    def register_commands(self):
        """Register bot health/status commands."""

        @self._bot.tree.command(name="status", description="Show bot health and runtime status")
        async def status(interaction: discord.Interaction):
            await self._handle_status(interaction)

    async def _handle_status(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)

        db_ok, db_detail = await self._check_database()
        scheduler_running = self._event_handler.is_scheduler_running()
        gift_polling_running = self._gift_code_handler.is_polling_running()

        now = datetime.now(timezone.utc)
        uptime_seconds = int((now - self._started_at).total_seconds())
        color = EmbedColors.SUCCESS if db_ok and scheduler_running and gift_polling_running else EmbedColors.WARNING

        embed = build_status_embed(
            title="Bot Status",
            description="Operational health for DS Translator / AI Clown.",
            color=color,
            footer="Private status response",
        )
        embed.add_field(name="Discord", value=status_value(not self._bot.is_closed()), inline=True)
        embed.add_field(name="Latency", value=f"{self._bot.latency * 1000:.0f} ms", inline=True)
        embed.add_field(name="Uptime", value=self._format_duration(uptime_seconds), inline=True)
        embed.add_field(name="Database", value=status_value(db_ok, "Reachable", db_detail), inline=True)
        embed.add_field(name="Scheduler", value=status_value(scheduler_running, "Running", "Stopped"), inline=True)
        embed.add_field(name="Gift Polling", value=status_value(gift_polling_running, "Running", "Stopped"), inline=True)
        embed.add_field(name="Guilds", value=str(len(self._bot.guilds)), inline=True)
        embed.add_field(name="AI Model", value=f"`{self._config.nvidia_model}`", inline=True)
        embed.add_field(name="Chat Context", value=f"{self._config.chat_history_limit} messages", inline=True)
        embed.add_field(name="Random Replies", value=f"{self._config.random_reply_chance:.0%}", inline=True)
        embed.add_field(name="Auto-Redeem Channels", value=str(len(self._config.auto_redeem_channels)), inline=True)

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _check_database(self) -> tuple[bool, str]:
        return await self._database_health_service.check_database()

    @staticmethod
    def _format_duration(total_seconds: int) -> str:
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        if days:
            return f"{days}d {hours}h {minutes}m"
        if hours:
            return f"{hours}h {minutes}m"
        return f"{minutes}m"
