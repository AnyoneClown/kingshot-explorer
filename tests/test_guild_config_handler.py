import asyncio
from types import SimpleNamespace

from handlers.guild_config_handler import GuildConfigView


class FakeGuildConfigService:
    def __init__(self, initial_use_voice_replies=True):
        self.guild_config = SimpleNamespace(
            guild_id=999,
            use_voice_replies=initial_use_voice_replies,
        )
        self.updated_values = []

    async def get_or_create_guild_configuration(self, guild_id):
        assert guild_id == 999
        return self.guild_config

    async def get_or_create_for_guild(self, guild_id):
        return await self.get_or_create_guild_configuration(guild_id)

    async def set_use_voice_replies(self, guild_id, use_voice_replies):
        assert guild_id == 999
        self.updated_values.append(use_voice_replies)
        self.guild_config.use_voice_replies = use_voice_replies
        return self.guild_config

    async def set_use_voice_replies_for_guild(self, guild_id, use_voice_replies):
        return await self.set_use_voice_replies(guild_id, use_voice_replies)


class FakeResponse:
    def __init__(self):
        self.edited_embed = None
        self.edited_view = None
        self.sent_message = None

    async def edit_message(self, *, embed=None, view=None):
        self.edited_embed = embed
        self.edited_view = view

    async def send_message(self, content, ephemeral=False):
        self.sent_message = (content, ephemeral)


class FakeInteraction:
    def __init__(self, user_id):
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()


async def _create_view(service=None):
    return GuildConfigView(
        guild_id=999,
        author_id=111,
        guild_name="Test Guild",
        guild_config_service=service or FakeGuildConfigService(initial_use_voice_replies=True),
    )


def test_guild_config_view_builds_embed_with_voice_status():
    async def run_test():
        view = await _create_view()
        return await view.build_embed()

    embed = asyncio.run(run_test())

    assert embed.title == "Bot Configuration"
    assert "Voice replies" in embed.description
    assert "Enabled" in embed.description


def test_toggle_voice_button_updates_config_and_message():
    service = FakeGuildConfigService(initial_use_voice_replies=True)
    interaction = FakeInteraction(user_id=111)

    async def run_test():
        view = await _create_view(service)
        await view.toggle_voice_button.callback(interaction)
        return interaction

    interaction = asyncio.run(run_test())

    assert service.updated_values == [False]
    assert interaction.response.edited_embed.title == "Bot Configuration"
    assert "Disabled" in interaction.response.edited_embed.description


def test_interaction_check_rejects_other_users():
    interaction = FakeInteraction(user_id=222)

    async def run_test():
        view = await _create_view(FakeGuildConfigService())
        return await view.interaction_check(interaction)

    allowed = asyncio.run(run_test())

    assert allowed is False
    assert interaction.response.sent_message == ("Only the command user can control this panel.", True)
