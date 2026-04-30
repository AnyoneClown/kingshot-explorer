from types import SimpleNamespace

from services.translation_service import TranslationService


class FakeCompletions:
    def __init__(self, responses):
        self._responses = list(responses)

    def create(self, **kwargs):
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

    result = service.translate_to_english("Hola a todos")

    assert result == {"language": "Spanish", "text": "Hello everyone"}


def test_translate_to_language_parses_code_fenced_json():
    service = TranslationService(
        FakeClient(['```json\n{"text":"Bonjour"}\n```']),
        model="openai/gpt-oss-120b",
    )

    result = service.translate_to_language("Hello", "French")

    assert result == {"text": "Bonjour"}


def test_generate_contextual_reply_returns_none_when_model_declines():
    service = TranslationService(
        FakeClient(['{"should_reply":false,"reply":""}']),
        model="openai/gpt-oss-120b",
    )

    result = service.generate_contextual_reply(
        "ok",
        [{"author": "Alice", "content": "We already solved it"}],
    )

    assert result is None


def test_generate_contextual_reply_truncates_long_output():
    service = TranslationService(
        FakeClient(['{"should_reply":true,"reply":"1234567890"}']),
        model="openai/gpt-oss-120b",
        max_chat_response_chars=5,
    )

    result = service.generate_contextual_reply(
        "Can you help?",
        [{"author": "Bob", "content": "Need an answer"}],
    )

    assert result == "12345"
