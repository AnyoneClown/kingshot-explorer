import asyncio
from types import SimpleNamespace

from handlers.translation_handler import TranslationHandler
from services.chatbot_service import ChatReply
from services.translation_service import TranslationUnavailableError, UnknownLanguageError

TRANSLATOR = SimpleNamespace(name="Translator", id=1)
BOT_USER = SimpleNamespace(id=42)


class FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSent:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeChannel:
    def __init__(self, referenced=None):
        self.id = 789
        self.sent = []
        self.fetches = []
        self._referenced = referenced

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return FakeSent(kwargs)

    async def fetch_message(self, message_id):
        self.fetches.append(message_id)
        return self._referenced

    def typing(self):
        return FakeTyping()

    async def _no_history(self):
        for item in ():
            yield item

    def history(self, **kwargs):
        return self._no_history()


class FakeMessage:
    def __init__(self, content, *, author_id=10, roles=(TRANSLATOR,), mentions=(), reference=None, channel=None):
        self.id = 1
        self.content = content
        self.clean_content = content
        self.author = SimpleNamespace(
            id=author_id, name="user", discriminator="0", display_name="User", roles=list(roles), bot=False
        )
        self.guild = SimpleNamespace(id=456, roles=[TRANSLATOR])
        self.channel = channel or FakeChannel()
        self.mentions = list(mentions)
        self.reference = reference
        self.replies = []

    async def reply(self, **kwargs):
        self.replies.append(kwargs)
        return FakeSent(kwargs)


class FakeTranslation:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def translate_to_english(self, text):
        self.calls.append(text)
        if self.error:
            raise self.error
        return self.result

    async def translate_to_language(self, text, target_language):
        self.calls.append((text, target_language))
        if self.error:
            raise self.error
        return self.result


class FakeChatbot:
    def __init__(self, reply="Sure thing."):
        self.reply = reply
        self.calls = []

    async def generate_contextual_reply(self, message, history, **kwargs):
        self.calls.append((message, kwargs))
        return self.reply


class FakeTracking:
    def __init__(self):
        self.calls = []

    async def track_translation(self, **kwargs):
        self.calls.append(kwargs)


class FakeAccess:
    def __init__(self, banned=()):
        self.banned = set(banned)

    def is_banned(self, user_id):
        return user_id in self.banned


class FakeGuildConfig:
    def __init__(self, use_random_replies=True):
        self.settings = SimpleNamespace(use_random_replies=use_random_replies)

    async def get_or_create_for_guild(self, guild_id):
        return self.settings


