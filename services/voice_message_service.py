import asyncio
import inspect
import io
import logging
import wave
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class VoiceMessageAudio:
    """In-memory audio payload ready for Discord upload."""

    filename: str
    data: bytes
    content_type: str


class RivaTtsBackend:
    """Thin adapter around NVIDIA's Riva Python client."""

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

    def synthesize(
        self,
        *,
        text: str,
        language_code: str,
        voice_name: str,
        sample_rate_hz: int,
        audio_encoding: str,
    ) -> bytes:
        try:
            import riva.client
        except ImportError as exc:
            raise RuntimeError(
                "nvidia-riva-client is not installed. Run `uv sync`."
            ) from exc

        metadata_args = [
            ("function-id", self._function_id),
            ("authorization", f"Bearer {self._api_key}"),
        ]
        auth = riva.client.Auth(uri=self._server, use_ssl=self._use_ssl, metadata_args=metadata_args)
        service = riva.client.SpeechSynthesisService(auth)
        encoding = getattr(riva.client.AudioEncoding, audio_encoding)
        response = service.synthesize(
            text=text,
            language_code=language_code,
            sample_rate_hz=sample_rate_hz,
            encoding=encoding,
            voice_name=voice_name,
        )
        return response.audio


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
        cleaned = " ".join((text or "").split()).strip()
        return cleaned[: self._max_text_chars]

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

    async def _synthesize(self, **kwargs) -> bytes:
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
