import asyncio
from types import SimpleNamespace

from services.chatbot_service import ChatbotService


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


def test_generate_contextual_reply_returns_none_when_model_declines():
    service = ChatbotService(
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
    service = ChatbotService(
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
    service = ChatbotService(client, model="openai/gpt-oss-120b")

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
    service = ChatbotService(client, model="openai/gpt-oss-120b")

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


def test_generate_contextual_reply_force_reply_uses_text_when_model_declines():
    service = ChatbotService(
        FakeClient(['{"should_reply":false,"reply":"What do you need help with?"}']),
        model="openai/gpt-oss-120b",
    )

    result = asyncio.run(
        service.generate_contextual_reply(
            "<@123456>",
            [],
            force_reply=True,
        )
    )

    assert result == "What do you need help with?"


def test_generate_contextual_reply_force_reply_uses_plain_text_when_model_omits_json():
    service = ChatbotService(
        FakeClient(["You just said: JUST SAY WHAT DID I SAY JUST NOW"]),
        model="openai/gpt-oss-120b",
    )

    result = asyncio.run(
        service.generate_contextual_reply(
            "JUST SAY WHAT DID I SAY JUST NOW",
            [{"author": "Denis", "content": "What did reg say?"}],
            force_reply=True,
        )
    )

    assert result == "You just said: JUST SAY WHAT DID I SAY JUST NOW"


def test_generate_contextual_reply_recovers_truncated_json_reply():
    expected_reply = (
        "Reginald’s last thing was “Holy guacamole.” Earlier he asked about eBay items, "
        "said “Jesus Christ,” “Welp,” and “I’m sleep-deprived giggling.”"
    )
    service = ChatbotService(
        FakeClient(['{"should_reply":true,"reply":"' + expected_reply]),
        model="openai/gpt-oss-120b",
    )

    result = asyncio.run(
        service.generate_contextual_reply(
            "What did reg say?",
            [{"author": "Reginald", "content": "Holy guacamole"}],
            force_reply=True,
        )
    )

    assert result == expected_reply


def test_generate_contextual_reply_random_candidate_ignores_plain_text_without_json():
    service = ChatbotService(
        FakeClient(["This is a plain text answer"]),
        model="openai/gpt-oss-120b",
    )

    result = asyncio.run(
        service.generate_contextual_reply(
            "maybe",
            [{"author": "Denis", "content": "What did reg say?"}],
            force_reply=False,
        )
    )

    assert result is None


def test_generate_contextual_reply_force_reply_falls_back_when_model_returns_empty_reply():
    service = ChatbotService(
        FakeClient(['{"should_reply":false,"reply":""}']),
        model="openai/gpt-oss-120b",
    )

    result = asyncio.run(
        service.generate_contextual_reply(
            "<@123456>",
            [],
            force_reply=True,
        )
    )

    assert result == "I saw your message, but I need a little more context to answer."
