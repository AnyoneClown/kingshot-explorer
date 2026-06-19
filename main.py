"""
Discord Translation Bot - Main Entry Point
Follows SOLID principles for maintainability and extensibility.
"""

import logging
import os
from datetime import datetime, timezone

import discord
from discord.ext import commands
from dotenv import load_dotenv
from openai import AsyncOpenAI

from config import BotConfig
from config.logging_config import setup_logging
from db import init_db
from handlers import (
    DatabaseHandler,
    EventHandler,
    GiftCodeHandler,
    GuildConfigHandler,
    KingshotRAGHandler,
    KVKHandler,
    PlayerInfoHandler,
    StatusHandler,
    TranslationHandler,
)
from services import (
    EventSchedulerService,
    GiftCodeService,
    GuildConfigurationService,
    InteractionTrackingService,
    ChatbotService,
    KingshotRAGService,
    KVKService,
    PlayerInfoService,
    PlayerRegistryService,
    TranslationService,
    DatabaseHealthService,
    KingshotDataService,
    VoiceMessageService,
)

logger = logging.getLogger(__name__)


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
        self.is_local_profile = config.bot_profile == "local"
        self._has_logged_ready = False
        self._has_synced_commands = False
        logger.info("Initializing TranslatorBot with %s profile", config.bot_profile)

        # Initialize database
        logger.info("Initializing database...")
        self.db_manager = init_db(config.database_url)
        logger.info("Database manager initialized")

        # Setup Discord bot
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        self.bot = commands.Bot(command_prefix=config.command_prefix, intents=intents)
        logger.info(f"Discord bot created with prefix: {config.command_prefix}")

        # Initialize services
        logger.info("Initializing services...")
        self.nvidia_client = None
        self.translation_service = None
        self.chatbot_service = None
        self.event_scheduler_service = None
        self.gift_code_service = None
        self.player_registry_service = None
        self.guild_configuration_service = None
        self.kingshot_rag_service = None
        self.voice_message_service = None

        self.player_info_service = PlayerInfoService()
        self.interaction_tracking_service = InteractionTrackingService(self.db_manager)
        self.database_health_service = DatabaseHealthService(self.db_manager)
        self.kvk_service = KVKService()
        self.kingshot_data_service = KingshotDataService(
            api_key=config.ks_data_api_key,
            base_url=config.ks_data_base_url,
            timeout_seconds=config.ks_data_timeout_seconds,
        )

        if self.is_local_profile:
            self.nvidia_client = AsyncOpenAI(
                base_url=config.nvidia_base_url,
                api_key=config.nvidia_api_key,
            )
            self.translation_service = TranslationService(
                self.nvidia_client,
                model=config.nvidia_model,
            )
            self.chatbot_service = ChatbotService(
                self.nvidia_client,
                model=config.nvidia_model,
                max_chat_response_chars=config.max_chat_response_chars,
            )
            self.event_scheduler_service = EventSchedulerService(self.db_manager)
            self.gift_code_service = GiftCodeService(self.db_manager)
            self.player_registry_service = PlayerRegistryService(self.db_manager)
            self.guild_configuration_service = GuildConfigurationService(
                default_use_voice_replies=False,
                default_use_random_replies=False,
                db_manager=self.db_manager,
            )
            self.kingshot_rag_service = KingshotRAGService(
                self.db_manager,
                self.nvidia_client,
                embedding_model=config.nvidia_embedding_model,
                chat_model=config.nvidia_model,
            )
            self.voice_message_service = (
                VoiceMessageService.from_nvidia(
                    api_key=config.nvidia_api_key,
                    server=config.nvidia_tts_server,
                    use_ssl=config.nvidia_tts_use_ssl,
                    function_id=config.nvidia_tts_function_id,
                    default_voice=config.nvidia_tts_default_voice,
                    default_language_code=config.nvidia_tts_default_language_code,
                    audio_encoding=config.nvidia_tts_audio_encoding,
                    sample_rate_hz=config.nvidia_tts_sample_rate_hz,
                    max_text_chars=config.nvidia_tts_max_text_chars,
                )
                if config.enable_voice_replies
                else None
            )
        logger.info("All services initialized")

        # Initialize handlers
        logger.info("Initializing handlers...")
        self.translation_handler = None
        self.event_handler = None
        self.gift_code_handler = None
        self.guild_config_handler = None
        self.kingshot_rag_handler = None
        self.database_handler = None
        self.status_handler = None

        self.player_info_handler = PlayerInfoHandler(
            self.player_info_service,
            self.bot,
            interaction_tracking_service=self.interaction_tracking_service,
            kingshot_data_service=self.kingshot_data_service,
        )
        self.kvk_handler = KVKHandler(self.kvk_service, self.bot)

        if self.is_local_profile:
            self.translation_handler = TranslationHandler(
                self.translation_service,
                self.chatbot_service,
                self.bot,
                config,
                voice_message_service=self.voice_message_service,
                interaction_tracking_service=self.interaction_tracking_service,
                guild_configuration_service=self.guild_configuration_service,
            )
            self.event_handler = EventHandler(
                self.event_scheduler_service,
                self.bot,
                admin_user_ids=config.admin_user_ids,
            )
            self.gift_code_handler = GiftCodeHandler(
                self.gift_code_service,
                self.player_info_service,
                self.bot,
                config,
                interaction_tracking_service=self.interaction_tracking_service,
                player_registry_service=self.player_registry_service,
                kingshot_data_service=self.kingshot_data_service,
            )
            self.guild_config_handler = GuildConfigHandler(
                self.bot,
                self.guild_configuration_service,
                admin_user_ids=config.admin_user_ids,
            )
            self.kingshot_rag_handler = KingshotRAGHandler(self.kingshot_rag_service, self.bot)
            self.database_handler = DatabaseHandler(self.bot)
            self.status_handler = StatusHandler(
                self.bot,
                config,
                event_handler=self.event_handler,
                gift_code_handler=self.gift_code_handler,
                database_health_service=self.database_health_service,
                started_at=self.started_at,
            )
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
                    command_names = ", ".join(f"/{cmd.name}" for cmd in synced)
                    logger.info("Synced %s slash command(s): %s", len(synced), command_names)
                    self._has_synced_commands = True
                except Exception as e:
                    logger.error(f"Failed to sync slash commands: {e}")

            if self.event_handler is not None:
                self.event_handler.start_scheduler_task()
                logger.info("Event scheduler task started")

            if self.gift_code_handler is not None:
                self.gift_code_handler.start_polling_task()
                logger.info("Auto gift code polling task started")

        @self.bot.event
        async def on_command_error(ctx, error):
            """Handle command errors."""
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
            """Log when bot joins a guild."""
            logger.info(f"Bot joined new guild: {guild.name} (ID: {guild.id}) - {guild.member_count} members")

        @self.bot.event
        async def on_guild_remove(guild):
            """Log when bot leaves a guild."""
            logger.info(f"Bot removed from guild: {guild.name} (ID: {guild.id})")

    def _setup_handlers(self):
        """Register all command and event handlers."""
        logger.info("Registering command handlers for %s profile...", self.config.bot_profile)
        self.player_info_handler.register_commands()
        self.kvk_handler.register_commands()

        if self.is_local_profile:
            self.translation_handler.register_commands()
            self.translation_handler.register_events()
            self.event_handler.register_commands()
            self.gift_code_handler.register_commands()
            self.guild_config_handler.register_commands()
            self.kingshot_rag_handler.register_commands()
            self.database_handler.register_commands()
            self.database_handler.register_events()
            self.status_handler.register_commands()
        logger.info("All command handlers registered")

    def run(self):
        """Start the bot."""
        logger.info("Starting Discord bot...")
        try:
            self.bot.run(self.config.discord_token)
        except KeyboardInterrupt:
            logger.info("Bot shutdown requested by user")
        except Exception as e:
            logger.critical(f"Fatal error running bot: {e}", exc_info=True)
            raise
        finally:
            # Cleanup database connections
            import asyncio

            try:
                asyncio.run(self.db_manager.close())
                logger.info("Database connections closed")
            except Exception as e:
                logger.error(f"Error closing database: {e}")
            if self.nvidia_client is not None:
                try:
                    asyncio.run(self.nvidia_client.close())
                    logger.info("NVIDIA client closed")
                except Exception as e:
                    logger.error(f"Error closing NVIDIA client: {e}")


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
    except Exception as e:
        logger.critical(f"Failed to start bot: {e}", exc_info=True)


if __name__ == "__main__":
    main()
