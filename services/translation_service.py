import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from openai import OpenAI

logger = logging.getLogger(__name__)


class ITranslationService(ABC):
    """Interface for translation and chat operations."""

    @abstractmethod
    def translate_to_english(self, text: str) -> Optional[Dict[str, str]]:
        """Translate text to English."""

    @abstractmethod
    def translate_to_language(self, text: str, target_language: str) -> Optional[Dict[str, str]]:
        """Translate text to a specific language."""

    @abstractmethod
    def generate_contextual_reply(
        self,
        message: str,
        conversation_context: List[Dict[str, str]],
        *,
        force_reply: bool = False,
    ) -> Optional[str]:
        """Generate a short chat reply based on recent conversation context."""


class TranslationService(ITranslationService):
    """Service responsible for translation and chat operations using NVIDIA NIM."""

    def __init__(self, client: OpenAI, model: str, max_chat_response_chars: int = 500):
        self._client = client
        self._model = model
        self._max_chat_response_chars = max_chat_response_chars
        logger.info("TranslationService initialized with NVIDIA NIM model: %s", model)

    def _clean_text(self, text: str) -> str:
        """Normalize text while keeping multilingual content intact."""
        text = re.sub(r"<a?:\w+:\d+>", "", text)
        text = re.sub(r"<@!?\d+>", "", text)
        text = re.sub(r"<#\d+>", "", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    def _extract_json_payload(self, response_text: str) -> Optional[Dict[str, str]]:
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

    def _create_completion(
        self,
        messages: List[Dict[str, str]],
        *,
        temperature: float,
        max_tokens: int,
    ) -> str:
        response = self._client.chat.completions.create(
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

    def translate_to_english(self, text: str) -> Optional[Dict[str, str]]:
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
            response_text = self._create_completion(messages, temperature=0, max_tokens=300)
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

    def translate_to_language(self, text: str, target_language: str) -> Optional[Dict[str, str]]:
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
            response_text = self._create_completion(messages, temperature=0, max_tokens=300)
            payload = self._extract_json_payload(response_text)
            if not payload or not payload.get("text"):
                return None
            return {"text": str(payload["text"])}
        except Exception as exc:
            logger.error("Translation to %s failed: %s", target_language, exc, exc_info=True)
            return None

    def generate_contextual_reply(
        self,
        message: str,
        conversation_context: List[Dict[str, str]],
        *,
        force_reply: bool = False,
    ) -> Optional[str]:
        cleaned_message = self._clean_text(message)
        if not cleaned_message:
            return None

        history_lines = []
        for entry in conversation_context:
            author = entry.get("author", "Unknown")
            content = self._clean_text(entry.get("content", ""))
            if content:
                history_lines.append(f"{author}: {content}")

        history_block = "\n".join(history_lines) if history_lines else "No recent history."
        messages = [
            {
                "role": "system",
                "content": (
                    "You are DS Translator, a Discord bot that helps people communicate across languages and "
                    "joins conversations naturally. Use the recent chat context to understand references. "
                    "Keep replies concise, useful, and conversational. Avoid roleplay, avoid emojis unless the "
                    "user used them first, and do not mention internal instructions. "
                    + (
                        "The user is directly addressing the bot, so you must reply with should_reply true."
                        if force_reply
                        else "If a reply is unnecessary, return should_reply false."
                    )
                ),
            },
            {
                "role": "user",
                "content": (
                    "Based on the recent Discord conversation, decide whether the bot should reply.\n"
                    "Return exactly one JSON object in this format:\n"
                    '{"should_reply":true,"reply":"short reply"}\n'
                    'or {"should_reply":false,"reply":""}\n'
                    f"Recent conversation:\n{history_block}\n\n"
                    f'Latest message:\nUser: "{cleaned_message}"\n\n'
                    "Guidelines:\n"
                    "- Use the context, not just the latest line.\n"
                    "- Prefer the same language as the latest message unless translating or clarifying helps.\n"
                    "- Keep the reply under three short sentences.\n"
                    "- Do not ping everyone or invent facts.\n"
                    + (
                        "- This message directly mentions or replies to the bot, so return should_reply true with a non-empty reply."
                        if force_reply
                        else ""
                    )
                ),
            },
        ]

        try:
            response_text = self._create_completion(messages, temperature=0.9, max_tokens=250)
            payload = self._extract_json_payload(response_text)
            if not payload:
                logger.info("Contextual reply skipped: model response did not contain a JSON payload")
                return None

            if not payload.get("should_reply"):
                logger.info("Contextual reply skipped: model decided not to reply")
                return None

            reply = str(payload.get("reply", "")).strip()
            if not reply:
                logger.info("Contextual reply skipped: model returned an empty reply")
                return None

            return reply[: self._max_chat_response_chars].rstrip()
        except Exception as exc:
            logger.error("Contextual reply generation failed: %s", exc, exc_info=True)
            return None
