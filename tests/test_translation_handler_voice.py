import asyncio
from types import SimpleNamespace

from handlers.translation_handler import TranslationHandler


class FakeTranslationService:
    async def translate_to_language(self, text, target_language):
        return {"text": "Hola a todos"}

    async def translate_to_english(self, text):
        return {"language": "Spanish", "text": "Hello everyone"}


class FakeChatbotService:
    async def generate_contextual_reply(self, *args, **kwargs):
        return "Voice-enabled reply"


class FakeVoiceService:
    def __init__(self):
        self.calls = []

    async def generate_audio(self, text, **kwargs):
        self.calls.append((text, kwargs))
        return SimpleNamespace(
            filename="reply.ogg",
            data=b"ogg-bytes",
            content_type="audio/ogg",
        )


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeChannel:
    def __init__(self, referenced_message):
        self._referenced_message = referenced_message

    async def fetch_message(self, message_id):
        return self._referenced_message


class FakeContext:
    def __init__(self, referenced_message):
        self.author = SimpleNamespace(
            id=123,
            name="tester",
            discriminator="0001",
            display_name="Tester",
        )
        self.guild = SimpleNamespace(id=456)
        self.channel = FakeChannel(referenced_message)
        self.message = SimpleNamespace(reference=SimpleNamespace(message_id=789))
        self.replies = []

    def typing(self):
        return FakeTyping()

    async def reply(self, *args, **kwargs):
        self.replies.append({"args": args, "kwargs": kwargs})


class FakeGuildConfigService:
    def __init__(self, use_voice_replies=True, use_random_replies=True):
        self.guild_config = SimpleNamespace(
            use_voice_replies=use_voice_replies,
            use_random_replies=use_random_replies,
        )

    async def get_or_create_for_guild(self, guild_id):
        assert guild_id == 456
        return self.guild_config


def test_translate_to_language_attaches_voice_file():
    voice_service = FakeVoiceService()
    handler = TranslationHandler(
        translation_service=FakeTranslationService(),
        chatbot_service=FakeChatbotService(),
        voice_message_service=voice_service,
        bot=SimpleNamespace(user=None),
        config=SimpleNamespace(banned_players=set()),
    )
    ctx = FakeContext(FakeMessage("Hello everyone"))

    asyncio.run(handler._handle_translate_to_language(ctx, "Spanish"))

    assert voice_service.calls == [
        (
            "Hola a todos",
            {
                "language_hint": "Spanish",
                "filename_stem": "translation",
            },
        )
    ]
    reply_kwargs = ctx.replies[0]["kwargs"]
    assert reply_kwargs["embed"].title == "Translated to Spanish"
    assert reply_kwargs["file"].filename == "reply.ogg"


def test_translate_to_language_skips_voice_when_guild_config_disables_it():
    voice_service = FakeVoiceService()
    handler = TranslationHandler(
        translation_service=FakeTranslationService(),
        chatbot_service=FakeChatbotService(),
        voice_message_service=voice_service,
        bot=SimpleNamespace(user=None),
        config=SimpleNamespace(banned_players=set(), enable_voice_replies=True),
        guild_configuration_service=FakeGuildConfigService(use_voice_replies=False),
    )
    ctx = FakeContext(FakeMessage("Hello everyone"))

    asyncio.run(handler._handle_translate_to_language(ctx, "Spanish"))

    assert voice_service.calls == []
    reply_kwargs = ctx.replies[0]["kwargs"]
    assert reply_kwargs["embed"].title == "Translated to Spanish"
    assert "file" not in reply_kwargs


def test_direct_reply_detection_fetches_unresolved_reference():
    bot_user = SimpleNamespace(id=42)
    referenced_message = SimpleNamespace(author=SimpleNamespace(id=42))

    class FakeChannel:
        async def fetch_message(self, message_id):
            assert message_id == 99
            return referenced_message

    handler = TranslationHandler(
        translation_service=FakeTranslationService(),
        chatbot_service=FakeChatbotService(),
        bot=SimpleNamespace(user=bot_user),
    )
    message = SimpleNamespace(
        mentions=[],
        reference=SimpleNamespace(message_id=99, resolved=None),
        channel=FakeChannel(),
    )

    assert asyncio.run(handler._is_direct_mention_or_reply(message)) is True


def test_random_replies_can_be_disabled_per_guild_without_blocking_direct_triggers():
    handler = TranslationHandler(
        translation_service=FakeTranslationService(),
        chatbot_service=FakeChatbotService(),
        bot=SimpleNamespace(user=SimpleNamespace(id=42)),
        config=SimpleNamespace(
            banned_players=set(),
            random_reply_chance=1,
            random_reply_cooldown_seconds=0,
        ),
        guild_configuration_service=FakeGuildConfigService(use_random_replies=False),
    )
    message = SimpleNamespace(
        content="Should the bot randomly answer this?",
        guild=SimpleNamespace(id=456),
        channel=SimpleNamespace(id=789),
        mentions=[],
        reference=None,
    )

    assert asyncio.run(handler._should_attempt_reply(message, direct_trigger=False)) is False
    assert asyncio.run(handler._should_attempt_reply(message, direct_trigger=True)) is True
