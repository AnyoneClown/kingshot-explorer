import pytest

from config.bot_config import BotConfig


def test_global_profile_does_not_require_nvidia_api_key():
    config = BotConfig(
        discord_token="discord-token",
        database_url="cockroachdb+asyncpg://user:pass@localhost:26257/db",
        nvidia_api_key=None,
        bot_profile="global",
    )

    assert config.bot_profile == "global"


def test_local_profile_requires_nvidia_api_key():
    with pytest.raises(ValueError, match="NVIDIA_API_KEY"):
        BotConfig(
            discord_token="discord-token",
            database_url="cockroachdb+asyncpg://user:pass@localhost:26257/db",
            nvidia_api_key=None,
            bot_profile="local",
        )


def test_bot_profile_must_be_known():
    with pytest.raises(ValueError, match="BOT_PROFILE"):
        BotConfig(
            discord_token="discord-token",
            database_url="cockroachdb+asyncpg://user:pass@localhost:26257/db",
            nvidia_api_key="nvidia-key",
            bot_profile="public",
        )
