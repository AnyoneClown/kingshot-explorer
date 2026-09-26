import asyncio
import logging
import re
from copy import deepcopy
from types import SimpleNamespace

import pytest

from services.chatbot_service import ChatbotService


class FakeClient:
    def __init__(self, responses):
        self.model = "nvidia/nemotron-3-ultra-550b-a55b"
        self.model_kwargs = {"chat_template_kwargs": {"enable_thinking": True, "other_option": "preserved"}}
        self._responses = list(responses)
        self.calls = []

    async def astream(self, messages, **kwargs):
        self.calls.append({"messages": deepcopy(messages), **kwargs})
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        chunks = response if isinstance(response, list) else [{"content": response}]
        for chunk in chunks:
            if isinstance(chunk, BaseException):
                raise chunk
            yield SimpleNamespace(
                content=chunk.get("content", ""),
                additional_kwargs=chunk.get("additional_kwargs", {}),
                response_metadata=chunk.get("response_metadata", {}),
                usage_metadata=chunk.get("usage_metadata"),
            )


def test_generate_contextual_reply_returns_none_when_model_declines():
    service = ChatbotService(
        FakeClient(['{"should_reply":false,"reply":""}']),
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
        max_chat_response_chars=5,
    )

    result = asyncio.run(
        service.generate_contextual_reply(
            "Can you help?",
            [{"author": "Bob", "content": "Need an answer"}],
        )
    )

    assert result == "12345"


def test_generate_contextual_reply_streams_final_content_and_omits_reasoning():
    client = FakeClient(
        [
            [
                {
                    "content": "",
                    "additional_kwargs": {"reasoning_content": "private reasoning"},
                },
                {"content": '{"should_reply":true,'},
                {"content": '"reply":"Streamed answer"}'},
            ]
        ]
    )
    service = ChatbotService(client)

    result = asyncio.run(
        service.generate_contextual_reply(
            "Can you help?",
            [{"author": "Bob", "content": "Need an answer"}],
            force_reply=True,
        )
    )

    assert result == "Streamed answer"
    assert "private reasoning" not in result


def test_generate_contextual_reply_removes_reasoning_tags_from_content():
    service = ChatbotService(
        FakeClient(
            [
                "<think>private reasoning</think>"
                '{"should_reply":true,"reply":"Visible answer"}'
            ]
        )
    )

    result = asyncio.run(
        service.generate_contextual_reply(
            "Can you help?",
            [],
            force_reply=True,
        )
    )

    assert result == "Visible answer"


def test_generate_contextual_reply_includes_structured_context_and_reply_target():
    client = FakeClient(['{"should_reply":true,"reply":"Use rally chat"}'])
    service = ChatbotService(client)

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

    sent_messages = client.calls[0]["messages"]
    prompt_text = "\n".join(message["content"] for message in sent_messages)

    assert result == "Use rally chat"
    assert "AI Clown [bot]: Use the event channel" in prompt_text
    assert "Message being replied to:" in prompt_text
    assert "Bob: Should we coordinate rally leaders?" in prompt_text
    assert "The user is directly addressing the bot" in prompt_text


def test_generate_contextual_reply_preserves_raw_discord_mentions_as_placeholders():
    client = FakeClient(['{"should_reply":true,"reply":"Alice is in strategy chat"}'])
    service = ChatbotService(client)

    result = asyncio.run(
        service.generate_contextual_reply(
            "<@123> ask <@&456> in <#789> <:wave:987>",
            [],
            force_reply=True,
        )
    )

    prompt_text = "\n".join(message["content"] for message in client.calls[0]["messages"])

    assert result == "Alice is in strategy chat"
    assert "@user-123 ask @role-456 in #channel-789 :wave:" in prompt_text


