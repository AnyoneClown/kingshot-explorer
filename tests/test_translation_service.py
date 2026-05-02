import asyncio
from types import SimpleNamespace

from services.translation_service import TranslationService


class FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        content = self._responses.pop(0)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                )
            ]
        )


class FakeClient:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=FakeCompletions(responses))


def test_translate_to_english_parses_json_payload():
    service = TranslationService(
        FakeClient(['{"language":"Spanish","text":"Hello everyone"}']),
        model="openai/gpt-oss-120b",
    )

    result = asyncio.run(service.translate_to_english("Hola a todos"))

    assert result == {"language": "Spanish", "text": "Hello everyone"}


def test_translate_to_language_parses_code_fenced_json():
    service = TranslationService(
        FakeClient(['```json\n{"text":"Bonjour"}\n```']),
        model="openai/gpt-oss-120b",
    )

    result = asyncio.run(service.translate_to_language("Hello", "French"))

    assert result == {"text": "Bonjour"}


def test_generate_contextual_reply_returns_none_when_model_declines():
    service = TranslationService(
        FakeClient(['{"should_reply":false,"reply":""}']),
        model="openai/gpt-oss-120b",
    )

    result = asyncio.run(
        service.generate_contextual_reply(
            "ok",
            [{"author": "Alice", "content": "We already solved it"}],
        )
    )

    assert result is None


def test_generate_contextual_reply_truncates_long_output():
    service = TranslationService(
        FakeClient(['{"should_reply":true,"reply":"1234567890"}']),
        model="openai/gpt-oss-120b",
        max_chat_response_chars=5,
    )

    result = asyncio.run(
        service.generate_contextual_reply(
            "Can you help?",
            [{"author": "Bob", "content": "Need an answer"}],
        )
    )

    assert result == "12345"


def test_generate_contextual_reply_includes_structured_context_and_reply_target():
    client = FakeClient(['{"should_reply":true,"reply":"Use rally chat"}'])
    service = TranslationService(client, model="openai/gpt-oss-120b")

    result = asyncio.run(
        service.generate_contextual_reply(
            "what about this?",
            [
                {
                    "author": "Alice",
                    "content": "We are talking about bear trap timing",
                    "is_bot": False,
                    "timestamp": "2026-05-02T10:00:00+00:00",
                },
                {
                    "author": "AI Clown",
                    "content": "Use the event channel",
                    "is_bot": True,
                    "timestamp": "2026-05-02T10:01:00+00:00",
                },
            ],
            reply_context={
                "author": "Bob",
                "content": "Should we coordinate rally leaders?",
                "is_bot": False,
                "timestamp": "2026-05-02T10:02:00+00:00",
            },
            force_reply=True,
        )
    )

    sent_messages = client.chat.completions.calls[0]["messages"]
    prompt_text = "\n".join(message["content"] for message in sent_messages)

    assert result == "Use rally chat"
    assert "AI Clown [bot]: Use the event channel" in prompt_text
    assert "Message being replied to:" in prompt_text
    assert "Bob: Should we coordinate rally leaders?" in prompt_text
    assert "The user is directly addressing the bot" in prompt_text


def test_generate_contextual_reply_includes_strict_random_reply_policy():
    client = FakeClient(['{"should_reply":false,"reply":""}'])
    service = TranslationService(client, model="openai/gpt-oss-120b")

    result = asyncio.run(
        service.generate_contextual_reply(
            "ok thanks",
            [{"author": "Alice", "content": "Problem solved"}],
            force_reply=False,
        )
    )

    sent_messages = client.chat.completions.calls[0]["messages"]
    prompt_text = "\n".join(message["content"] for message in sent_messages)

    assert result is None
    assert "The bot is considering a random reply" in prompt_text
    assert "Return should_reply false for announcements, commands, logs" in prompt_text
