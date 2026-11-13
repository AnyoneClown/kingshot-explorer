"""
Discord Translation Bot - Main Entry Point
Follows SOLID principles for maintainability and extensibility.
"""

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from typing import Awaitable, Callable

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
from openai import AsyncOpenAI

from config import BotConfig
from config.logging_config import setup_logging
from db import init_db
from handlers import (
    CalendarHandler,
    EventHandler,
    GiftCodeHandler,
    GuildConfigHandler,
    HelpHandler,
    KVKHandler,
    LeaderboardHandler,
    PlayerInfoHandler,
    StatusHandler,
    TranslationHandler,
)
from handlers.permissions import AccessPolicy, build_denied_embed
from services.chat_tools import build_chat_tool_registry
from services.kvk_dossier_service import KvKDossierService
from services.leaderboard_service import LeaderboardService
from services.usage_stats_service import UsageStatsService
from services import (
    EventSchedulerService,
    GiftCodeService,
    GuildConfigurationService,
    InteractionTrackingService,
    ChatbotService,
    KingshotCalendarService,
    KingshotRAGService,
    KVKService,
    PlayerInfoService,
    PlayerRegistryService,
    TranslationService,
    DatabaseHealthService,
    KingshotDataService,
    MightPulseService,
    MightPulseAllianceDirectory,
)

logger = logging.getLogger(__name__)

# Never let user- or model-controlled text ping @everyone, @here or roles. Messages
# that should ping (event reminders, job completion) opt in explicitly.
DEFAULT_ALLOWED_MENTIONS = discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=False)


class BotCommandTree(app_commands.CommandTree):
    """Command tree with a global ban check and a safety-net error handler."""

    access_policy: AccessPolicy | None = None
    usage_stats = None

    def _count_usage(self, interaction: discord.Interaction) -> None:
        """Count slash and context-menu commands (not autocomplete or buttons)."""
        stats = self.usage_stats
        command = interaction.command
        if stats is None or command is None or interaction.type is not discord.InteractionType.application_command:
            return
        try:
            stats.record(command.qualified_name, interaction.guild_id)
        except Exception:
            logger.debug("Could not count command usage", exc_info=True)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        policy = self.access_policy
        if policy is not None and policy.is_banned(interaction.user.id):
            logger.info("Ignored interaction from banned user %s", interaction.user.id)
            # Autocomplete interactions cannot receive a message response.
            if interaction.type is discord.InteractionType.autocomplete:
                return False
            embed = build_denied_embed("You can't use this bot.")
            if not interaction.response.is_done():
                try:
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                except discord.HTTPException:
                    logger.debug("Could not notify banned user", exc_info=True)
            return False
        self._count_usage(interaction)
        return True

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        command_name = interaction.command.qualified_name if interaction.command else "unknown"
        # CommandOnCooldown subclasses CheckFailure, so it must be tested first.
        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"Slow down — try again in {error.retry_after:.0f}s."
        elif isinstance(error, app_commands.CheckFailure):
            logger.info("Check failed for /%s by %s: %s", command_name, interaction.user.id, error)
            message = "You can't use this command here."
        else:
            original = getattr(error, "original", error)
            logger.error(
                "Unhandled error in /%s (user=%s guild=%s)",
                command_name,
                interaction.user.id,
                interaction.guild_id,
                exc_info=(type(original), original, original.__traceback__),
            )
            message = "Something went wrong while running this command. Please try again in a moment."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.HTTPException:
            logger.debug("Could not report command error to the user", exc_info=True)