def test_generate_contextual_reply_includes_strict_random_reply_policy():
    client = FakeClient(['{"should_reply":false,"reply":""}'])
    service = ChatbotService(client)

    result = asyncio.run(
        service.generate_contextual_reply(
            "ok thanks",
            [{"author": "Alice", "content": "Problem solved"}],
            force_reply=False,
        )
    )

    sent_messages = client.calls[0]["messages"]
    prompt_text = "\n".join(message["content"] for message in sent_messages)

    assert result is None
    assert "The bot is considering a random reply" in prompt_text
    assert "Return should_reply false for announcements, commands, logs" in prompt_text
    assert "quick-witted regular in the chat" in prompt_text
    assert "dry humor, playful sarcasm" in prompt_text
    assert "genuinely funny context-specific one-liner" in prompt_text
    assert "skip humor when it would feel insensitive" in prompt_text


def test_generate_contextual_reply_force_reply_uses_text_when_model_declines():
    service = ChatbotService(
        FakeClient(['{"should_reply":false,"reply":"What do you need help with?"}']),
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
    client = FakeClient(['{"should_reply":false,"reply":""}', '{"should_reply":true,"reply":""}'])
    service = ChatbotService(client)

    result = asyncio.run(
        service.generate_contextual_reply(
            "<@123456>",
            [],
            force_reply=True,
        )
    )

    assert result == "I couldn't generate a reply just now. Please try again in a moment."
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    "first_response",
    [
        "",
        "   ",
        [],
        [{"additional_kwargs": {"reasoning_content": "private reasoning"}}],
        "<think>unfinished private reasoning",
        "<think>private reasoning</think>",
        '{"should_reply":true,"reply":',
        '{"reply":}',
        "{}",
        "[]",
        "null",
        '{"should_reply":true,"reply":"   "}',
        '{"should_reply":false,"reply":""}',
        '{"should_reply":true,"reply":null}',
        '{"should_reply":true,"reply":[]}',
        '{"should_reply":true,"reply":42}',
    ],
)
def test_generate_contextual_reply_retries_unusable_direct_response_with_same_context(first_response):
    client = FakeClient([first_response, '{"should_reply":true,"reply":"The seaweed one."}'])
    original_model_kwargs = deepcopy(client.model_kwargs)
    service = ChatbotService(client)

    result = asyncio.run(
        service.generate_contextual_reply(
            "Which do you prefer?",
            [{"author": "Alice", "content": "We have seaweed and prawn snacks."}],
            force_reply=True,
            reply_context={"author": "Bot", "is_bot": True, "content": "Both snacks sound good."},
        )
    )

    assert result == "The seaweed one."
    assert len(client.calls) == 2
    assert client.calls[0]["messages"] == client.calls[1]["messages"]
    prompt = client.calls[1]["messages"][1]["content"]
    assert "Alice: We have seaweed and prawn snacks." in prompt
    assert "Bot [bot]: Both snacks sound good." in prompt
    assert 'User: "Which do you prefer?"' in prompt
    assert "chat_template_kwargs" not in client.calls[0]
    assert client.calls[1]["chat_template_kwargs"] == {"enable_thinking": False, "other_option": "preserved"}
    assert client.model_kwargs == original_model_kwargs


@pytest.mark.parametrize(
    "response",
    ["", '{"reply":}', '{"should_reply":true,"reply":null}', RuntimeError("upstream unavailable")],
)
def test_generate_contextual_reply_stops_after_two_failed_attempts(response):
    client = FakeClient([response, response])
    service = ChatbotService(client)

    result = asyncio.run(service.generate_contextual_reply("Can you help?", [], force_reply=True))

    assert result == "I couldn't generate a reply just now. Please try again in a moment."
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    "response",
    [
        "",
        "plain text without a reply decision",
        '{"should_reply":false,"reply":""}',
        '{"should_reply":true,"reply":null}',
        RuntimeError("upstream unavailable"),
    ],
)
def test_generate_contextual_reply_does_not_retry_or_send_failures_for_random_candidates(response):
    client = FakeClient([response])

    result = asyncio.run(ChatbotService(client).generate_contextual_reply("Maybe?", []))

    assert result is None
    assert len(client.calls) == 1


