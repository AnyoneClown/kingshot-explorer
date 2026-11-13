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
            default_use_random_replies=False,
        )
    )

    assert guild_config.guild_id == 123
    assert guild_config.use_random_replies is False
    assert session.added == [guild_config]
    assert session.flush_count == 1


def test_set_use_random_replies_updates_existing_row():
    existing = SimpleNamespace(guild_id=123, use_random_replies=True)
    session = FakeSession(existing=existing)

    guild_config = asyncio.run(
        GuildConfigurationService.set_use_random_replies(
            session,
            guild_id=123,
            use_random_replies=False,
            default_use_random_replies=True,
        )
    )

    assert guild_config is existing
    assert guild_config.use_random_replies is False
    assert session.flush_count == 1


def test_time_zone_is_an_editable_setting_and_defaults_to_unset():
    from services.guild_configuration_service import EDITABLE_GUILD_SETTINGS, GuildSettings

    assert "time_zone" in EDITABLE_GUILD_SETTINGS
    assert GuildSettings(guild_id=1).time_zone is None
    # Rows read before the column existed (or test doubles without it) stay unset.
    assert GuildSettings.from_model(SimpleNamespace(guild_id=1)).time_zone is None
    stored = SimpleNamespace(guild_id=1, time_zone="Europe/Kyiv")
    assert GuildSettings.from_model(stored).time_zone == "Europe/Kyiv"


def test_typed_timezones_resolve_to_tz_database_names():
    from services.guild_configuration_service import canonical_time_zone

    for typed, name in [
        ("Europe/Kyiv", "Europe/Kyiv"),
        (" europe/kyiv ", "Europe/Kyiv"),
        ("America/New York", "America/New_York"),
        ("new york", "America/New_York"),
        ("Tokyo", "Asia/Tokyo"),
        ("UTC", "UTC"),
        ("Etc/GMT+2", "Etc/GMT+2"),
    ]:
        assert canonical_time_zone(typed) == name
    # "GMT+2" must not become Etc/GMT+2, which is UTC-2.
    for typed in (None, "", "   ", "GMT+2", "Kyiv time", "Europe/Atlantis", "../etc/passwd"):
        assert canonical_time_zone(typed) is None