class DSBot(commands.Bot):
    """Bot with graceful shutdown on SIGTERM and ordered resource cleanup."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._cleanup_callbacks: list[tuple[str, Callable[[], Awaitable[object]]]] = []
        self._closing = False

    def add_cleanup(self, name: str, callback: Callable[[], Awaitable[object]]) -> None:
        """Register an async cleanup step that runs while the event loop is alive."""
        self._cleanup_callbacks.append((name, callback))

    async def setup_hook(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, lambda s=sig: asyncio.create_task(self._shutdown(s)))
            except (NotImplementedError, RuntimeError):
                logger.debug("Signal handlers are not supported on this platform")

    async def _shutdown(self, sig: signal.Signals) -> None:
        logger.info("Received %s, shutting down gracefully", sig.name)
        await self.close()

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        # Run in reverse registration order: stop background work before closing the
        # clients and the database pool it uses.
        for name, callback in reversed(self._cleanup_callbacks):
            try:
                await asyncio.wait_for(callback(), timeout=15)
                logger.info("Shutdown step completed: %s", name)
            except Exception:
                logger.exception("Shutdown step failed: %s", name)
        await super().close()


class TranslatorBot:
    """Main bot class - Dependency Injection and Single Responsibility."""

    def __init__(self, config: BotConfig):
        """
        Initialize the bot with configuration and services.

        Args:
            config: Bot configuration object
        """
        self.config = config
        self.started_at = datetime.now(timezone.utc)
        self._has_logged_ready = False
        self._has_synced_commands = False
        logger.info("Initializing TranslatorBot")

        # Initialize database
        logger.info("Initializing database...")
        self.db_manager = init_db(config.database_url)
        logger.info("Database manager initialized")

        # Setup Discord bot
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        self.bot = DSBot(
            command_prefix=config.command_prefix,
            intents=intents,
            allowed_mentions=DEFAULT_ALLOWED_MENTIONS,
            tree_cls=BotCommandTree,
        )
        self.bot.add_cleanup("database pool", self.db_manager.close)
        logger.info(f"Discord bot created with prefix: {config.command_prefix}")

        # Initialize services
        logger.info("Initializing services...")
        self.nvidia_client = AsyncOpenAI(
            base_url=config.nvidia_base_url,
            api_key=config.nvidia_api_key,
            timeout=config.llm_timeout_seconds,
            max_retries=1,
        )
        self.bot.add_cleanup("NVIDIA client", self.nvidia_client.close)
        self.translation_service = TranslationService(
            self.nvidia_client,
            model=config.nvidia_model,
        )
        self.event_scheduler_service = EventSchedulerService(self.db_manager)
        self.gift_code_service = GiftCodeService(self.db_manager)
        self.player_registry_service = PlayerRegistryService(self.db_manager)
        self.guild_configuration_service = GuildConfigurationService(
            default_use_random_replies=False,
            db_manager=self.db_manager,
        )
        self.access_policy = AccessPolicy(
            config.admin_user_ids,
            guild_configuration_service=self.guild_configuration_service,
            banned_user_ids=config.banned_players,
        )
        self.bot.tree.access_policy = self.access_policy
        self.interaction_tracking_service = InteractionTrackingService(self.db_manager)
        self.usage_stats_service = UsageStatsService(self.db_manager)
        self.bot.tree.usage_stats = self.usage_stats_service
        # Registered right after the pool, so the last counters are saved before it closes.
        self.bot.add_cleanup("command usage counters", self.usage_stats_service.flush)
        self.database_health_service = DatabaseHealthService(self.db_manager)
        self.kvk_service = KVKService()
        self.bot.add_cleanup("KVK history client", self.kvk_service.close)
        self.kingshot_calendar_service = KingshotCalendarService()
        self.bot.add_cleanup("event calendar client", self.kingshot_calendar_service.close)
        self.kingshot_data_service = KingshotDataService(
            api_key=config.ks_data_api_key,
            base_url=config.ks_data_base_url,
            timeout_seconds=config.ks_data_timeout_seconds,
        )
        self.bot.add_cleanup("KingShot Data API client", self.kingshot_data_service.close)
        self.player_info_service = PlayerInfoService(self.kingshot_data_service)
        self.mightpulse_service = (
            MightPulseService(config.mightpulse_api_key, base_url=config.mightpulse_base_url)
            if config.mightpulse_api_key else None
        )
        self.alliance_directory_service = MightPulseAllianceDirectory()
        self.kingshot_rag_service = KingshotRAGService(
            self.db_manager,
            self.nvidia_client,
            embedding_model=config.nvidia_embedding_model,
            chat_model=config.nvidia_model,
            max_distance=config.kingshot_rag_max_distance,
        )
        self.kvk_dossier_service = KvKDossierService(self.kingshot_data_service, self.kvk_service)
        self.leaderboard_service = LeaderboardService(self.kingshot_data_service)
        # Chat replies use the same OpenAI-compatible NIM client. Direct mentions may call
        # read-only, server-scoped tools through the tool-capable NVIDIA_MODEL.
        self.chat_tool_registry = build_chat_tool_registry(
            event_scheduler_service=self.event_scheduler_service,
            gift_code_service=self.gift_code_service,
            kingshot_data_service=self.kingshot_data_service,
            rag_service=self.kingshot_rag_service,
            calendar_service=self.kingshot_calendar_service,
            guild_configuration_service=self.guild_configuration_service,
        )
        self.chatbot_service = ChatbotService(
            self.nvidia_client,
            model=config.nvidia_chat_model,
            seed=42,
            tool_model=config.nvidia_model,
            gate_model=config.nvidia_model,
            tools=self.chat_tool_registry,
            max_chat_response_chars=config.max_chat_response_chars,
        )
        logger.info("All services initialized")

        # Initialize handlers
        logger.info("Initializing handlers...")
        self.translation_handler = TranslationHandler(
            self.translation_service,
            self.chatbot_service,
            self.bot,
            config,
            interaction_tracking_service=self.interaction_tracking_service,
            guild_configuration_service=self.guild_configuration_service,
            access_policy=self.access_policy,
        )
        self.event_handler = EventHandler(
            self.event_scheduler_service,
            self.bot,
            admin_user_ids=config.admin_user_ids,
            access_policy=self.access_policy,
            guild_configuration_service=self.guild_configuration_service,
        )
        self.player_info_handler = PlayerInfoHandler(
            self.player_info_service,
            self.bot,
            interaction_tracking_service=self.interaction_tracking_service,
            kingshot_data_service=self.kingshot_data_service,
            guild_configuration_service=self.guild_configuration_service,
        )
        self.kvk_handler = KVKHandler(
            self.kvk_service,
            self.bot,
            dossier_service=self.kvk_dossier_service,
            guild_configuration_service=self.guild_configuration_service,
        )
        self.leaderboard_handler = LeaderboardHandler(
            self.leaderboard_service,
            self.bot,
            guild_configuration_service=self.guild_configuration_service,
        )
        self.gift_code_handler = GiftCodeHandler(
            self.gift_code_service,
            self.player_info_service,
            self.bot,
            config,
            interaction_tracking_service=self.interaction_tracking_service,
            player_registry_service=self.player_registry_service,
            kingshot_data_service=self.kingshot_data_service,
            guild_configuration_service=self.guild_configuration_service,
            access_policy=self.access_policy,
            mightpulse_service=self.mightpulse_service,
            alliance_directory_service=self.alliance_directory_service,
        )
        self.calendar_handler = CalendarHandler(
            self.kingshot_calendar_service,
            self.bot,
            guild_configuration_service=self.guild_configuration_service,
            access_policy=self.access_policy,
        )
        self.guild_config_handler = GuildConfigHandler(
            self.bot,
            self.guild_configuration_service,
            admin_user_ids=config.admin_user_ids,
            access_policy=self.access_policy,
            calendar_handler=self.calendar_handler,
            gift_handler=self.gift_code_handler,
        )

        self.status_handler = StatusHandler(
            self.bot,
            config,
            event_handler=self.event_handler,
            gift_code_handler=self.gift_code_handler,
            calendar_handler=self.calendar_handler,
            database_health_service=self.database_health_service,
            kingshot_data_service=self.kingshot_data_service,
            started_at=self.started_at,
            access_policy=self.access_policy,
            usage_stats_service=self.usage_stats_service,
        )
        self.help_handler = HelpHandler(
            self.bot,
            admin_user_ids=config.admin_user_ids,
            access_policy=self.access_policy,
        )

        # Shutdown runs in reverse registration order: these stop background work
        # before the API clients and the database pool above are closed.
        self.bot.add_cleanup("daily calendar posts", self._stop_calendar_posts)
        self.bot.add_cleanup("event scheduler", self._stop_event_scheduler)
        self.bot.add_cleanup("maintenance", self._stop_maintenance)
        self._maintenance_loop = tasks.loop(hours=24)(self._run_maintenance)
        self._usage_flush_loop = tasks.loop(minutes=5)(self.usage_stats_service.flush)
        logger.info("All handlers initialized")

        # Setup bot
        self._setup_events()
        self._setup_handlers()
        logger.info("Bot setup complete")

    def _setup_events(self):
        """Register bot events."""

        @self.bot.event
        async def on_ready():
            guilds_summary = ", ".join(f"{guild.name}({guild.id})" for guild in self.bot.guilds)
            if not self._has_logged_ready:
                logger.info(
                    "Bot ready: user=%s id=%s prefix=%s guilds=%s [%s]",
                    self.bot.user,
                    self.bot.user.id,
                    self.config.command_prefix,
                    len(self.bot.guilds),
                    guilds_summary,
                )
                self._has_logged_ready = True
            else:
                logger.info("Bot reconnected: user=%s guilds=%s", self.bot.user, len(self.bot.guilds))

            # Sync slash commands
            if not self._has_synced_commands:
                try:
                    logger.info("Syncing slash commands...")
                    synced = await self.bot.tree.sync()
                    # /help mentions commands by ID, so they are clickable.
                    self.help_handler.remember_commands(synced)
                    command_names = ", ".join(f"/{cmd.name}" for cmd in synced)
                    logger.info("Synced %s slash command(s): %s", len(synced), command_names)
                    self._has_synced_commands = True
                except Exception as e:
                    logger.error(f"Failed to sync slash commands: {e}")

            # Buttons of gift-code panels posted before this start work again.
            self.gift_code_handler.register_persistent_views()
            self.calendar_handler.register_persistent_views()

            self.event_handler.start_scheduler_task()
            logger.info("Event scheduler task started")

            self.gift_code_handler.start_polling_task()
            logger.info("Auto gift code polling task started")
            self.calendar_handler.start_daily_task()
            if not self._maintenance_loop.is_running():
                self._maintenance_loop.start()
            if not self._usage_flush_loop.is_running():
                self._usage_flush_loop.start()

        @self.bot.event
        async def on_command_error(ctx, error):
            """Handle command errors."""
            # Replacing Bot.on_command_error drops discord.py's own skip for commands with a
            # local error handler (!t, !en): without this check they would get two replies.
            if ctx.command is not None and ctx.command.has_error_handler():
                return
            if isinstance(error, commands.CommandNotFound):
                logger.warning(f"Unknown command attempted by {ctx.author}: {ctx.message.content}")
            elif isinstance(error, commands.MissingRequiredArgument):
                logger.warning(f"Missing argument for command by {ctx.author}: {ctx.command}")
                await ctx.send(
                    f"❌ Missing required argument: `{error.param.name}`. "
                    f"Use `!help {ctx.command}` to see command usage."
                )
            elif isinstance(error, commands.BadArgument):
                logger.warning(f"Bad argument for command by {ctx.author}: {ctx.command} - {error}")
                await ctx.send("❌ Invalid argument provided. Please check command format with `!help`.")
            else:
                logger.error(
                    f"Command error in {ctx.command} by {ctx.author}: {error}",
                    exc_info=error,
                )
                await ctx.send("❌ An unexpected error occurred while processing the command.")

        @self.bot.event
        async def on_guild_join(guild):
            """Log the join and post a short setup hint for the server's managers."""
            logger.info(f"Bot joined new guild: {guild.name} (ID: {guild.id}) - {guild.member_count} members")
            await self._send_welcome(guild)

        @self.bot.event
        async def on_guild_remove(guild):
            """Log when bot leaves a guild."""
            logger.info(f"Bot removed from guild: {guild.name} (ID: {guild.id})")

    async def _stop_event_scheduler(self) -> None:
        await self.event_handler.shutdown_scheduler()

    async def _stop_calendar_posts(self) -> None:
        self.calendar_handler.stop_daily_task()

    async def _stop_maintenance(self) -> None:
        if self._maintenance_loop.is_running():
            self._maintenance_loop.cancel()
        if self._usage_flush_loop.is_running():
            self._usage_flush_loop.cancel()

    async def _run_maintenance(self) -> None:
        """Daily housekeeping: drop stored data past its retention.

        Each step is independent: one failing purge must not skip the others or stop
        the daily loop.
        """
        for name, purge in (
            ("old gift-code announcement records", self.gift_code_service.purge_announcements),
            ("old command usage counters", self.usage_stats_service.purge_old),
        ):
            try:
                deleted = await purge()
                if deleted:
                    logger.info("Purged %s %s", deleted, name)
            except Exception:
                logger.exception("Purge of %s failed", name)
        retention_days = self.config.translation_log_retention_days
        purge = getattr(self.interaction_tracking_service, "purge_old_translation_logs", None)
        if retention_days <= 0 or purge is None:
            return
        try:
            deleted = await purge(retention_days)
            logger.info("Purged %s translation log rows older than %s days", deleted, retention_days)
        except Exception:
            logger.exception("Translation log purge failed")

    async def _send_welcome(self, guild: discord.Guild) -> None:
        channel = guild.system_channel
        me = guild.me
        if channel is None or me is None or not channel.permissions_for(me).send_messages:
            channel = next(
                (c for c in guild.text_channels if me is not None and c.permissions_for(me).send_messages),
                None,
            )
        if channel is None:
            return
        embed = discord.Embed(
            title=f"Thanks for adding {getattr(self.bot.user, 'display_name', None) or 'the bot'}",
            description=(
                "**Server managers:** run `/settings`. Its three-step checklist sets the server's kingdom, "
                "the gift-code channel with the sign-up panel, and the daily event calendar.\n"
                "**Everyone:** run `/gift` to get new gift codes redeemed for you automatically, and `/help` "
                "for everything else: player lookups, rankings, KvK, the event calendar, reminders and "
                "translation (right-click a message → Apps → Translate)."
            ),
            color=discord.Color.blurple(),
        )
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            logger.debug("Could not send welcome message in guild %s", guild.id, exc_info=True)

    def _setup_handlers(self):
        """Register all command and event handlers."""
        logger.info("Registering command handlers...")
        self.player_info_handler.register_commands()
        self.kvk_handler.register_commands()
        self.leaderboard_handler.register_commands()
        self.translation_handler.register_commands()
        self.translation_handler.register_events()
        self.event_handler.register_commands()
        self.gift_code_handler.register_commands()
        self.calendar_handler.register_commands()
        self.guild_config_handler.register_commands()
        self.status_handler.register_commands()
        self.help_handler.register_commands()
        logger.info("All command handlers registered")

    def run(self):
        """Start the bot. Cleanup runs inside ``DSBot.close`` on the live event loop."""
        logger.info("Starting Discord bot...")
        try:
            # log_handler=None keeps our own logging configuration.
            self.bot.run(self.config.discord_token, log_handler=None)
        except KeyboardInterrupt:
            logger.info("Bot shutdown requested by user")
        except Exception as e:
            logger.critical(f"Fatal error running bot: {e}", exc_info=True)
            raise


def main():
    """Main entry point for the application."""
    load_dotenv()

    # Setup logging first
    log_level = os.getenv("LOG_LEVEL", "INFO")
    setup_logging(log_level)

    logger.info("Starting Discord Translator Bot application")

    try:
        config = BotConfig.from_env()
        bot = TranslatorBot(config)
        bot.run()
    except ValueError as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(2)
    except Exception as e:
        logger.critical(f"Failed to start bot: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
