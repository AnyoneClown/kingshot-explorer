import asyncio
from types import SimpleNamespace

from services.guild_configuration_service import GuildConfigurationService


class FakeSession:
    def __init__(self, existing=None):
        self.existing = existing
        self.added = []
        self.flush_count = 0

    async def execute(self, statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self.existing)

    def add(self, value):
        self.added.append(value)
        self.existing = value

    async def flush(self):
        self.flush_count += 1


def test_get_or_create_guild_configuration_creates_row_from_defaults():
    session = FakeSession()

    guild_config = asyncio.run(
        GuildConfigurationService.get_or_create_guild_configuration(
            session,
            guild_id=123,
            default_use_voice_replies=True,
        )
    )

    assert guild_config.guild_id == 123
    assert guild_config.use_voice_replies is True
    assert session.added == [guild_config]
    assert session.flush_count == 1


def test_set_use_voice_replies_updates_existing_row():
    existing = SimpleNamespace(guild_id=123, use_voice_replies=True)
    session = FakeSession(existing=existing)

    guild_config = asyncio.run(
        GuildConfigurationService.set_use_voice_replies(
            session,
            guild_id=123,
            use_voice_replies=False,
            default_use_voice_replies=True,
        )
    )

    assert guild_config is existing
    assert guild_config.use_voice_replies is False
    assert session.flush_count == 1