def test_generate_contextual_reply_retries_stream_error_without_reusing_partial_answer():
    client = FakeClient(
        [
            [{"content": '{"should_reply":true,"reply":"Incomplete'}, RuntimeError("stream interrupted")],
            '{"should_reply":true,"reply":"Complete answer"}',
        ]
    )

    result = asyncio.run(ChatbotService(client).generate_contextual_reply("Can you help?", [], force_reply=True))

    assert result == "Complete answer"
    assert len(client.calls) == 2


def test_generate_contextual_reply_propagates_cancellation_without_retry():
    client = FakeClient([asyncio.CancelledError()])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ChatbotService(client).generate_contextual_reply("Can you help?", [], force_reply=True))

    assert len(client.calls) == 1


def test_generate_contextual_reply_logs_stop_reason_and_separate_usage_chunk_without_reasoning(caplog):
    client = FakeClient(
        [
            [
                {"additional_kwargs": {"reasoning_content": "private reasoning"}},
                {"response_metadata": {"finish_reason": "length"}},
                {"usage_metadata": {"input_tokens": 100, "output_tokens": 16384, "total_tokens": 16484}},
            ],
            [
                {"content": '{"should_reply":true,"reply":"Visible answer"}'},
                {"response_metadata": {"finish_reason": "stop"}},
                {"usage_metadata": {"input_tokens": 100, "output_tokens": 12, "total_tokens": 112}},
            ],
        ]
    )

    with caplog.at_level(logging.INFO, logger="services.chatbot_service"):
        result = asyncio.run(ChatbotService(client).generate_contextual_reply("Can you help?", [], force_reply=True))

    assert result == "Visible answer"
    summaries = [record.message for record in caplog.records if "ChatNVIDIA stream summary:" in record.message]
    assert len(summaries) == 2
    assert "attempt=1 completed=True thinking_disabled=False finish_reason=length" in summaries[0]
    assert "input_tokens=100 output_tokens=16384 total_tokens=16484" in summaries[0]
    assert "reasoning_chunks=1 reasoning_chars=17 content_chars=0 answer_chars=0" in summaries[0]
    assert "attempt=2 completed=True thinking_disabled=True finish_reason=stop" in summaries[1]
    assert "input_tokens=100 output_tokens=12 total_tokens=112" in summaries[1]
    assert "reason=empty_response" in caplog.text
    assert "private reasoning" not in caplog.text
    request_ids = re.findall(r"request_id=([0-9a-f]+)", caplog.text)
    assert len(set(request_ids)) == 1


def test_generate_contextual_reply_logs_interrupted_stream_with_missing_usage(caplog):
    client = FakeClient([RuntimeError("stream interrupted"), "Recovered answer"])

    with caplog.at_level(logging.INFO, logger="services.chatbot_service"):
        result = asyncio.run(ChatbotService(client).generate_contextual_reply("Can you help?", [], force_reply=True))

    assert result == "Recovered answer"
    assert "attempt=1 completed=False" in caplog.text
    assert "finish_reason=None input_tokens=None output_tokens=None total_tokens=None" in caplog.text
    assert "reason=completion_error" in caplog.text


def test_generate_contextual_reply_next_request_keeps_default_thinking():
    client = FakeClient(["", "Recovered answer", "Next answer"])
    service = ChatbotService(client)

    async def run_requests():
        first = await service.generate_contextual_reply("Can you help?", [], force_reply=True)
        second = await service.generate_contextual_reply("Anything else?", [], force_reply=True)
        return first, second

    assert asyncio.run(run_requests()) == ("Recovered answer", "Next answer")
    assert len(client.calls) == 3
    assert "chat_template_kwargs" not in client.calls[2]
    assert client.model_kwargs["chat_template_kwargs"]["enable_thinking"] is True
