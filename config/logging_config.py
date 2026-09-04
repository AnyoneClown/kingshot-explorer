"""Logging configuration for the Discord bot."""

import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path


class BotContextFilter(logging.Filter):
    """Attach bot instance metadata to every log record."""

    def __init__(self) -> None:
        super().__init__()
        self.bot_instance = os.getenv("BOT_INSTANCE", "bot")

    def filter(self, record: logging.LogRecord) -> bool:
        record.bot_instance = self.bot_instance
        return True


def setup_logging(log_level: str = "INFO") -> None:
    """
    Configure logging for the application.

    Creates both console and file handlers with proper formatting.
    Logs are saved to logs/ directory with rotation.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    """
    # Create logs directory if it doesn't exist
    log_dir = Path("logs")
    try:
        log_dir.mkdir(exist_ok=True)
    except PermissionError as e:
        print(f"Warning: Cannot create logs directory: {e}")
        print("Logging to console only")
        log_dir = None

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Remove existing handlers to avoid duplicates
    root_logger.handlers.clear()

    context_filter = BotContextFilter()

    # Create formatters
    detailed_formatter = logging.Formatter(
        fmt=(
            "%(asctime)s | %(levelname)-8s | %(bot_instance)s | %(name)s:%(funcName)s:%(lineno)d | "
            "%(message)s"
        ),
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname).1s | %(bot_instance)s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # File handler with rotation (10MB max, keep 5 backup files) - only if we can create the directory
    if log_dir:
        try:
            log_file = log_dir / f"{context_filter.bot_instance}_{datetime.now().strftime('%Y%m%d')}.log"
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=10 * 1024 * 1024,  # 10 MB
                backupCount=5,
                encoding="utf-8",
            )
            file_handler.setLevel(logging.DEBUG)  # Log everything to file
            file_handler.setFormatter(detailed_formatter)
            file_handler.addFilter(context_filter)
            root_logger.addHandler(file_handler)
        except Exception as e:
            print(f"Warning: Cannot create log file: {e}")
            print("Logging to console only")

    # Console handler (less verbose)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    console_handler.setFormatter(console_formatter)
    console_handler.addFilter(context_filter)

    # Add handlers
    root_logger.addHandler(console_handler)

    # Reduce noise from discord.py and aiohttp
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.INFO)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    # Log startup message
    root_logger.info("=" * 80)
    root_logger.info("Logging system initialized")
    root_logger.info(f"Bot instance: {context_filter.bot_instance}")
    root_logger.info(f"Log level: {log_level.upper()}")
    if log_dir:
        root_logger.info(f"Log file: {log_file}")
    else:
        root_logger.info("Console logging only (no file logging)")
    root_logger.info("=" * 80)


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger instance for a module.

    Args:
        name: Name of the module (usually __name__)

    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)
