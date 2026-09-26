"""Service responsible for conversational chat reply generation."""

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from uuid import uuid4

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

    _FORCED_REPLY_FALLBACK = "I couldn't generate a reply just now. Please try again in a moment."

    def __init__(self, client: ChatNVIDIA, max_chat_response_chars: int = 500):
        self._client = client
        self._max_chat_response_chars = max_chat_response_chars
        logger.info(
            "ChatbotService initialized with ChatNVIDIA model: %s",
            getattr(client, "model", "unknown"),
        )

    def _clean_text(self, text: str) -> str:
        """Normalize text while keeping multilingual content intact."""
        # Discord messages normally arrive pre-resolved as @name/#channel by the
        # handler. Preserve meaningful placeholders if raw tokens reach this layer.
        text = re.sub(r"<a?:(\w+):\d+>", r":\1:", text)
        text = re.sub(r"<@&(\d+)>", r"@role-\1", text)
        text = re.sub(r"<@!?(\d+)>", r"@user-\1", text)
        text = re.sub(r"<#(\d+)>", r"#channel-\1", text)
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

        if stripped.startswith(("{", "[")) or stripped in {"null", "true", "false"}:
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

    async def _create_completion(
        self,
        messages: List[Dict[str, str]],
        *,
        request_id: str,
        attempt: int,
    ) -> str:
        """Stream a ChatNVIDIA response asynchronously and collect final content only."""
        content_parts: list[str] = []
        chunks = 0
        reasoning_chunks = 0
        reasoning_chars = 0
        finish_reason = None
        usage: dict[str, int] = {}
        response_text = ""
        completed = False
        started_at = time.monotonic()
        stream_kwargs: dict[str, Any] = {}

        if attempt > 1:
            # Override only this invocation; other concurrent requests keep thinking enabled.
            model_kwargs = getattr(self._client, "model_kwargs", {}) or {}
            template_kwargs = dict(model_kwargs.get("chat_template_kwargs") or {})
            template_kwargs["enable_thinking"] = False
            stream_kwargs["chat_template_kwargs"] = template_kwargs

        try:
            async for chunk in self._client.astream(messages, **stream_kwargs):
                chunks += 1
                additional_kwargs = getattr(chunk, "additional_kwargs", None)
                if isinstance(additional_kwargs, dict) and additional_kwargs.get("reasoning_content"):
                    reasoning_chunks += 1
                    reasoning_chars += len(str(additional_kwargs["reasoning_content"]))

                content_parts.append(self._chunk_content_text(getattr(chunk, "content", "")))
                metadata = getattr(chunk, "response_metadata", None)
                if isinstance(metadata, dict) and metadata.get("finish_reason"):
                    finish_reason = metadata["finish_reason"]
                chunk_usage = getattr(chunk, "usage_metadata", None)
                if isinstance(chunk_usage, dict):
                    # Usage arrives as cumulative totals, often after the finish-reason chunk.
                    for key in ("input_tokens", "output_tokens", "total_tokens"):
                        if chunk_usage.get(key) is not None:
                            usage[key] = chunk_usage[key]

            response_text = self._strip_reasoning_tags("".join(content_parts))
            completed = True
            return response_text
        finally:
            logger.info(
                "ChatNVIDIA stream summary: request_id=%s attempt=%s completed=%s "
                "thinking_disabled=%s finish_reason=%s input_tokens=%s output_tokens=%s "
                "total_tokens=%s chunks=%s reasoning_chunks=%s reasoning_chars=%s "
                "content_chars=%s answer_chars=%s elapsed_seconds=%.2f",
                request_id,
                attempt,
                completed,
                attempt > 1,
                finish_reason,
                usage.get("input_tokens"),
                usage.get("output_tokens"),
                usage.get("total_tokens"),
                chunks,
                reasoning_chunks,
                reasoning_chars,
                sum(len(part) for part in content_parts),
                len(response_text),
                time.monotonic() - started_at,
            )

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

        request_id = uuid4().hex[:12]
        logger.info(
            "Contextual reply request: request_id=%s force_reply=%s latest_message=%r "
            "replied_to=%r recent_context=%r",
            request_id,
            force_reply,
            cleaned_message,
            replied_to_block,
            history_block,
        )
        max_attempts = 2 if force_reply else 1
        for attempt in range(1, max_attempts + 1):
            try:
                response_text = await self._create_completion(messages, request_id=request_id, attempt=attempt)
                logger.info(
                    "Contextual reply raw model response: request_id=%s attempt=%s response=%r",
                    request_id,
                    attempt,
                    response_text,
                )

                payload = self._extract_json_payload(response_text)
                if payload is None:
                    reply = self._extract_plain_reply(response_text) if force_reply else None
                    failure_reason = "empty_response" if not response_text else "invalid_json"
                else:
                    if not force_reply and payload.get("should_reply") is not True:
                        logger.info(
                            "Contextual reply skipped: request_id=%s model decided not to reply",
                            request_id,
                        )
                        return None
                    reply_value = payload.get("reply")
                    reply = reply_value.strip() if isinstance(reply_value, str) else None
                    failure_reason = "empty_or_invalid_reply"

                if reply:
                    final_reply = reply[: self._max_chat_response_chars].rstrip()
                    logger.info(
                        "Contextual reply final reply: request_id=%s attempt=%s reply=%r",
                        request_id,
                        attempt,
                        final_reply,
                    )
                    return final_reply
            except Exception as exc:
                failure_reason = "completion_error"
                logger.warning(
                    "Contextual reply generation failed: request_id=%s attempt=%s error=%s",
                    request_id,
                    attempt,
                    type(exc).__name__,
                    exc_info=True,
                )

            if attempt < max_attempts:
                logger.warning(
                    "Contextual reply retrying without thinking: request_id=%s attempt=%s reason=%s",
                    request_id,
                    attempt,
                    failure_reason,
                )

        logger.warning(
            "Contextual reply %s: request_id=%s attempts=%s reason=%s",
            "using failure fallback" if force_reply else "skipped",
            request_id,
            max_attempts,
            failure_reason,
        )
        return self._FORCED_REPLY_FALLBACK[: self._max_chat_response_chars].rstrip() if force_reply else None
