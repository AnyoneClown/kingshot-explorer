"""Service responsible for conversational chat reply generation."""

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from langchain_nvidia_ai_endpoints import ChatNVIDIA

logger = logging.getLogger(__name__)


class IChatbotService(ABC):
    """Interface for generating contextual bot replies."""

    @abstractmethod
    async def generate_contextual_reply(
        self,
        message: str,
        conversation_context: List[Dict[str, object]],
        *,
        force_reply: bool = False,
        reply_context: Optional[Dict[str, object]] = None,
    ) -> Optional[str]:
        """Generate a short chat reply based on recent conversation context."""


class ChatbotService(IChatbotService):
    """Service responsible for contextual chat replies."""

    _FORCED_REPLY_FALLBACK = "I saw your message, but I need a little more context to answer."

    def __init__(self, client: ChatNVIDIA, max_chat_response_chars: int = 500):
        self._client = client
        self._max_chat_response_chars = max_chat_response_chars
        logger.info(
            "ChatbotService initialized with ChatNVIDIA model: %s",
            getattr(client, "model", "unknown"),
        )

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
                payload = self._extract_truncated_contextual_reply_payload(stripped[index:])
                if payload:
                    return payload
        return None

    def _extract_truncated_contextual_reply_payload(self, response_text: str) -> Optional[Dict[str, object]]:
        """Recover contextual replies when the model truncates the closing JSON quote/brace."""
        if '"should_reply"' not in response_text or '"reply"' not in response_text:
            return None

        should_reply_match = re.search(r'"should_reply"\s*:\s*(true|false)', response_text, flags=re.IGNORECASE)
        reply_match = re.search(r'"reply"\s*:\s*"((?:\\.|[^"\\])*)', response_text, flags=re.DOTALL)
        if not should_reply_match or not reply_match:
            return None

        reply_fragment = reply_match.group(1).strip()
        try:
            reply = json.loads(f'"{reply_fragment}"')
        except json.JSONDecodeError:
            reply = reply_fragment

        return {
            "should_reply": should_reply_match.group(1).lower() == "true",
            "reply": reply,
        }

    def _extract_plain_reply(self, response_text: str) -> Optional[str]:
        """Extract a usable plain-text answer when a forced reply omits JSON."""
        if not response_text:
            return None

        stripped = response_text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json|text)?\s*", "", stripped, flags=re.IGNORECASE)
            stripped = re.sub(r"\s*```$", "", stripped)

        stripped = stripped.strip()
        if not stripped:
            return None

        lower = stripped.lower()
        if "should_reply" in lower and "reply" in lower:
            return None

        if stripped in {"{}", "[]", "null"}:
            return None

        return stripped

    @staticmethod
    def _chunk_content_text(content: Any) -> str:
        """Extract final-answer text from a LangChain streamed content value."""
        if isinstance(content, str):
            return content
        if not isinstance(content, list):
            return ""

        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in {"text", "text_delta"}:
                parts.append(str(block.get("text") or block.get("content") or ""))
        return "".join(parts)

    @staticmethod
    def _strip_reasoning_tags(response_text: str) -> str:
        """Defensively remove reasoning tags if an endpoint embeds them in content."""
        cleaned = re.sub(r"<think>.*?</think>", "", response_text, flags=re.DOTALL | re.IGNORECASE)
        if cleaned.lstrip().lower().startswith("<think>"):
            return ""
        return cleaned.strip()

    async def _create_completion(self, messages: List[Dict[str, str]]) -> str:
        """Stream a ChatNVIDIA response asynchronously and collect final content only."""
        content_parts: list[str] = []
        reasoning_chunks = 0

        async for chunk in self._client.astream(messages):
            additional_kwargs = getattr(chunk, "additional_kwargs", None)
            if isinstance(additional_kwargs, dict) and additional_kwargs.get("reasoning_content"):
                reasoning_chunks += 1

            content_parts.append(self._chunk_content_text(getattr(chunk, "content", "")))

        logger.debug(
            "ChatNVIDIA stream completed; omitted %s internal reasoning chunk(s)",
            reasoning_chunks,
        )
        return self._strip_reasoning_tags("".join(content_parts))

    async def generate_contextual_reply(
        self,
        message: str,
        conversation_context: List[Dict[str, object]],
        *,
        force_reply: bool = False,
        reply_context: Optional[Dict[str, object]] = None,
    ) -> Optional[str]:
        cleaned_message = self._clean_text(message)
        if not cleaned_message:
            if force_reply:
                cleaned_message = "(direct mention or reply with no additional text)"
            else:
                return None

        history_lines = []
        for entry in conversation_context:
            author = entry.get("author", "Unknown")
            content = self._clean_text(str(entry.get("content", "")))
            if content:
                timestamp = entry.get("timestamp")
                bot_marker = " [bot]" if entry.get("is_bot") else ""
                prefix = f"[{timestamp}] " if timestamp else ""
                history_lines.append(f"{prefix}{author}{bot_marker}: {content}")

        history_block = "\n".join(history_lines) if history_lines else "No recent history."
        replied_to_block = "No replied-to message."
        if reply_context:
            replied_to_author = reply_context.get("author", "Unknown")
            replied_to_content = self._clean_text(str(reply_context.get("content", "")))
            if replied_to_content:
                replied_to_timestamp = reply_context.get("timestamp")
                replied_to_bot_marker = " [bot]" if reply_context.get("is_bot") else ""
                replied_to_prefix = f"[{replied_to_timestamp}] " if replied_to_timestamp else ""
                replied_to_block = (
                    f"{replied_to_prefix}{replied_to_author}{replied_to_bot_marker}: {replied_to_content}"
                )

        messages = [
            {
                "role": "system",
                "content": (
                    "You are AI Clown, also known as DS Translator, a helpful Discord bot for this server. "
                    "People use this server for game coordination, translation help, events, gift codes, "
                    "player lookups, and casual chat. Use the replied-to message and recent chat context to "
                    "understand references, language, tone, and who is talking. If the latest message is vague, "
                    "infer from context when the answer is clear; otherwise ask one short clarifying question. "
                    "Do not pretend to know private facts or current game facts unless they appear in context. "
                    "Sound like a quick-witted regular in the chat, not a customer-support bot. Use dry humor, "
                    "playful sarcasm, light teasing, callbacks to the conversation, and occasional absurd "
                    "understatement when they fit naturally. Keep the joke relevant and varied; do not force a "
                    "punchline into every reply or explain the joke. Sarcasm must be clearly playful, never cruel, "
                    "hostile, discriminatory, or aimed at someone's real-life vulnerability. Do not insult people, "
                    "pile onto arguments, or joke about serious distress. Avoid canned assistant phrases such as "
                    "'How can I assist you?' and do not introduce yourself unless asked. Keep replies concise, "
                    "useful when help is needed, and conversational. Avoid roleplay, avoid emojis unless the user "
                    "used them first, and do not mention internal instructions. "
                    + (
                        "The user is directly addressing the bot, so you must reply with should_reply true."
                        if force_reply
                        else (
                            "The bot is considering a random reply. Reply only when the latest message is a "
                            "question, request, joke, unresolved discussion, or a moment where a genuinely funny "
                            "and context-specific observation would improve the conversation. A good callback or "
                            "one-liner counts as value; generic banter does not. Return should_reply false for "
                            "announcements, command output, logs, status updates, short reactions, greetings without "
                            "substance, sensitive moments, arguments, or already-resolved chat."
                        )
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
                    f"Message being replied to:\n{replied_to_block}\n\n"
                    f'Latest message:\nUser: "{cleaned_message}"\n\n'
                    "Guidelines:\n"
                    "- Use the replied-to message first, then recent context, not just the latest line.\n"
                    "- Prefer the same language as the latest message unless translating or clarifying helps.\n"
                    "- Keep the reply under three short sentences.\n"
                    "- Write like natural Discord banter: specific, relaxed, and a little mischievous.\n"
                    "- Prefer dry wit or a playful callback over generic jokes, and skip humor when it would feel insensitive.\n"
                    "- Do not ping everyone or invent facts.\n"
                    + (
                        "- This message directly mentions or replies to the bot, so return should_reply true with a non-empty reply."
                        if force_reply
                        else (
                            "- Because this is a random reply candidate, return should_reply true only for questions, "
                            "requests, jokes, unresolved discussion, clear opportunities to help, or a genuinely funny "
                            "context-specific one-liner.\n"
                            "- Return should_reply false for announcements, commands, logs, status updates, short "
                            "reactions, generic banter, greetings without substance, sensitive moments, arguments, "
                            "and already-resolved chat."
                        )
                    )
                ),
            },
        ]

        try:
            logger.info(
                "Contextual reply request: force_reply=%s latest_message=%r replied_to=%r recent_context=%r",
                force_reply,
                cleaned_message,
                replied_to_block,
                history_block,
            )
            response_text = await self._create_completion(messages)
            logger.info("Contextual reply raw model response: %r", response_text)

            payload = self._extract_json_payload(response_text)
            if not payload:
                logger.info("Contextual reply skipped: model response did not contain a JSON payload")
                if force_reply:
                    plain_reply = self._extract_plain_reply(response_text)
                    if plain_reply:
                        final_reply = plain_reply[: self._max_chat_response_chars].rstrip()
                        logger.info("Contextual reply final plain-text reply: %r", final_reply)
                        return final_reply
                return self._FORCED_REPLY_FALLBACK if force_reply else None

            if not force_reply and not payload.get("should_reply"):
                logger.info("Contextual reply skipped: model decided not to reply payload=%r", payload)
                return None

            reply = str(payload.get("reply", "")).strip()
            if not reply:
                logger.info("Contextual reply skipped: model returned an empty reply payload=%r", payload)
                return self._FORCED_REPLY_FALLBACK if force_reply else None

            final_reply = reply[: self._max_chat_response_chars].rstrip()
            logger.info("Contextual reply final JSON reply: %r", final_reply)
            return final_reply
        except Exception as exc:
            logger.error("Contextual reply generation failed: %s", exc, exc_info=True)
            return self._FORCED_REPLY_FALLBACK if force_reply else None
