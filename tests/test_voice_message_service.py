import asyncio

from services.voice_message_service import VoiceMessageService


class FakeBackend:
    def __init__(self, audio_bytes=b"audio-bytes"):
        self.audio_bytes = audio_bytes
        self.calls = []

    async def synthesize(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.audio_bytes, list):
            return self.audio_bytes.pop(0)
        return self.audio_bytes


def test_generate_audio_uses_language_voice_mapping_and_ogg_extension():
    backend = FakeBackend()
    service = VoiceMessageService(
        backend=backend,
        default_voice="Magpie-Multilingual.EN-US.Aria",
        default_language_code="en-US",
        audio_encoding="OGGOPUS",
    )

    audio = asyncio.run(
        service.generate_audio(
            "Hola equipo",
            language_hint="Spanish",
            filename_stem="translation",
        )
    )

    assert audio.filename == "translation.ogg"
    assert audio.content_type == "audio/ogg"
    assert audio.data == b"audio-bytes"
    assert backend.calls == [
        {
            "text": "Hola equipo",
            "language_code": "es-US",
            "voice_name": "Magpie-Multilingual.ES-US.Aria",
            "sample_rate_hz": 44100,
            "audio_encoding": "OGGOPUS",
        }
    ]


def test_generate_audio_truncates_text_to_configured_limit():
    backend = FakeBackend()
    service = VoiceMessageService(
        backend=backend,
        default_voice="Magpie-Multilingual.EN-US.Aria",
        default_language_code="en-US",
        max_text_chars=10,
    )

    asyncio.run(service.generate_audio("1234567890ABCDEFGHIJ", filename_stem="reply"))

    assert backend.calls[0]["text"] == "1234567890"


def test_generate_audio_returns_none_for_blank_text():
    backend = FakeBackend()
    service = VoiceMessageService(
        backend=backend,
        default_voice="Magpie-Multilingual.EN-US.Aria",
        default_language_code="en-US",
    )

    audio = asyncio.run(service.generate_audio("   ", filename_stem="reply"))

    assert audio is None
    assert backend.calls == []


def test_generate_audio_falls_back_to_linear_pcm_when_primary_encoding_returns_empty():
    backend = FakeBackend(audio_bytes=[b"", b"\x01\x02\x03\x04"])
    service = VoiceMessageService(
        backend=backend,
        default_voice="Magpie-Multilingual.EN-US.Aria",
        default_language_code="en-US",
        audio_encoding="OGGOPUS",
        sample_rate_hz=22050,
    )

    audio = asyncio.run(service.generate_audio("Hello there", filename_stem="reply"))

    assert audio is not None
    assert audio.filename == "reply.wav"
    assert audio.content_type == "audio/wav"
    assert audio.data.startswith(b"RIFF")
    assert backend.calls == [
        {
            "text": "Hello there",
            "language_code": "en-US",
            "voice_name": "Magpie-Multilingual.EN-US.Aria",
            "sample_rate_hz": 22050,
            "audio_encoding": "OGGOPUS",
        },
        {
            "text": "Hello there",
            "language_code": "en-US",
            "voice_name": "Magpie-Multilingual.EN-US.Aria",
            "sample_rate_hz": 22050,
            "audio_encoding": "LINEAR_PCM",
        },
    ]
