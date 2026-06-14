import asyncio
from types import SimpleNamespace

from handlers.guild_config_handler import GuildConfigHandler, GuildConfigView


class FakeGuildConfigService:
    def __init__(self, initial_use_voice_replies=False, initial_use_random_replies=False):
        self.guild_config = SimpleNamespace(
            guild_id=999,
            use_voice_replies=initial_use_voice_replies,
            use_random_replies=initial_use_random_replies,
        )
        self.voice_updates = []
        self.random_reply_updates = []

    async def get_or_create_guild_configuration(self, guild_id):
        assert guild_id == 999
        return self.guild_config

    async def get_or_create_for_guild(self, guild_id):
        return await self.get_or_create_guild_configuration(guild_id)

    async def set_use_voice_replies(self, guild_id, use_voice_replies):
        assert guild_id == 999
        self.voice_updates.append(use_voice_replies)
        self.guild_config.use_voice_replies = use_voice_replies
        return self.guild_config

    async def set_use_voice_replies_for_guild(self, guild_id, use_voice_replies):
        return await self.set_use_voice_replies(guild_id, use_voice_replies)

    async def set_use_random_replies(self, guild_id, use_random_replies):
        assert guild_id == 999
        self.random_reply_updates.append(use_random_replies)
        self.guild_config.use_random_replies = use_random_replies
        return self.guild_config

    async def set_use_random_replies_for_guild(self, guild_id, use_random_replies):
        return await self.set_use_random_replies(guild_id, use_random_replies)


class FakeResponse:
    def __init__(self):
        self.edited_embed = None
        self.edited_view = None
        self.sent_message = None
        self.sent_embed = None
        self.sent_view = None
        self.deferred = None

    async def edit_message(self, *, embed=None, view=None):
        self.edited_embed = embed
        self.edited_view = view

    async def send_message(self, content=None, *, embed=None, view=None, ephemeral=False):
        self.sent_message = (content, ephemeral)
        self.sent_embed = embed
        self.sent_view = view

    async def defer(self, *, ephemeral=False, thinking=False):
        self.deferred = (ephemeral, thinking)


class FakeInteraction:
    def __init__(self, user_id, guild=None):
        self.user = SimpleNamespace(id=user_id)
        self.guild = guild
        self.response = FakeResponse()
        self.edited_original_embed = None
        self.edited_original_view = None

    async def edit_original_response(self, *, embed=None, view=None):
        self.edited_original_embed = embed
        self.edited_original_view = view


async def _create_view(service=None):
    return GuildConfigView(
        guild_id=999,
        author_id=111,
        guild_name="Test Guild",
        guild_config_service=service or FakeGuildConfigService(),
    )


def test_guild_config_view_builds_embed_with_default_off_statuses():
    async def run_test():
        view = await _create_view()
        return await view.build_embed()

    embed = asyncio.run(run_test())

    assert embed.title == "Bot Configuration"
    assert "Voice replies" in embed.description
    assert "AI random replies" in embed.description
    assert "Voice replies: **Disabled**" in embed.description
    assert "AI random replies: **Disabled**" in embed.description


def test_toggle_voice_button_updates_config_and_message():
    service = FakeGuildConfigService(initial_use_voice_replies=False)
    interaction = FakeInteraction(user_id=111)

    async def run_test():
        view = await _create_view(service)
        await view.toggle_voice_button.callback(interaction)
        return interaction

    interaction = asyncio.run(run_test())

    assert service.voice_updates == [True]
    assert interaction.response.deferred == (False, False)
    assert interaction.edited_original_embed.title == "Bot Configuration"
    assert "Voice replies: **Enabled**" in interaction.edited_original_embed.description


def test_toggle_random_replies_button_updates_config_and_message():
    service = FakeGuildConfigService(initial_use_random_replies=False)
    interaction = FakeInteraction(user_id=111)

    async def run_test():
        view = await _create_view(service)
        await view.toggle_random_replies_button.callback(interaction)
        return interaction

    interaction = asyncio.run(run_test())

    assert service.random_reply_updates == [True]
    assert interaction.response.deferred == (False, False)
    assert interaction.edited_original_embed.title == "Bot Configuration"
    assert "AI random replies: **Enabled**" in interaction.edited_original_embed.description


def test_interaction_check_rejects_other_users():
    interaction = FakeInteraction(user_id=222)

    async def run_test():
        view = await _create_view(FakeGuildConfigService())
        return await view.interaction_check(interaction)

    allowed = asyncio.run(run_test())

    assert allowed is False
    assert interaction.response.sent_message == ("Only the command user can control this panel.", True)


def test_configure_rejects_non_env_admin_even_when_server_admin():
    handler = GuildConfigHandler(
        bot=SimpleNamespace(),
        guild_config_service=FakeGuildConfigService(),
        admin_user_ids={111},
    )
    interaction = FakeInteraction(
        user_id=222,
        guild=SimpleNamespace(id=999, name="Test Guild"),
    )
    interaction.user.guild_permissions = SimpleNamespace(administrator=True)

    asyncio.run(handler._handle_configure(interaction))

    assert interaction.response.sent_message == ("Only configured bot admins can configure the bot.", True)


def test_configure_allows_env_admin_without_server_admin_permission():
    handler = GuildConfigHandler(
        bot=SimpleNamespace(),
        guild_config_service=FakeGuildConfigService(),
        admin_user_ids={111},
    )
    interaction = FakeInteraction(
        user_id=111,
        guild=SimpleNamespace(id=999, name="Test Guild"),
    )
    interaction.user.guild_permissions = SimpleNamespace(administrator=False)

    asyncio.run(handler._handle_configure(interaction))

    assert interaction.response.deferred == (True, True)
    assert interaction.edited_original_embed.title == "Bot Configuration"
    assert interaction.edited_original_view is not None
