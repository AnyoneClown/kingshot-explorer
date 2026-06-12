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
