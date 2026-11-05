import asyncio
import inspect
import io
import logging
import re
import threading
import wave
from dataclasses import dataclass
from typing import Any

from services.text_utils import truncate_cleanly

logger = logging.getLogger(__name__)

# Mentions, custom emoji, timestamps and links are noise when spoken.
_SPOKEN_MARKUP = re.compile(
    r"<a?:\w+:\d+>|<@[!&]?\d+>|<#\d+>|</[\w\- ]+:\d+>|<t:-?\d+(?::\w)?>|<?https?://[^\s>]+>?"
)
_MARKDOWN_CHARS = re.compile(r"(?m)^\s*>+\s?|[*_~`|]")


@dataclass
class VoiceMessageAudio:
    """In-memory audio payload ready for Discord upload."""

    filename: str
    data: bytes
    content_type: str


class RivaTtsBackend:
    """Thin adapter around NVIDIA's Riva Python client.

    The gRPC channel (and its auth metadata) is created once and reused; gRPC channels
    are thread-safe and reconnect on their own. A failed call drops the cached channel
    so the next call starts from a fresh connection.
    """

    def __init__(
        self,
        *,
        server: str,
        use_ssl: bool,
        api_key: str,
        function_id: str,
    ):
        self._server = server
        self._use_ssl = use_ssl
        self._api_key = api_key
        self._function_id = function_id
        self._lock = threading.Lock()
        self._service = None

    SYNTHESIS_TIMEOUT_SECONDS = 20.0

    @staticmethod
    def _riva_client():
        try:
            import riva.client
        except ImportError as exc:
            raise RuntimeError(
                "nvidia-riva-client is not installed. Run `uv sync`."
            ) from exc
        return riva.client

    def _get_service(self):
        with self._lock:
            if self._service is None:
                riva_client = self._riva_client()
                metadata_args = [
                    ("function-id", self._function_id),
                    ("authorization", f"Bearer {self._api_key}"),
                ]
                auth = riva_client.Auth(uri=self._server, use_ssl=self._use_ssl, metadata_args=metadata_args)
                self._service = riva_client.SpeechSynthesisService(auth)
            return self._service

    def _drop_service(self, service) -> None:
        with self._lock:
            if self._service is not service:
                return
            self._service = None
        self._close_service(service)

    @staticmethod
    def _close_service(service) -> None:
        channel = getattr(getattr(service, "auth", None), "channel", None)
        close = getattr(channel, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.debug("Closing the TTS gRPC channel failed", exc_info=True)

    def synthesize(
        self,
        *,
        text: str,
        language_code: str,
        voice_name: str,
        sample_rate_hz: int,
        audio_encoding: str,
    ) -> bytes:
        riva_client = self._riva_client()
        service = self._get_service()
        encoding = getattr(riva_client.AudioEncoding, audio_encoding)
        try:
            # Riva's blocking call has no deadline; use the gRPC future so a stalled
            # endpoint cannot park a worker thread forever.
            pending = service.synthesize(
                text=text,
                language_code=language_code,
                sample_rate_hz=sample_rate_hz,
                encoding=encoding,
                voice_name=voice_name,
                future=True,
            )
            if hasattr(pending, "result"):
                try:
                    response = pending.result(timeout=self.SYNTHESIS_TIMEOUT_SECONDS)
                except Exception:
                    cancel = getattr(pending, "cancel", None)
                    if cancel is not None:
                        cancel()
                    raise
            else:
                response = pending
        except Exception:
            self._drop_service(service)
            raise
        return response.audio

    def close(self) -> None:
        with self._lock:
            service, self._service = self._service, None
        if service is not None:
            self._close_service(service)


class VoiceMessageService:
    """Generate audio attachments for translated and conversational bot replies."""

    _DEFAULT_VOICES = {
        "en-US": "Magpie-Multilingual.EN-US.Aria",
        "es-US": "Magpie-Multilingual.ES-US.Aria",
        "fr-FR": "Magpie-Multilingual.FR-FR.Aria",
        "de-DE": "Magpie-Multilingual.DE-DE.Aria",
        "it-IT": "Magpie-Multilingual.IT-IT.Aria",
        "vi-VN": "Magpie-Multilingual.VI-VN.Aria",
        "zh-CN": "Magpie-Multilingual.ZH-CN.Aria",
        "hi-IN": "Magpie-Multilingual.HI-IN.Aria",
        "ja-JP": "Magpie-Multilingual.JA-JP.Aria",
    }
    _LANGUAGE_ALIASES = {
        "english": "en-US",
        "en": "en-US",
        "en-us": "en-US",
        "spanish": "es-US",
        "es": "es-US",
        "es-us": "es-US",
        "french": "fr-FR",
        "fr": "fr-FR",
        "fr-fr": "fr-FR",
        "german": "de-DE",
        "de": "de-DE",
        "de-de": "de-DE",
        "italian": "it-IT",
        "it": "it-IT",
        "it-it": "it-IT",
        "vietnamese": "vi-VN",
        "vi": "vi-VN",
        "vi-vn": "vi-VN",
        "mandarin": "zh-CN",
        "mandarin chinese": "zh-CN",
        "chinese": "zh-CN",
        "zh": "zh-CN",
        "zh-cn": "zh-CN",
        "hindi": "hi-IN",
        "hi": "hi-IN",
        "hi-in": "hi-IN",
        "japanese": "ja-JP",
        "ja": "ja-JP",
        "ja-jp": "ja-JP",
    }

    def __init__(
        self,
        *,
        backend: Any,
        default_voice: str,
        default_language_code: str,
        audio_encoding: str = "OGGOPUS",
        sample_rate_hz: int = 44100,
        max_text_chars: int = 350,
    ):
        self._backend = backend
        self._default_voice = default_voice
        self._default_language_code = default_language_code
        self._audio_encoding = audio_encoding.upper()
        self._sample_rate_hz = sample_rate_hz
        self._max_text_chars = max_text_chars
        # Synthesis runs in the default thread pool, which is shared with image
        # rendering and DNS lookups: keep only a couple of calls in flight.
        self._synthesis_slots: asyncio.Semaphore | None = None

    @classmethod
    def from_nvidia(
        cls,
        *,
        api_key: str,
        server: str,
        use_ssl: bool,
        function_id: str,
        default_voice: str,
        default_language_code: str,
        audio_encoding: str = "OGGOPUS",
        sample_rate_hz: int = 44100,
        max_text_chars: int = 350,
    ) -> "VoiceMessageService":
        return cls(
            backend=RivaTtsBackend(
                server=server,
                use_ssl=use_ssl,
                api_key=api_key,
                function_id=function_id,
            ),
            default_voice=default_voice,
            default_language_code=default_language_code,
            audio_encoding=audio_encoding,
            sample_rate_hz=sample_rate_hz,
            max_text_chars=max_text_chars,
        )

    async def generate_audio(
        self,
        text: str,
        *,
        language_hint: str | None = None,
        filename_stem: str = "reply",
    ) -> VoiceMessageAudio | None:
        cleaned = self._clean_text(text)
        if not cleaned:
            return None

        language_code = self._resolve_language_code(language_hint)
        voice_name = self._resolve_voice_name(language_code)
        audio_bytes = await self._synthesize(
            text=cleaned,
            language_code=language_code,
            voice_name=voice_name,
            sample_rate_hz=self._sample_rate_hz,
            audio_encoding=self._audio_encoding,
        )
        selected_encoding = self._audio_encoding

        if not audio_bytes and self._audio_encoding != "LINEAR_PCM":
            logger.warning(
                "TTS returned 0 bytes for encoding %s, retrying with LINEAR_PCM",
                self._audio_encoding,
            )
            audio_bytes = await self._synthesize(
                text=cleaned,
                language_code=language_code,
                voice_name=voice_name,
                sample_rate_hz=self._sample_rate_hz,
                audio_encoding="LINEAR_PCM",
            )
            selected_encoding = "LINEAR_PCM"

        if not audio_bytes:
            logger.error("TTS returned 0 bytes after synthesis for language=%s voice=%s", language_code, voice_name)
            return None

        logger.info(
            "Generated TTS audio bytes=%s language=%s voice=%s encoding=%s",
            len(audio_bytes),
            language_code,
            voice_name,
            selected_encoding,
        )

        if selected_encoding == "LINEAR_PCM":
            return VoiceMessageAudio(
                filename=f"{filename_stem}.wav",
                data=self._wrap_pcm_as_wav(audio_bytes),
                content_type="audio/wav",
            )

        return VoiceMessageAudio(
            filename=f"{filename_stem}.ogg",
            data=audio_bytes,
            content_type="audio/ogg",
        )

    def _clean_text(self, text: str) -> str:
        """Drop Discord markup that should not be read aloud and cut at a natural boundary."""
        cleaned = _SPOKEN_MARKUP.sub(" ", text or "")
        cleaned = _MARKDOWN_CHARS.sub("", cleaned)
        cleaned = " ".join(cleaned.split()).strip()
        # Ending on a full sentence sounds far better than stopping mid-phrase.
        return truncate_cleanly(cleaned, self._max_text_chars, ellipsis="", sentence_min_ratio=0.25)

    async def close(self) -> None:
        """Release the TTS connection (safe to call more than once)."""
        close = getattr(self._backend, "close", None)
        if not callable(close):
            return
        result = await asyncio.to_thread(close)
        if inspect.isawaitable(result):
            await result

    def _resolve_language_code(self, language_hint: str | None) -> str:
        if not language_hint:
            return self._default_language_code

        normalized = language_hint.strip().lower()
        if normalized in self._LANGUAGE_ALIASES:
            return self._LANGUAGE_ALIASES[normalized]

        logger.warning("Unsupported TTS language hint %r, falling back to %s", language_hint, self._default_language_code)
        return self._default_language_code

    def _resolve_voice_name(self, language_code: str) -> str:
        if language_code == self._default_language_code:
            return self._default_voice
        return self._DEFAULT_VOICES.get(language_code, self._default_voice)

    MAX_CONCURRENT_SYNTHESES = 2
    SYNTHESIS_DEADLINE_SECONDS = 25.0

    async def _synthesize(self, **kwargs) -> bytes:
        if self._synthesis_slots is None:
            self._synthesis_slots = asyncio.Semaphore(self.MAX_CONCURRENT_SYNTHESES)
        async with self._synthesis_slots:
            return await asyncio.wait_for(self._call_backend(**kwargs), timeout=self.SYNTHESIS_DEADLINE_SECONDS)

    async def _call_backend(self, **kwargs) -> bytes:
        synthesize = self._backend.synthesize
        if inspect.iscoroutinefunction(synthesize):
            return await synthesize(**kwargs)

        result = await asyncio.to_thread(synthesize, **kwargs)
        if inspect.isawaitable(result):
            return await result
        return result

    def _wrap_pcm_as_wav(self, pcm_bytes: bytes) -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self._sample_rate_hz)
            wav_file.writeframes(pcm_bytes)
        return buffer.getvalue()
