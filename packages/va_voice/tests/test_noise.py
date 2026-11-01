"""SpectralGate on the VAD path (specs/07-media-pipeline.md §10.1, §22).

The recall test needs ``models/silero_vad.onnx`` (``make setup``; CI fetches it before ``make check``).
"""

from __future__ import annotations

import math
from collections.abc import AsyncIterator
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio
from numpy.typing import NDArray
from vad_fixtures import SPEECH_SEGMENTS_MS, read_fixture

from va_voice.audio import rms_dbfs
from va_voice.noise import (
    HOP,
    LATENCY_MS,
    LATENCY_SAMPLES,
    MIN_PROFILE_FRAMES,
    PERCENTILE_TO_MEAN,
    PROFILE_FRAMES,
    SpectralGate,
)
from va_voice.vad import SileroVad, VadExecutor
from va_voice.vad_model import SileroModel

MODELS_DIR = Path(__file__).resolve().parents[3] / "models"
RATE = 16_000
CHUNK = 320


def _noise(seconds: float, dbfs: float, seed: int = 3) -> NDArray[np.int16]:
    rng = np.random.default_rng(seed)
    values = rng.normal(0.0, 32_767 * 10 ** (dbfs / 20), int(seconds * RATE))
    return np.clip(np.rint(values), -32_768, 32_767).astype(np.int16)


def _gate(gate: SpectralGate, pcm: NDArray[np.int16], chunk: int = CHUNK) -> NDArray[np.int16]:
    parts = [gate.process(pcm[i : i + chunk]) for i in range(0, pcm.size, chunk)]
    return np.concatenate(parts)


def test_constants() -> None:
    assert (LATENCY_SAMPLES, LATENCY_MS, PROFILE_FRAMES) == (256, 16, 94)
    assert pytest.approx(1.876, abs=1e-3) == PERCENTILE_TO_MEAN


def test_without_a_profile_the_output_is_the_input_delayed_by_one_hop() -> None:
    pcm = _noise(1.0, -10.0)
    gate = SpectralGate()
    gate.set_speech(True)
    out = _gate(gate, pcm)
    assert out.size == (pcm.size // HOP) * HOP
    assert not out[:LATENCY_SAMPLES].any()
    assert np.array_equal(out[LATENCY_SAMPLES:], pcm[: out.size - LATENCY_SAMPLES])
    assert not gate.profile_ready
    assert not gate.noise_profile.any()


@pytest.mark.parametrize("chunks", [[1, 255, 256, 257, 3000], [512] * 7, [100] * 40, [7999, 1]])
def test_output_does_not_depend_on_chunking(chunks: list[int]) -> None:
    pcm = _noise(sum(chunks) / RATE, -25.0)
    reference = SpectralGate().process(pcm)
    gate = SpectralGate()
    parts, start = [], 0
    for size in chunks:
        parts.append(gate.process(pcm[start : start + size]))
        start += size
    out = np.concatenate(parts)
    assert out.size == reference.size == (pcm.size // HOP) * HOP
    assert np.array_equal(out, reference)


def test_stationary_noise_is_reduced_by_at_least_10_db() -> None:
    pcm = _noise(4.0, -30.0)
    out = _gate(SpectralGate(), pcm)
    settled = int(1.5 * RATE)
    before = rms_dbfs(pcm[settled - LATENCY_SAMPLES : out.size - LATENCY_SAMPLES])
    after = rms_dbfs(out[settled:])
    assert before == pytest.approx(-30.0, abs=0.1)
    assert before - after >= 10.0


def test_gating_starts_after_the_minimum_profile() -> None:
    gate = SpectralGate()
    pcm = _noise(1.0, -30.0)
    out = gate.process(pcm[: (MIN_PROFILE_FRAMES - 1) * HOP])
    assert not gate.profile_ready
    assert out.size == (MIN_PROFILE_FRAMES - 1) * HOP
    gate.process(pcm[(MIN_PROFILE_FRAMES - 1) * HOP : MIN_PROFILE_FRAMES * HOP])
    assert gate.profile_ready


def test_the_profile_learns_only_during_non_speech() -> None:
    gate = SpectralGate()
    _gate(gate, _noise(1.0, -40.0))
    learned = gate.noise_profile
    gate.set_speech(True)
    t = np.arange(RATE) / RATE
    tone = np.rint(16_000 * np.sin(2 * np.pi * 1000 * t)).astype(np.int16)
    out = _gate(gate, tone)
    assert np.array_equal(gate.noise_profile, learned)
    assert rms_dbfs(out[RATE // 2 :]) == pytest.approx(rms_dbfs(tone), abs=0.5)
    gate.set_speech(False)
    _gate(gate, np.tile(tone, 2))
    assert gate.noise_profile.max() > 10 * learned.max()


def test_reset_forgets_everything() -> None:
    gate = SpectralGate()
    _gate(gate, _noise(1.0, -30.0))
    gate.process(np.zeros(100, dtype=np.int16))
    gate.reset()
    assert not gate.profile_ready
    pcm = _noise(0.2, -30.0, seed=9)
    assert np.array_equal(gate.process(pcm), SpectralGate().process(pcm))


def test_rejects_other_sample_types() -> None:
    with pytest.raises(ValueError, match="expected 1-D int16 samples"):
        SpectralGate().process(np.zeros(10, dtype=np.float32))
    with pytest.raises(ValueError, match="expected 1-D int16 samples"):
        SpectralGate().process(np.zeros((2, 5), dtype=np.int16))


def test_clips_to_int16() -> None:
    gate = SpectralGate()
    gate.set_speech(True)
    loud = np.full(4096, 32_767, dtype=np.int16)
    out = gate.process(loud)
    assert out.dtype == np.int16
    assert out[LATENCY_SAMPLES:].min() == 32_767


@pytest_asyncio.fixture
async def executor() -> AsyncIterator[VadExecutor]:
    async with VadExecutor(2) as pool:
        yield pool


def _covered_ms(segments: list[tuple[float, float]], start: float, end: float) -> float:
    return sum(max(0.0, min(end, b) - max(start, a)) for a, b in segments)


async def test_gated_vad_keeps_speech_recall_on_noisy_speech(executor: VadExecutor) -> None:
    speech = read_fixture("speech.wav")
    noisy = np.clip(speech.astype(np.int32) + _noise(speech.size / RATE, -30.0), -32_768, 32_767).astype(
        np.int16
    )
    vad = SileroVad(SileroModel.load(MODELS_DIR), executor)
    gate = SpectralGate()
    detected: list[tuple[float, float]] = []
    opened: float | None = None
    for start in range(0, noisy.size, CHUNK):
        for event in await vad.feed(gate.process(noisy[start : start + CHUNK])):
            if event.kind == "speech_start":
                opened = event.at_ms - LATENCY_MS
                gate.set_speech(True)
            elif event.kind == "speech_end":
                assert opened is not None
                detected.append((opened, event.at_ms - LATENCY_MS))
                opened = None
                gate.set_speech(False)
    if opened is not None:
        detected.append((opened, math.inf))
    truth = sum(end - start for start, end in SPEECH_SEGMENTS_MS)
    covered = sum(_covered_ms(detected, start, end) for start, end in SPEECH_SEGMENTS_MS)
    assert gate.profile_ready
    assert len(detected) == len(SPEECH_SEGMENTS_MS)
    assert covered / truth >= 0.95
