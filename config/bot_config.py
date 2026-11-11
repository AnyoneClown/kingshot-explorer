"""Configuration module for bot settings."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv


def _env_number(name: str, default, convert):
    """Read a numeric setting; an empty value means "use the default"."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return convert(raw.strip())
    except ValueError:
        raise ValueError(f"{name} must be a number, got {raw!r}") from None


def _env_int(name: str, default: int) -> int:
    return _env_number(name, default, int)


def _env_float(name: str, default: float) -> float:
    return _env_number(name, default, float)


@dataclass
class BotConfig:
    """Bot configuration settings."""

    discord_token: str
    database_url: str
    nvidia_api_key: str
    command_prefix: str = "!"
    translator_role_name: str = "Translator"
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_model: str = "nvidia/nemotron-3.5-super-vl-120b-a12b"
    nvidia_chat_model: str = "nvidia/nemotron-3.5-super-vl-120b-a12b"
    nvidia_embedding_model: str = "nvidia/nemotron-3.5-embed-1b"
    random_reply_chance: float = 0.0
    random_reply_cooldown_seconds: int = 180
    chat_history_limit: int = 25
    max_chat_response_chars: int = 500
    ks_data_api_key: str | None = None
    ks_data_base_url: str = "https://ks.jeab.dev"
    ks_data_timeout_seconds: int = 30
    mightpulse_api_key: str | None = None
    mightpulse_base_url: str = "https://api.mightpulse.com"
    admin_user_ids: set = None
    # Discord user IDs that the bot ignores (slash commands, translation, chat).
    banned_players: set = None
    # Legacy global announcement channels for gift-code summaries. Servers can set
    # their own channel in /configure; these remain as a fallback for operators.
    auto_redeem_channels: set = None
    # Gift-code roster limits per Discord server.
    max_players_per_guild: int = 300
    max_self_registered_players: int = 3
    llm_timeout_seconds: float = 30.0
    # Stored translation text is deleted after this many days (0 keeps it forever).
    translation_log_retention_days: int = 30
    # Minimum seconds between AI replies to the same user (direct mentions/replies).
    chat_user_cooldown_seconds: float = 10.0
    # Knowledge chunks farther than this cosine distance are ignored (0..2).
    kingshot_rag_max_distance: float = 0.7

    def __post_init__(self):
        """Initialize mutable default values."""
        if not self.nvidia_api_key:
            raise ValueError("NVIDIA_API_KEY not found in environment variables")
        if self.admin_user_ids is None:
            self.admin_user_ids = set()
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
        # Discord rejects messages over 2000 characters, so a larger value would make
        # long replies fail to send.
        self.max_chat_response_chars = min(self.max_chat_response_chars, 2000)
        if self.max_players_per_guild < 1:
            raise ValueError("MAX_PLAYERS_PER_GUILD must be >= 1")
        if self.max_self_registered_players < 0:
            raise ValueError("MAX_SELF_REGISTERED_PLAYERS must be >= 0")
        if self.llm_timeout_seconds <= 0:
            raise ValueError("LLM_TIMEOUT_SECONDS must be > 0")
        if self.translation_log_retention_days < 0:
            raise ValueError("TRANSLATION_LOG_RETENTION_DAYS must be >= 0")
        if self.chat_user_cooldown_seconds < 0:
            raise ValueError("CHAT_USER_COOLDOWN_SECONDS must be >= 0")
        if not 0 < self.kingshot_rag_max_distance <= 2:
            raise ValueError("KINGSHOT_RAG_MAX_DISTANCE must be in (0, 2]")

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

        # Parse bot admin users from env. ADMIN_USER_ID is kept for single-admin setups.
        admin_users_str = ",".join(
            value for value in [os.getenv("ADMIN_USER_ID", ""), os.getenv("ADMIN_USER_IDS", "")] if value
        )
        admin_user_ids = set()
        if admin_users_str.strip():
            try:
                admin_user_ids = set(
                    int(user_id.strip()) for user_id in admin_users_str.split(",") if user_id.strip()
                )
            except ValueError:
                raise ValueError("ADMIN_USER_ID/ADMIN_USER_IDS must contain Discord user IDs (integers)")

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

        def parse_bool(value: str | None, default: bool) -> bool:
            if value is None:
                return default
            return value.strip().lower() in {"1", "true", "yes", "on"}

        return cls(
            discord_token=discord_token,
            database_url=database_url,
            nvidia_api_key=nvidia_api_key,
            # An empty prefix would make every message look like a command.
            command_prefix=(os.getenv("COMMAND_PREFIX") or "").strip() or "!",
            translator_role_name=os.getenv("TRANSLATOR_ROLE", "Translator"),
            nvidia_base_url=os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1"),
            nvidia_model=os.getenv("NVIDIA_MODEL", "nvidia/nemotron-3.5-super-vl-120b-a12b"),
            nvidia_chat_model=os.getenv(
                "NVIDIA_CHAT_MODEL",
                "nvidia/nemotron-3.5-super-vl-120b-a12b",
            ),
            nvidia_embedding_model=os.getenv(
                "NVIDIA_EMBEDDING_MODEL",
                "nvidia/nemotron-3.5-embed-1b",
            ),
            random_reply_chance=_env_float("RANDOM_REPLY_CHANCE", 0),
            random_reply_cooldown_seconds=_env_int("RANDOM_REPLY_COOLDOWN_SECONDS", 180),
            chat_history_limit=_env_int("CHAT_HISTORY_LIMIT", 25),
            max_chat_response_chars=_env_int("MAX_CHAT_RESPONSE_CHARS", 500),
            ks_data_api_key=os.getenv("KS_DATA_API_KEY"),
            ks_data_base_url=os.getenv("KS_DATA_API_BASE_URL", "https://ks.jeab.dev"),
            ks_data_timeout_seconds=_env_int("KS_DATA_TIMEOUT_SECONDS", 30),
            mightpulse_api_key=os.getenv("MIGHTPULSE_API_KEY"),
            mightpulse_base_url=os.getenv("MIGHTPULSE_API_BASE_URL", "https://api.mightpulse.com"),
            admin_user_ids=admin_user_ids,
            banned_players=banned_players,
            auto_redeem_channels=auto_redeem_channels,
            max_players_per_guild=_env_int("MAX_PLAYERS_PER_GUILD", 300),
            max_self_registered_players=_env_int("MAX_SELF_REGISTERED_PLAYERS", 3),
            llm_timeout_seconds=_env_float("LLM_TIMEOUT_SECONDS", 30),
            translation_log_retention_days=_env_int("TRANSLATION_LOG_RETENTION_DAYS", 30),
            chat_user_cooldown_seconds=_env_float("CHAT_USER_COOLDOWN_SECONDS", 10),
            kingshot_rag_max_distance=_env_float("KINGSHOT_RAG_MAX_DISTANCE", 0.7),
        )
