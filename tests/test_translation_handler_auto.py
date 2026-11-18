import asyncio
from types import SimpleNamespace

from handlers.translation_handler import TranslationHandler
from services.chatbot_service import ChatReply
from services.translation_service import TranslationUnavailableError, UnknownLanguageError

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
    def __init__(self, content, *, author_id=10, roles=(), mentions=(), reference=None, channel=None):
        self.id = 1
        self.content = content
        self.clean_content = content
        self.author = SimpleNamespace(
            id=author_id, name="user", discriminator="0", display_name="User", roles=list(roles), bot=False
        )
        self.guild = SimpleNamespace(id=456, roles=[])
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
        # The target languages of each ``translate_to_languages`` call.
        self.targets = []

    async def translate_to_english(self, text):
        self.calls.append(text)
        if self.error:
            raise self.error
        return self.result

    async def translate_to_languages(self, text, target_languages):
        self.calls.append(text)
        self.targets.append(list(target_languages))
        if self.error:
            raise self.error
        if self.result and "translations" not in self.result:
            # ``{"language", "text"}``: the same text for every language asked for.
            return {
                "language": self.result.get("language"),
                "translations": {target: self.result["text"] for target in target_languages},
            }
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
    def __init__(self, use_random_replies=True, translation_languages="en", error=None):
        self.settings = SimpleNamespace(
            use_random_replies=use_random_replies, translation_languages=translation_languages
        )
        self.error = error
        self.updates = []

    async def get_or_create_for_guild(self, guild_id):
        if self.error:
            raise self.error
        return self.settings

    async def update_for_guild(self, guild_id, **changes):
        self.updates.append((guild_id, changes))
        for name, value in changes.items():
            setattr(self.settings, name, value)
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


def make_handler(translation=None, chatbot=None, *, access=None, guild_config=None, languages="en", **config):
    """A handler on a server whose chat is translated between ``languages`` (codes)."""
    tracking = FakeTracking()
    if guild_config is None:
        guild_config = FakeGuildConfig(translation_languages=languages)
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
    assert translation.targets == [["English"]]
    assert tracking.calls[0]["source_language"] == "Spanish"
    assert tracking.calls[0]["target_language"] == "en"
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


def test_auto_translation_needs_no_role_and_ignores_banned_users():
    translation = FakeTranslation({"language": "Spanish", "text": "Hi friends"})
    handler, _ = make_handler(translation, access=FakeAccess(banned={10}))

    asyncio.run(handler._handle_auto_translation(FakeMessage("hola amigos")))
    assert translation.calls == []

    member = FakeMessage("hola amigos", author_id=11, roles=())
    asyncio.run(handler._handle_auto_translation(member))
    assert translation.calls == ["hola amigos"] and member.replies[0]["content"] == "> Hi friends"


def test_auto_translation_is_off_until_a_server_picks_its_languages():
    for languages in (None, "", "xx"):
        translation = FakeTranslation({"language": "Spanish", "text": "Hi friends"})
        handler, _ = make_handler(translation, languages=languages)
        message = FakeMessage("hola amigos")
        asyncio.run(handler._handle_auto_translation(message))
        assert translation.calls == [] and message.replies == []

    # A database failure must not switch translation on, nor break the chat handler.
    translation = FakeTranslation({"language": "Spanish", "text": "Hi friends"})
    handler, _ = make_handler(translation, guild_config=FakeGuildConfig(error=RuntimeError("db down")))
    asyncio.run(handler._handle_auto_translation(FakeMessage("hola amigos")))
    assert translation.calls == []


def test_two_server_languages_translate_in_both_directions():
    translation = FakeTranslation()
    handler, tracking = make_handler(translation, languages="en,uk")

    # Clearly English: only the other language is asked for, without language detection.
    translation.result = {"language": "English", "translations": {"Ukrainian": "Ралі на ведмедя о 17:00"}}
    english = FakeMessage("we need to rally the bear at 5 pm")
    asyncio.run(handler._handle_auto_translation(english))
    assert translation.targets[-1] == ["Ukrainian"]
    assert english.replies[0]["content"] == "> Ралі на ведмедя о 17:00"
    assert tracking.calls[-1]["target_language"] == "uk"

    # Ukrainian: the model is asked for both and leaves out the message's own language.
    translation.result = {"language": "Ukrainian", "translations": {"English": "Who is leading the rally?"}}
    ukrainian = FakeMessage("Хто веде ралі?")
    asyncio.run(handler._handle_auto_translation(ukrainian))
    assert translation.targets[-1] == ["English", "Ukrainian"]
    assert ukrainian.replies[0]["content"] == "> Who is leading the rally?"
    assert [call["target_language"] for call in tracking.calls] == ["uk", "en"]


