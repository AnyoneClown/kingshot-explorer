"""Configuration module for bot settings."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass
class BotConfig:
    """Bot configuration settings."""

    discord_token: str
    database_url: str
    nvidia_api_key: str
    command_prefix: str = "!"
    translator_role_name: str = "Translator"
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "openai/gpt-oss-120b"
    random_reply_chance: float = 0.08
    random_reply_cooldown_seconds: int = 180
    chat_history_limit: int = 25
    max_chat_response_chars: int = 500
    banned_players: set = None
    auto_redeem_channels: set = None

    def __post_init__(self):
        """Initialize mutable default values."""
        if self.banned_players is None:
            self.banned_players = set()
        if self.auto_redeem_channels is None:
            self.auto_redeem_channels = set()
        if not 0 <= self.random_reply_chance <= 1:
            raise ValueError("RANDOM_REPLY_CHANCE must be between 0 and 1")
        if self.random_reply_cooldown_seconds < 0:
            raise ValueError("RANDOM_REPLY_COOLDOWN_SECONDS must be >= 0")
        if self.chat_history_limit < 1:
            raise ValueError("CHAT_HISTORY_LIMIT must be >= 1")
        if self.max_chat_response_chars < 50:
            raise ValueError("MAX_CHAT_RESPONSE_CHARS must be >= 50")

    @classmethod
    def from_env(cls) -> "BotConfig":
        """Load configuration from environment variables."""
        load_dotenv()

        discord_token = os.getenv("DISCORD_TOKEN")
        if not discord_token:
            raise ValueError("DISCORD_TOKEN not found in environment variables")

        database_url = os.getenv("COCKROACHDB_URL")
        if not database_url:
            raise ValueError("COCKROACHDB_URL not found in environment variables")

        nvidia_api_key = os.getenv("NVIDIA_API_KEY")
        if not nvidia_api_key:
            raise ValueError("NVIDIA_API_KEY not found in environment variables")

        # Parse banned players from environment variable (comma-separated user IDs)
        banned_players_str = os.getenv("BANNED_PLAYERS", "")
        banned_players = set()
        if banned_players_str.strip():
            try:
                banned_players = set(
                    int(user_id.strip()) for user_id in banned_players_str.split(",") if user_id.strip()
                )
            except ValueError:
                raise ValueError("BANNED_PLAYERS must contain comma-separated user IDs (integers)")

        # Parse auto redeem announcement channels from environment variable (comma-separated channel IDs)
        auto_redeem_channels_str = os.getenv("AUTO_REDEEM_CHANNELS", "")
        auto_redeem_channels = set()
        if auto_redeem_channels_str.strip():
            try:
                auto_redeem_channels = set(
                    int(channel_id.strip()) for channel_id in auto_redeem_channels_str.split(",") if channel_id.strip()
                )
            except ValueError:
                raise ValueError("AUTO_REDEEM_CHANNELS must contain comma-separated channel IDs (integers)")

        return cls(
            discord_token=discord_token,
            database_url=database_url,
            nvidia_api_key=nvidia_api_key,
            command_prefix=os.getenv("COMMAND_PREFIX", "!"),
            translator_role_name=os.getenv("TRANSLATOR_ROLE", "Translator"),
            nvidia_base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
            nvidia_model=os.getenv("NVIDIA_MODEL", "openai/gpt-oss-120b"),
            random_reply_chance=float(os.getenv("RANDOM_REPLY_CHANCE", "0.08")),
            random_reply_cooldown_seconds=int(os.getenv("RANDOM_REPLY_COOLDOWN_SECONDS", "180")),
            chat_history_limit=int(os.getenv("CHAT_HISTORY_LIMIT", "25")),
            max_chat_response_chars=int(os.getenv("MAX_CHAT_RESPONSE_CHARS", "500")),
            banned_players=banned_players,
            auto_redeem_channels=auto_redeem_channels,
        )
