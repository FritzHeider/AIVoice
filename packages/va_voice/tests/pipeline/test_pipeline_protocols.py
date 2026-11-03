"""Vendor clients and the Silero VAD satisfy the pipeline protocols as they are (mypy checks it)."""

from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from va_common.time import FrozenClock
from va_elevenlabs import (
    ChunkAlignment,
    ElevenLabsRealtimeStt,
    SttConnection,
    SttEvent,
    TtsAudio,
    TtsConfig,
    TtsSocket,
)
from va_elevenlabs.stt_models import SttWord
from va_voice.pipeline import SttEventLike, SttStream, TtsChunkLike, TtsStream, VoiceActivityDetector
from va_voice.vad import SileroVad, VadExecutor

URL = "ws://127.0.0.1:9"


class _SilentModel:
    def infer_batch(
        self, samples: NDArray[np.float32], state: NDArray[np.float32]
    ) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
        return np.zeros(samples.shape[0], dtype=np.float32), state


def test_elevenlabs_streams_are_pipeline_streams() -> None:
    clock = FrozenClock()
    tts: TtsStream = TtsSocket(TtsConfig(URL, "k"), clock=clock)
    stt: SttStream = ElevenLabsRealtimeStt(SttConnection(URL, "k"), clock=clock)
    assert callable(tts.audio)
    assert callable(stt.events)


def test_elevenlabs_events_are_pipeline_events() -> None:
    word = SttWord(text="hi", start_s=0.0, end_s=0.2)
    event: SttEventLike = SttEvent(
        kind="committed", text="hi", words=[word], language_code="en", received_at=1.0
    )
    alignment = ChunkAlignment(chars=("h",), char_start_ms=(0,), char_duration_ms=(10,))
    chunk: TtsChunkLike = TtsAudio(context_id="turn-1", audio=b"\xff" * 160, alignment=alignment)
    assert (event.kind, [w.text for w in event.words], event.is_update) == ("committed", ["hi"], False)
    assert (chunk.context_id, chunk.is_final, chunk.alignment) == ("turn-1", False, alignment)


async def test_silero_vad_is_a_pipeline_vad() -> None:
    async with VadExecutor(1) as executor:
        vad: VoiceActivityDetector = SileroVad(_SilentModel(), executor)
        events = await vad.feed(np.zeros(512, dtype=np.int16))
    assert [event.kind for event in events] == ["probability"]
