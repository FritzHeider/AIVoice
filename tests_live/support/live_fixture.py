"""The cached STT fixture (specs/30 §7 Decision P03-T16 rule 4): one phrase synthesized by ElevenLabs TTS the
first time a live run finds it missing, stored as a PCM16 mono 8 kHz WAV plus a JSON record whose sha256 is
checked on every run."""

from __future__ import annotations

import hashlib
import io
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Protocol

import numpy as np
import orjson

from va_sim.audio.codec import ulaw_encode

__all__ = [
    "FIXTURE_DIR",
    "META_NAME",
    "SAMPLE_RATE",
    "STT_PHRASE",
    "WAV_NAME",
    "SttFixture",
    "Synthesizer",
    "ensure_stt_fixture",
    "load_fixture",
    "save_fixture",
    "wav_bytes",
]

STT_PHRASE: Final = (
    "Please confirm your appointment with doctor Lee on Tuesday morning at half past ten, thank you."
)
SAMPLE_RATE: Final = 8000
TTS_FORMAT: Final = "pcm_8000"
WAV_NAME: Final = "stt_phrase.wav"
META_NAME: Final = "stt_phrase.json"
FIXTURE_DIR: Final = Path(__file__).resolve().parent.parent / "fixtures"


class _Audio(Protocol):
    @property
    def audio(self) -> bytes: ...


class Synthesizer(Protocol):
    """The part of :class:`va_elevenlabs.ElevenLabsRest` the fixture needs."""

    async def tts_convert(self, voice_id: str, text: str, *, model_id: str, output_format: str) -> _Audio: ...


@dataclass(frozen=True, slots=True)
class SttFixture:
    text: str
    pcm16: bytes = field(repr=False)

    @property
    def wav(self) -> bytes:
        return wav_bytes(self.pcm16)

    @property
    def ulaw(self) -> bytes:
        return ulaw_encode(np.frombuffer(self.pcm16, dtype="<i2"))


def wav_bytes(pcm16: bytes) -> bytes:
    """A PCM16 mono 8 kHz WAV of little-endian samples."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm16)
    return buffer.getvalue()


def _samples(wav: bytes) -> bytes:
    with wave.open(io.BytesIO(wav), "rb") as source:
        shape = (source.getnchannels(), source.getsampwidth(), source.getframerate())
        if shape != (1, 2, SAMPLE_RATE):
            msg = f"stt fixture must be PCM16 mono {SAMPLE_RATE} Hz, got channels/width/rate {shape}"
            raise AssertionError(msg)
        return source.readframes(source.getnframes())


def save_fixture(directory: Path, *, text: str, voice_id: str, model_id: str, pcm16: bytes) -> SttFixture:
    """Write the WAV and its JSON record (text, voice, model, sha256 of the WAV file)."""
    samples = pcm16[: len(pcm16) - len(pcm16) % 2]
    wav = wav_bytes(samples)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / WAV_NAME).write_bytes(wav)
    meta = {
        "text": text,
        "voice_id": voice_id,
        "model_id": model_id,
        "sha256": hashlib.sha256(wav).hexdigest(),
    }
    (directory / META_NAME).write_bytes(
        orjson.dumps(meta, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS) + b"\n"
    )
    return SttFixture(text, samples)


def load_fixture(directory: Path) -> SttFixture | None:
    """The stored fixture, ``None`` when neither file exists; ``AssertionError`` when only one exists or the
    WAV does not match the recorded sha256 (never regenerated silently)."""
    wav_path, meta_path = directory / WAV_NAME, directory / META_NAME
    present = (wav_path.exists(), meta_path.exists())
    if present == (False, False):
        return None
    if present != (True, True):
        msg = f"stt fixture is incomplete in {directory}: commit or delete both {WAV_NAME} and {META_NAME}"
        raise AssertionError(msg)
    wav = wav_path.read_bytes()
    meta = orjson.loads(meta_path.read_bytes())
    if hashlib.sha256(wav).hexdigest() != meta["sha256"]:
        msg = f"stt fixture sha256 mismatch for {wav_path}"
        raise AssertionError(msg)
    return SttFixture(str(meta["text"]), _samples(wav))


async def ensure_stt_fixture(
    rest: Synthesizer, *, voice_id: str, model_id: str, directory: Path = FIXTURE_DIR
) -> SttFixture:
    """The stored fixture, or a new one synthesized from :data:`STT_PHRASE` and saved."""
    stored = load_fixture(directory)
    if stored is not None:
        return stored
    audio = await rest.tts_convert(voice_id, STT_PHRASE, model_id=model_id, output_format=TTS_FORMAT)
    return save_fixture(directory, text=STT_PHRASE, voice_id=voice_id, model_id=model_id, pcm16=audio.audio)
