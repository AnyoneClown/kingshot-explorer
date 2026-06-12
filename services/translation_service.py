import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)


class ITranslationService(ABC):
    """Interface for translation operations."""

    @abstractmethod
    async def translate_to_english(self, text: str) -> Optional[Dict[str, str]]:
        """Translate text to English."""

    @abstractmethod
    async def translate_to_language(self, text: str, target_language: str) -> Optional[Dict[str, str]]:
        """Translate text to a specific language."""


class TranslationService(ITranslationService):
    """Service responsible for translation operations using NVIDIA NIM."""

    def __init__(self, client: AsyncOpenAI, model: str):
        self._client = client
        self._model = model
        logger.info("TranslationService initialized with NVIDIA NIM model: %s", model)

    def _clean_text(self, text: str) -> str:
        """Normalize text while keeping multilingual content intact."""
        text = re.sub(r"<a?:\w+:\d+>", "", text)
        text = re.sub(r"<@!?\d+>", "", text)
        text = re.sub(r"<#\d+>", "", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _extract_json_payload(self, response_text: str) -> Optional[Dict[str, object]]:
        """Extract the first JSON object from a model response."""
        if not response_text:
            return None

        stripped = response_text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
            stripped = re.sub(r"\s*```$", "", stripped)

        decoder = json.JSONDecoder()
        for index, char in enumerate(stripped):
            if char != "{":
                continue
            try:
                payload, _ = decoder.raw_decode(stripped[index:])
                if isinstance(payload, dict):
                    return payload
            except json.JSONDecodeError:
                continue
        return None

    async def _create_completion(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=temperature,
            top_p=1,
            max_tokens=max_tokens,
        )

        if not response.choices:
            return ""

        content = response.choices[0].message.content
        if isinstance(content, list):
            return "".join(part.get("text", "") for part in content if isinstance(part, dict))
        return content or ""

    async def translate_to_english(self, text: str) -> Optional[Dict[str, str]]:
        text_cleaned = self._clean_text(text)
        if not text_cleaned:
            return None

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a deterministic language detection and translation engine. "
                    "Return JSON only. If the input is unusable, return null."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Detect the original language and translate the text into English.\n"
                    "Rules:\n"
                    '- If the text is already English, set "language" to "English" and return the original text.\n'
                    "- Preserve meaning and tone.\n"
                    "- Do not explain anything.\n"
                    "Return exactly one JSON object in this format:\n"
                    '{"language":"<language name in English>","text":"<English translation>"}\n'
                    f'Text: "{text_cleaned}"'
                ),
            },
        ]

        try:
            response_text = await self._create_completion(messages, temperature=0, max_tokens=300)
            payload = self._extract_json_payload(response_text)
            if not payload:
                return None

            language = payload.get("language")
            translated_text = payload.get("text")
            if not language or not translated_text:
                return None

            return {"language": str(language), "text": str(translated_text)}
        except Exception as exc:
            logger.error("Translation to English failed: %s", exc, exc_info=True)
            return None

    async def translate_to_language(self, text: str, target_language: str) -> Optional[Dict[str, str]]:
        text_cleaned = self._clean_text(text)
        if not text_cleaned:
            return None

        messages = [
            {
                "role": "system",
                "content": "You are a deterministic translator. Return JSON only and do not add commentary.",
            },
            {
                "role": "user",
                "content": (
                    f"Translate the following text into {target_language}. Preserve meaning and tone.\n"
                    'Return exactly one JSON object in this format: {"text":"translated text"}\n'
                    f'Text: "{text_cleaned}"'
                ),
            },
        ]

        try:
            response_text = await self._create_completion(messages, temperature=0, max_tokens=300)
            payload = self._extract_json_payload(response_text)
            if not payload or not payload.get("text"):
                return None
            return {"text": str(payload["text"])}
        except Exception as exc:
            logger.error("Translation to %s failed: %s", target_language, exc, exc_info=True)
            return None
