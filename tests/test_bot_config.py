import pytest

from config.bot_config import BotConfig


def test_bot_requires_nvidia_api_key():
    with pytest.raises(ValueError, match="NVIDIA_API_KEY"):
        BotConfig(
            discord_token="discord-token",
            database_url="cockroachdb+asyncpg://user:pass@localhost:26257/db",
            nvidia_api_key="",
        )