def make_config(**overrides):
    values = dict(
        translator_role_name="Translator",
        banned_players=set(),
        max_chat_response_chars=500,
        chat_history_limit=5,
        random_reply_chance=0,
        random_reply_cooldown_seconds=180,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def make_handler(translation=None, chatbot=None, *, access=None, guild_config=None, **config):
    tracking = FakeTracking()
    handler = TranslationHandler(
        translation_service=translation or FakeTranslation(),
        chatbot_service=chatbot or FakeChatbot(),
        bot=SimpleNamespace(user=BOT_USER),
        config=make_config(**config),
        interaction_tracking_service=tracking,
        guild_configuration_service=guild_config,
        access_policy=access,
    )
    return handler, tracking


# ---------------------------------------------------------------- auto translation


def test_auto_translation_skips_clearly_english_and_wordless_messages():
    translation = FakeTranslation({"language": "Spanish", "text": "x"})
    handler, tracking = make_handler(translation)

    for content in ["we need to rally the bear at 5 pm", "😂😂", "https://example.com/x", "<@123456789012345678>"]:
        message = FakeMessage(content)
        asyncio.run(handler._handle_auto_translation(message))
        assert message.replies == []

    assert translation.calls == []
    assert tracking.calls == []


def test_auto_translation_replies_without_pinging_and_tracks():
    translation = FakeTranslation({"language": "Spanish", "text": "Attack at five <@123456789012345678>"})
    handler, tracking = make_handler(translation)
    message = FakeMessage("Ataquen a las cinco <@123456789012345678>")

    asyncio.run(handler._handle_auto_translation(message))

    reply = message.replies[0]
    assert reply["content"] == "> Attack at five <@123456789012345678>"
    assert reply["mention_author"] is False
    assert reply["allowed_mentions"].users is False and reply["allowed_mentions"].replied_user is False
    assert tracking.calls[0]["source_language"] == "Spanish"
    assert tracking.calls[0]["translation_type"] == "auto"


def test_auto_translation_language_check_is_case_insensitive_and_skips_tracking():
    handler, tracking = make_handler(FakeTranslation({"language": "english", "text": "hola amigos"}))
    message = FakeMessage("hola amigos")

    asyncio.run(handler._handle_auto_translation(message))

    assert message.replies == []
    assert tracking.calls == []


def test_auto_translation_outage_is_silent():
    handler, tracking = make_handler(FakeTranslation(error=TranslationUnavailableError("down")))
    message = FakeMessage("hola amigos")

    asyncio.run(handler._handle_auto_translation(message))

    assert message.replies == [] and tracking.calls == []


def test_auto_translation_ignores_banned_users_and_non_translators():
    translation = FakeTranslation({"language": "Spanish", "text": "Hi friends"})
    handler, _ = make_handler(translation, access=FakeAccess(banned={10}))

    asyncio.run(handler._handle_auto_translation(FakeMessage("hola amigos")))
    asyncio.run(handler._handle_auto_translation(FakeMessage("hola amigos", author_id=11, roles=())))

    assert translation.calls == []


def test_long_auto_translation_is_split_cleanly():
    long_text = "\n".join(f"Line {index}: the rally leader goes first." for index in range(120))
    translation = FakeTranslation({"language": "Spanish", "text": long_text})
    handler, _ = make_handler(translation)
    message = FakeMessage("plan de rally largo")

    asyncio.run(handler._handle_auto_translation(message))

    contents = [message.replies[0]["content"]] + [sent["content"] for sent in message.channel.sent]
    assert len(contents) >= 2
    assert all(len(content) <= 2000 for content in contents)
    assert all(line.startswith(">") for content in contents for line in content.splitlines())
    assert all(sent["allowed_mentions"].users is False for sent in message.channel.sent)
    assert "Line 0:" in contents[0] and "Line 60:" in "".join(contents)


# ---------------------------------------------------------------- commands


class FakeContext:
    def __init__(self, text):
        self.author = SimpleNamespace(id=10, name="u", discriminator="0", display_name="U")
        self.guild = SimpleNamespace(id=456)
        self.channel = FakeChannel(referenced=SimpleNamespace(content=text))
        self.message = SimpleNamespace(reference=SimpleNamespace(message_id=5))
        self.replies = []

    def typing(self):
        return FakeTyping()

    async def reply(self, **kwargs):
        self.replies.append(kwargs)
        return FakeSent(kwargs)


def test_translate_command_distinguishes_unknown_language_from_outage():
    unknown, _ = make_handler(FakeTranslation(error=UnknownLanguageError("Elvish")))
    down, _ = make_handler(FakeTranslation(error=TranslationUnavailableError("timeout")))
    ctx_unknown, ctx_down = FakeContext("Hello"), FakeContext("Hello")

    asyncio.run(unknown._handle_translate_to_language(ctx_unknown, "Elvish"))
    asyncio.run(down._handle_translate_to_language(ctx_down, "French"))

    assert ctx_unknown.replies[0]["embed"].title == "Unknown Language"
    assert ctx_down.replies[0]["embed"].title == "Translation Unavailable"


def test_en_command_handles_lowercase_and_missing_language():
    already, _ = make_handler(FakeTranslation({"language": "EN", "text": "Hello"}))
    unknown, _ = make_handler(FakeTranslation({"language": None, "text": "Hello friends"}))
    ctx_already, ctx_unknown = FakeContext("Hello"), FakeContext("hola amigos")

    asyncio.run(already._handle_translate_to_english(ctx_already))
    asyncio.run(unknown._handle_translate_to_english(ctx_unknown))

    assert ctx_already.replies[0]["embed"].title == "Already English"
    assert ctx_unknown.replies[0]["embed"].title == "Translated from unknown"


def test_command_skips_llm_for_wordless_messages():
    translation = FakeTranslation({"language": "Spanish", "text": "x"})
    handler, _ = make_handler(translation)
    ctx = FakeContext("😂 <:kek:123456789012345678>")

    asyncio.run(handler._handle_translate_to_english(ctx))

    assert translation.calls == []
    assert ctx.replies[0]["embed"].title == "Nothing to Translate"


# ---------------------------------------------------------------- chat controls


def test_direct_mentions_have_a_per_user_cooldown():
    chatbot = FakeChatbot()
    handler, _ = make_handler(chatbot=chatbot)

    first = FakeMessage("hey bot", mentions=[BOT_USER])
    second = FakeMessage("hey bot again", mentions=[BOT_USER])
    other_user = FakeMessage("me too", author_id=11, mentions=[BOT_USER])
    for message in (first, second, other_user):
        asyncio.run(handler._handle_contextual_chat(message))

    assert [call[0] for call in chatbot.calls] == ["hey bot", "me too"]
    assert second.replies == []


def test_direct_reply_passes_server_scoped_tool_context_and_never_pings():
    chatbot = FakeChatbot(ChatReply("Power is up 5%.", language="English"))
    handler, _ = make_handler(chatbot=chatbot)
    message = FakeMessage("how is our alliance doing?", mentions=[BOT_USER])

    asyncio.run(handler._handle_contextual_chat(message))

    kwargs = chatbot.calls[0][1]
    assert kwargs["force_reply"] is True
    assert (kwargs["tool_context"].guild_id, kwargs["tool_context"].channel_id) == (456, 789)
    reply = message.replies[0]
    assert reply["content"] == "Power is up 5%."
    assert reply["allowed_mentions"].everyone is False and reply["allowed_mentions"].users is False


def test_unresolved_reference_is_not_fetched_when_ai_replies_are_disabled():
    channel = FakeChannel(referenced=SimpleNamespace(author=BOT_USER))
    chatbot = FakeChatbot()
    handler, _ = make_handler(chatbot=chatbot, guild_config=FakeGuildConfig(use_random_replies=False))
    message = FakeMessage(
        "replying to you", reference=SimpleNamespace(message_id=77, resolved=None), channel=channel
    )

    asyncio.run(handler._handle_contextual_chat(message))

    assert channel.fetches == []
    assert chatbot.calls == []


def test_random_replies_require_some_substance_before_any_llm_call():
    chatbot = FakeChatbot()
    handler, _ = make_handler(chatbot=chatbot, random_reply_chance=1, random_reply_cooldown_seconds=0)

    for content in ["lol ok", "😂😂😂😂", "https://example.com/abc"]:
        asyncio.run(handler._handle_contextual_chat(FakeMessage(content, author_id=hash(content) % 1000)))

    assert chatbot.calls == []


def test_banned_users_get_no_chat_reply():
    chatbot = FakeChatbot()
    handler, _ = make_handler(chatbot=chatbot, access=FakeAccess(banned={10}))

    asyncio.run(handler._handle_contextual_chat(FakeMessage("hey bot", mentions=[BOT_USER])))

    assert chatbot.calls == []
