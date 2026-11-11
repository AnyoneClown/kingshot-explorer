"""Guild-level bot configuration service."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, fields

from sqlalchemy import select

from db.models import GuildConfiguration
from db.session import DatabaseManager, get_db
from repositories.discord_repositories import GuildConfigurationRepository

logger = logging.getLogger(__name__)

# Settings that can be changed through ``update_for_guild``.
EDITABLE_GUILD_SETTINGS = frozenset(
    {
        "use_voice_replies",
        "use_random_replies",
        "manager_role_id",
        "gift_code_channel_id",
        "alliance_report_channel_id",
    }
)


@dataclass(frozen=True)
class GuildSettings:
    """Immutable, cache-safe snapshot of a guild's configuration row."""

    guild_id: int
    use_voice_replies: bool = False
    use_random_replies: bool = False
    manager_role_id: int | None = None
    gift_code_channel_id: int | None = None
    alliance_report_channel_id: int | None = None

    @classmethod
    def from_model(cls, model) -> "GuildSettings":
        values = {}
        for field in fields(cls):
            values[field.name] = getattr(model, field.name, field.default)
        return cls(**values)


class GuildConfigurationService:
    """Read and update persisted bot configuration for a guild.

    Settings are read on every guild message (translation and AI reply gates), so
    reads are served from a short in-memory cache that is refreshed on every write.
    """

    def __init__(
        self,
        default_use_voice_replies: bool = False,
        default_use_random_replies: bool = False,
        db_manager: DatabaseManager | None = None,
        cache_ttl_seconds: float = 300.0,
    ):
        self._default_use_voice_replies = default_use_voice_replies
        self._default_use_random_replies = default_use_random_replies
        self._db_manager = db_manager
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache: dict[int, tuple[float, GuildSettings]] = {}
        # Bumped by every update: a read that started before an update finished must
        # not put its (old) row back into the cache afterwards.
        self._generations: dict[int, int] = {}

    async def _get_db_manager(self):
        return self._db_manager or get_db()

    @staticmethod
    async def get_or_create_guild_configuration(
        session,
        guild_id: int,
        default_use_voice_replies: bool,
        default_use_random_replies: bool = False,
    ):
        repo = GuildConfigurationRepository(session)
        return await repo.get_or_create(guild_id, default_use_voice_replies, default_use_random_replies)

    @staticmethod
    async def set_use_voice_replies(
        session,
        guild_id: int,
        use_voice_replies: bool,
        default_use_voice_replies: bool,
        default_use_random_replies: bool = False,
    ):
        repo = GuildConfigurationRepository(session)
        return await repo.set_voice_replies(
            guild_id,
            use_voice_replies,
            default_use_voice_replies,
            default_use_random_replies,
        )

    @staticmethod
    async def set_use_random_replies(
        session,
        guild_id: int,
        use_random_replies: bool,
        default_use_voice_replies: bool,
        default_use_random_replies: bool = False,
    ):
        repo = GuildConfigurationRepository(session)
        return await repo.set_random_replies(
            guild_id,
            use_random_replies,
            default_use_voice_replies,
            default_use_random_replies,
        )

    def _cached(self, guild_id: int) -> GuildSettings | None:
        entry = self._cache.get(guild_id)
        if entry is None:
            return None
        stored_at, settings = entry
        if time.monotonic() - stored_at > self._cache_ttl_seconds:
            self._cache.pop(guild_id, None)
            return None
        return settings

    def _remember(self, settings: GuildSettings) -> GuildSettings:
        self._cache[settings.guild_id] = (time.monotonic(), settings)
        return settings

    def invalidate(self, guild_id: int | None = None) -> None:
        """Drop cached settings for one guild, or for all guilds."""
        if guild_id is None:
            self._cache.clear()
        else:
            self._cache.pop(guild_id, None)

    async def _run(self, operation, *, retry_on_integrity_error: bool = False):
        db = await self._get_db_manager()
        runner = getattr(db, "run_in_transaction", None)
        if runner is not None:
            return await runner(operation, retry_on_integrity_error=retry_on_integrity_error)
        async with db.session() as session:
            return await operation(session)

    async def get_or_create_for_guild(self, guild_id: int) -> GuildSettings:
        cached = self._cached(guild_id)
        if cached is not None:
            return cached

        async def operation(session):
            model = await self.get_or_create_guild_configuration(
                session,
                guild_id,
                self._default_use_voice_replies,
                self._default_use_random_replies,
            )
            return GuildSettings.from_model(model)

        generation = self._generations.get(guild_id, 0)
        # A concurrent first message in the same guild can race the insert.
        settings = await self._run(operation, retry_on_integrity_error=True)
        if self._generations.get(guild_id, 0) == generation:
            self._remember(settings)
        return settings

    async def get_settings(self, guild_id: int) -> GuildSettings:
        """Alias of ``get_or_create_for_guild`` for readability at call sites."""
        return await self.get_or_create_for_guild(guild_id)

    async def update_for_guild(self, guild_id: int, **changes) -> GuildSettings:
        """Update one or more editable settings and refresh the cache."""
        unknown = set(changes) - EDITABLE_GUILD_SETTINGS
        if unknown:
            raise ValueError(f"Unknown guild settings: {', '.join(sorted(unknown))}")

        async def operation(session):
            model = await self.get_or_create_guild_configuration(
                session,
                guild_id,
                self._default_use_voice_replies,
                self._default_use_random_replies,
            )
            for name, value in changes.items():
                setattr(model, name, value)
            await session.flush()
            return GuildSettings.from_model(model)

        generation = self._generations.get(guild_id, 0) + 1
        self._generations[guild_id] = generation
        self.invalidate(guild_id)
        try:
            settings = await self._run(operation, retry_on_integrity_error=True)
        finally:
            # Another update of this guild ran meanwhile: under READ COMMITTED our row
            # can miss the column it changed, so no result of the two is safe to cache.
            overlapped = self._generations.get(guild_id, 0) != generation
            # Again after the write: reads that began during it carry the old row.
            self._generations[guild_id] = self._generations.get(guild_id, 0) + 1
        if overlapped:
            self.invalidate(guild_id)
            return settings
        return self._remember(settings)

    async def set_use_voice_replies_for_guild(self, guild_id: int, use_voice_replies: bool) -> GuildSettings:
        return await self.update_for_guild(guild_id, use_voice_replies=use_voice_replies)

    async def set_use_random_replies_for_guild(self, guild_id: int, use_random_replies: bool) -> GuildSettings:
        return await self.update_for_guild(guild_id, use_random_replies=use_random_replies)

    async def list_all(self) -> list[GuildSettings]:
        """Return every stored guild configuration (uncached; for background jobs)."""

        async def operation(session):
            result = await session.execute(select(GuildConfiguration))
            return [GuildSettings.from_model(model) for model in result.scalars().all()]

        generations = dict(self._generations)
        settings = await self._run(operation)
        for item in settings:
            if self._generations.get(item.guild_id, 0) == generations.get(item.guild_id, 0):
                self._remember(item)
        return settings
