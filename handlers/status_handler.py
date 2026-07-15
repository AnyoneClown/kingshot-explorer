import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Protocol

import discord
from discord.ext import commands

from config.bot_config import BotConfig
from handlers.ui import EmbedColors, build_status_embed, status_value
from services.database_health_service import DatabaseHealthService

logger = logging.getLogger(__name__)


class _KingshotDataHealthService(Protocol):
    async def get_health(self) -> dict[str, Any]: ...


class StatusHandler:
    """Handles operational status commands."""

    def __init__(
        self,
        bot: commands.Bot,
        config: BotConfig,
        database_health_service: DatabaseHealthService | None = None,
        *,
        event_handler=None,
        gift_code_handler=None,
        kingshot_data_service: _KingshotDataHealthService | None = None,
        started_at: datetime,
        scheduler_enabled: bool | None = None,
        gift_polling_enabled: bool | None = None,
    ):
        self._bot = bot
        self._config = config
        self._event_handler = event_handler
        self._gift_code_handler = gift_code_handler
        self._kingshot_data_service = kingshot_data_service
        self._started_at = started_at
        self._database_health_service = database_health_service or DatabaseHealthService()
        self._scheduler_enabled = (
            config.bot_profile == "local" if scheduler_enabled is None else scheduler_enabled
        )
        self._gift_polling_enabled = (
            config.bot_profile == "local" if gift_polling_enabled is None else gift_polling_enabled
        )
        logger.info("StatusHandler initialized")

    def register_commands(self):
        """Register bot health/status commands."""

        @self._bot.tree.command(name="status", description="Show bot health and runtime status")
        async def status(interaction: discord.Interaction):
            await self._handle_status(interaction)

    async def _handle_status(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True, ephemeral=True)

        (db_ok, db_detail), data_api_health = await asyncio.gather(
            self._check_database(),
            self._check_data_api(),
        )
        scheduler_state = self._get_worker_state(
            enabled=self._scheduler_enabled,
            handler=self._event_handler,
            status_method="is_scheduler_running",
            component="event scheduler",
        )
        gift_polling_state = self._get_worker_state(
            enabled=self._gift_polling_enabled,
            handler=self._gift_code_handler,
            status_method="is_polling_running",
            component="gift-code polling",
        )

        now = datetime.now(timezone.utc)
        started_at = self._started_at
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        uptime_seconds = max(0, int((now - started_at).total_seconds()))

        required_checks = [not self._bot.is_closed(), db_ok]
        if data_api_health is not None:
            required_checks.append(data_api_health[0])
        if self._scheduler_enabled:
            required_checks.append(scheduler_state == "running")
        if self._gift_polling_enabled:
            required_checks.append(gift_polling_state == "running")
        color = EmbedColors.SUCCESS if all(required_checks) else EmbedColors.WARNING

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
        if data_api_health is None:
            data_api_value = self._component_value("disabled")
        else:
            data_api_value = status_value(data_api_health[0], "Reachable", data_api_health[1])
        embed.add_field(name="KingShot Data API", value=data_api_value, inline=True)
        embed.add_field(name="Scheduler", value=self._component_value(scheduler_state), inline=True)
        embed.add_field(name="Gift Polling", value=self._component_value(gift_polling_state), inline=True)
        embed.add_field(name="Guilds", value=str(len(self._bot.guilds)), inline=True)
        if self._config.bot_profile == "local":
            embed.add_field(name="AI Model", value=f"`{self._config.nvidia_model}`", inline=True)
            embed.add_field(name="Chat Context", value=f"{self._config.chat_history_limit} messages", inline=True)
            embed.add_field(name="Random Replies", value=f"{self._config.random_reply_chance:.0%}", inline=True)
            embed.add_field(
                name="Auto-Redeem Channels",
                value=str(len(self._config.auto_redeem_channels)),
                inline=True,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _check_database(self) -> tuple[bool, str]:
        try:
            ok, _detail = await self._database_health_service.check_database()
        except Exception:
            logger.exception("Database health service failed")
            return False, "Unavailable"
        return (True, "Reachable") if ok else (False, "Unavailable")

    async def _check_data_api(self) -> tuple[bool, str] | None:
        if self._kingshot_data_service is None:
            return None

        try:
            result = await self._kingshot_data_service.get_health()
        except Exception:
            logger.exception("KingShot Data API health check failed")
            return False, "Unavailable"

        if not isinstance(result, dict) or result.get("success") is not True:
            return False, "Unavailable"

        data = result.get("data")
        if isinstance(data, dict):
            status = str(data.get("status", "")).strip().lower()
            if status in {"degraded", "down", "error", "failed", "unhealthy"} or data.get(
                "connected"
            ) is False:
                return False, "Degraded"
        return True, "Reachable"

    @staticmethod
    def _get_worker_state(
        *,
        enabled: bool,
        handler: Any,
        status_method: str,
        component: str,
    ) -> str:
        if not enabled:
            return "disabled"
        if handler is None:
            return "stopped"
        try:
            return "running" if bool(getattr(handler, status_method)()) else "stopped"
        except Exception:
            logger.exception("Could not inspect %s status", component)
            return "stopped"

    @staticmethod
    def _component_value(state: str) -> str:
        if state == "running":
            return "OK - Running"
        if state == "disabled":
            return "INFO - Disabled"
        return "ISSUE - Stopped"

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
