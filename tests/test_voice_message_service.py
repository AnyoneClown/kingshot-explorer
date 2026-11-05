import asyncio
from types import SimpleNamespace

import pytest

from services.voice_message_service import RivaTtsBackend, VoiceMessageService


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


def make_service(backend, **kwargs):
    return VoiceMessageService(
        backend=backend,
        default_voice="Magpie-Multilingual.EN-US.Aria",
        default_language_code="en-US",
        **kwargs,
    )


def test_long_text_is_cut_at_a_sentence_boundary():
    backend = FakeBackend()
    service = make_service(backend, max_text_chars=60)
    text = "Rally starts at five. Bring your strongest heroes and full troops for the bear."

    asyncio.run(service.generate_audio(text, filename_stem="reply"))

    assert backend.calls[0]["text"] == "Rally starts at five."


def test_long_text_without_sentences_is_cut_at_a_word_boundary():
    backend = FakeBackend()
    service = make_service(backend, max_text_chars=30)

    asyncio.run(service.generate_audio("alpha bravo charlie delta echo foxtrot golf", filename_stem="reply"))

    spoken = backend.calls[0]["text"]
    assert spoken == "alpha bravo charlie delta echo"
    assert len(spoken) <= 30


def test_discord_markup_is_not_read_aloud():
    backend = FakeBackend()
    service = make_service(backend)

    asyncio.run(
        service.generate_audio(
            "> **Rally** now <@123456789012345678> <:bear:123456789012345678> see https://example.com/x",
            filename_stem="reply",
        )
    )

    assert backend.calls[0]["text"] == "Rally now see"


class FakeRivaModule:
    """Mimics the parts of ``riva.client`` the backend uses."""

    def __init__(self, fail_first=False):
        self.auth_count = 0
        self.closed_channels = 0
        self.fail_first = fail_first
        self.AudioEncoding = SimpleNamespace(LINEAR_PCM="pcm", OGGOPUS="ogg")
        module = self

        class Channel:
            def close(self):
                module.closed_channels += 1

        class Auth:
            def __init__(self, uri, use_ssl, metadata_args):
                module.auth_count += 1
                self.channel = Channel()
                self.metadata = metadata_args

        class SpeechSynthesisService:
            def __init__(self, auth):
                self.auth = auth

            def synthesize(self, **kwargs):
                if module.fail_first:
                    module.fail_first = False
                    raise RuntimeError("UNAVAILABLE")
                return SimpleNamespace(audio=b"pcm")

        self.Auth = Auth
        self.SpeechSynthesisService = SpeechSynthesisService


def make_backend(monkeypatch, module):
    backend = RivaTtsBackend(server="grpc.test:443", use_ssl=True, api_key="test-key", function_id="fn")
    monkeypatch.setattr(RivaTtsBackend, "_riva_client", staticmethod(lambda: module))
    return backend


def synth(backend):
    return backend.synthesize(
        text="hi", language_code="en-US", voice_name="v", sample_rate_hz=22050, audio_encoding="LINEAR_PCM"
    )


def test_riva_backend_reuses_one_channel(monkeypatch):
    module = FakeRivaModule()
    backend = make_backend(monkeypatch, module)

    assert synth(backend) == b"pcm"
    assert synth(backend) == b"pcm"
    assert module.auth_count == 1

    backend.close()
    assert module.closed_channels == 1


def test_riva_backend_reconnects_after_a_failed_call(monkeypatch):
    module = FakeRivaModule(fail_first=True)
    backend = make_backend(monkeypatch, module)

    with pytest.raises(RuntimeError):
        synth(backend)
    assert synth(backend) == b"pcm"
    assert module.auth_count == 2
    assert module.closed_channels == 1


def test_riva_backend_uses_grpc_future_with_deadline(monkeypatch):
    module = FakeRivaModule()
    calls = []

    class Pending:
        def __init__(self):
            self.cancelled = False

        def result(self, timeout=None):
            calls.append(timeout)
            raise TimeoutError("deadline")

        def cancel(self):
            self.cancelled = True

    pending = Pending()

    def synthesize(self, **kwargs):
        assert kwargs["future"] is True
        return pending

    monkeypatch.setattr(module.SpeechSynthesisService, "synthesize", synthesize)
    backend = make_backend(monkeypatch, module)

    try:
        synth(backend)
    except TimeoutError:
        pass
    else:  # pragma: no cover - the stalled call must raise
        raise AssertionError("expected a timeout")

    assert calls == [RivaTtsBackend.SYNTHESIS_TIMEOUT_SECONDS]
    assert pending.cancelled is True