def test_a_third_language_is_translated_into_every_server_language_in_one_reply():
    translation = FakeTranslation(
        {"language": "Korean", "translations": {"English": "Why attack?", "Ukrainian": "Навіщо атакувати?"}}
    )
    handler, tracking = make_handler(translation, languages="uk,en")
    message = FakeMessage("왜 공격해요")

    asyncio.run(handler._handle_auto_translation(message))

    assert len(message.replies) == 1 and message.channel.sent == []
    assert message.replies[0]["content"] == (
        "-# 🇬🇧 English\n> Why attack?\n-# 🇺🇦 Українська\n> Навіщо атакувати?"
    )
    assert message.replies[0]["allowed_mentions"].users is False
    assert [(call["target_language"], call["source_language"]) for call in tracking.calls] == [
        ("en", "Korean"), ("uk", "Korean"),
    ]


def test_translations_into_the_messages_own_language_are_dropped():
    # The model echoed the text for its own language instead of leaving it out.
    translation = FakeTranslation(
        {"language": "Brazilian Portuguese", "translations": {"Portuguese": "bom dia a todos", "English": "x"}}
    )
    handler, tracking = make_handler(translation, languages="en,pt")
    message = FakeMessage("bom dia a todos")
    asyncio.run(handler._handle_auto_translation(message))
    assert message.replies[0]["content"] == "> x" and len(tracking.calls) == 1

    # Reported as another language, but the "translation" is the message itself.
    translation.result = {"language": "Spanish", "translations": {"English": "Hola Amigos", "Portuguese": None}}
    message = FakeMessage("hola amigos")
    asyncio.run(handler._handle_auto_translation(message))
    assert message.replies == []


def test_chat_noises_are_not_translated_in_any_direction():
    translation = FakeTranslation({"language": "English", "text": "x"})
    handler, _ = make_handler(translation, languages="en,uk")

    for content in ("ok", "lol gg", "Хахаха", "ок 👍", "jajaja xD", "ㅋㅋㅋ"):
        message = FakeMessage(content)
        asyncio.run(handler._handle_auto_translation(message))
        assert message.replies == [], content
    assert translation.calls == []


def test_a_backlog_of_translations_skips_new_messages_instead_of_queueing_forever():
    translation = FakeTranslation({"language": "Spanish", "text": "Hi friends"})
    handler, _ = make_handler(translation)
    handler._pending_auto_translations = handler.MAX_PENDING_AUTO_TRANSLATIONS

    asyncio.run(handler._handle_auto_translation(FakeMessage("hola amigos")))
    assert translation.calls == []

    # The counter is given back after a translation, also a failed one.
    handler._pending_auto_translations = 0
    asyncio.run(handler._handle_auto_translation(FakeMessage("hola amigos")))
    translation.error = TranslationUnavailableError("down")
    asyncio.run(handler._handle_auto_translation(FakeMessage("buenas noches")))
    assert len(translation.calls) == 2 and handler._pending_auto_translations == 0


def test_servers_that_used_the_translator_role_keep_english_translation():
    def guild(*, members=(), chunked=True, with_role=True):
        role = SimpleNamespace(name="Translator", id=1, members=list(members))
        return SimpleNamespace(id=456, roles=[role] if with_role else [], chunked=chunked)

    async def adopt(guild_config, server):
        handler, _ = make_handler(guild_config=guild_config)
        await handler.adopt_translator_role(server)
        return guild_config

    in_use = asyncio.run(adopt(FakeGuildConfig(translation_languages=None), guild(members=[object()])))
    assert in_use.updates == [(456, {"translation_languages": "en"})]

    for unused in (guild(), guild(with_role=False)):
        config = asyncio.run(adopt(FakeGuildConfig(translation_languages=None), unused))
        assert config.updates == [(456, {"translation_languages": ""})]

    # A choice that is already stored (also "off") is never overwritten.
    for chosen in ("", "uk,en"):
        config = asyncio.run(adopt(FakeGuildConfig(translation_languages=chosen), guild(members=[object()])))
        assert config.updates == []

    # Without the member list "nobody has the role" is unknown: decide on a later start.
    config = asyncio.run(adopt(FakeGuildConfig(translation_languages=None), guild(chunked=False)))
    assert config.updates == []

    # A failing database is logged, not raised into on_ready.
    asyncio.run(adopt(FakeGuildConfig(error=RuntimeError("db down")), guild(members=[object()])))


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
