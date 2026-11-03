"""GatedVad: noise_cancellation on the VAD path (specs/07-media-pipeline.md §10.1, Decision P05-T15).

The real-model test needs ``models/silero_vad.onnx`` (``make setup``; CI fetches it before ``make check``).
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

from va_voice.audio import L16_16000, pcm_to_l16
from va_voice.noise import HOP, LATENCY_MS, LATENCY_SAMPLES, MIN_PROFILE_FRAMES, SpectralGate
from va_voice.pipeline import GatedVad, InboundAudio, InboundPath, PipelineConfig
from va_voice.vad import SileroVad, VadEvent, VadEventKind, VadExecutor
from va_voice.vad_model import SileroModel

MODELS_DIR = Path(__file__).resolve().parents[3] / "models"
RATE = 16_000
CHUNK = 320


class ScriptedVad:
    """Records what it is fed; returns the head of ``script`` on every non-empty feed."""

    def __init__(self, *script: list[VadEvent]) -> None:
        self.fed: list[NDArray[np.int16]] = []
        self._script = list(script)

    async def feed(self, pcm16k: NDArray[np.int16]) -> list[VadEvent]:
        self.fed.append(pcm16k.copy())
        return self._script.pop(0) if self._script else []

    @property
    def total(self) -> int:
        return sum(part.size for part in self.fed)


class NullStt:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_audio(self, audio: bytes) -> None:
        self.sent.append(audio)


def _event(kind: VadEventKind, at_ms: float) -> VadEvent:
    return VadEvent(kind=kind, at_ms=at_ms, probability=1.0, speech=kind == "speech_start")


def _noise(seconds: float, dbfs: float, seed: int = 11) -> NDArray[np.int16]:
    scale = 32_767 * 10 ** (dbfs / 20)
    white = np.random.default_rng(seed).standard_normal(round(seconds * RATE)) * scale
    return white.round().clip(-32_768, 32_767).astype(np.int16)


async def _feed(vad: GatedVad, pcm: NDArray[np.int16]) -> list[VadEvent]:
    events: list[VadEvent] = []
    for start in range(0, pcm.size, CHUNK):
        events.extend(await vad.feed(pcm[start : start + CHUNK]))
    return events


async def test_the_vad_hears_the_gated_stream_one_hop_late() -> None:
    inner = ScriptedVad()
    gated = GatedVad(inner)
    pcm = _noise(0.5, -20.0)
    await _feed(gated, pcm)
    assert inner.total == (pcm.size // HOP) * HOP
    assert not np.concatenate(inner.fed)[:LATENCY_SAMPLES].any()
    reference = SpectralGate()
    expected = np.concatenate([reference.process(pcm[i : i + CHUNK]) for i in range(0, pcm.size, CHUNK)])
    assert np.array_equal(np.concatenate(inner.fed), expected)


async def test_an_empty_gate_output_never_reaches_the_vad() -> None:
    inner = ScriptedVad([_event("speech_start", 0.0)])
    gated = GatedVad(inner)
    assert await gated.feed(np.zeros(100, dtype=np.int16)) == []
    assert inner.fed == []


async def test_event_times_move_back_by_the_gate_latency_and_clamp_at_zero() -> None:
    script = [_event("speech_start", 10.0), _event("probability", 40.0)], [_event("speech_end", 500.0)]
    gated = GatedVad(ScriptedVad(*script))
    first = await gated.feed(np.zeros(CHUNK * 2, dtype=np.int16))
    second = await gated.feed(np.zeros(CHUNK, dtype=np.int16))
    assert [(e.kind, e.at_ms) for e in first] == [("speech_start", 0.0), ("probability", 40.0 - LATENCY_MS)]
    assert [(e.kind, e.at_ms) for e in second] == [("speech_end", 500.0 - LATENCY_MS)]
    assert first[0].probability == 1.0
    assert first[0].speech


async def test_the_profile_learns_only_between_speech_start_and_end() -> None:
    noise = _noise(2.0, -30.0)
    starts = ScriptedVad([_event("speech_start", 0.0)])
    gate = SpectralGate()
    gated = GatedVad(starts, gate)
    assert gated.gate is gate
    await _feed(gated, noise)
    assert not gate.profile_ready  # speech from the first gated feed on: nothing learnt
    ends = ScriptedVad([_event("speech_start", 0.0)], [_event("speech_end", 20.0)])
    learning = GatedVad(ends)
    await _feed(learning, noise)
    assert learning.gate.profile_ready
    probabilities = ScriptedVad(*([[_event("probability", 0.0)]] * 200))
    untouched = GatedVad(probabilities)
    await _feed(untouched, noise[: (MIN_PROFILE_FRAMES + 2) * 256])
    assert untouched.gate.profile_ready


async def test_window_batching_feeds_once_per_vad_window_with_identical_results() -> None:
    noise = _noise(2.0, -30.0)
    script = [[_event("probability", 0.0)]] * 30 + [[_event("speech_start", 10.0)]] + [[]] * 20
    script += [[_event("speech_end", 900.0)]] + [[_event("probability", 0.0)]] * 100
    every_feed, per_window = ScriptedVad(), ScriptedVad()
    plain = GatedVad(_WindowVad(every_feed, *script))
    batched = GatedVad(_WindowVad(per_window, *script), window_samples=512)
    plain_events = await _feed(plain, noise)
    batched_events = await _feed(batched, noise)
    assert batched_events == plain_events
    assert np.array_equal(np.concatenate(per_window.fed), np.concatenate(every_feed.fed))
    assert batched.gate.profile_ready
    assert np.array_equal(batched.gate.noise_profile, plain.gate.noise_profile)
    # One gate run per completed 512-sample window instead of one per gate-releasing 20 ms chunk.
    assert len(per_window.fed) == noise.size // 512
    assert len(every_feed.fed) > len(per_window.fed)


class _WindowVad:
    """Records into ``recorder``; emits the next scripted events for every 512-sample window completed."""

    def __init__(self, recorder: ScriptedVad, *script: list[VadEvent]) -> None:
        self._recorder = recorder
        self._script = list(script)
        self._samples = 0

    async def feed(self, pcm16k: NDArray[np.int16]) -> list[VadEvent]:
        await self._recorder.feed(pcm16k)
        before, self._samples = self._samples, self._samples + pcm16k.size
        events: list[VadEvent] = []
        for _ in range(self._samples // 512 - before // 512):
            events.extend(self._script.pop(0) if self._script else [])
        return events


@pytest.mark.parametrize("window", [0, 100, 300])
def test_window_samples_must_be_a_multiple_of_the_hop(window: int) -> None:
    with pytest.raises(ValueError, match="multiple of 256"):
        GatedVad(ScriptedVad(), window_samples=window)


async def test_other_sample_types_are_rejected_before_buffering() -> None:
    gated = GatedVad(ScriptedVad(), window_samples=512)
    with pytest.raises(ValueError, match="1-D int16"):
        await gated.feed(np.zeros(10, dtype=np.float32))


def _l16_frames(pcm: NDArray[np.int16]) -> list[InboundAudio]:
    samples = L16_16000.frame_samples
    return [
        InboundAudio(chunk=index + 1, payload=pcm_to_l16(pcm[start : start + samples], "big"))
        for index, start in enumerate(range(0, pcm.size, samples))
    ]


async def _run_inbound(config: PipelineConfig, vad: ScriptedVad, pcm: NDArray[np.int16]) -> list[VadEvent]:
    path = InboundPath(config, stt=NullStt(), vad=vad, now_ms=lambda: 1000.0)  # type: ignore[arg-type]
    events: list[VadEvent] = []
    for frame in _l16_frames(pcm):
        events.extend(await path.push(frame))
    events.extend(await path.drain())
    return events


async def test_inbound_path_gates_the_vad_with_noise_cancellation() -> None:
    pcm = _noise(0.4, -25.0)
    vad = ScriptedVad([_event("speech_start", 100.0)])
    config = PipelineConfig(fmt=L16_16000, noise_cancellation=True)
    events = await _run_inbound(config, vad, pcm)
    # speech from the first feed on: the gate never learns a profile, so the VAD hears the delayed input
    delayed = np.concatenate([np.zeros(LATENCY_SAMPLES, dtype=np.int16), pcm])[: (pcm.size // HOP) * HOP]
    assert np.array_equal(np.concatenate(vad.fed), delayed)
    # Every frame "arrives" at t = 1000, so the origin follows the frames read: the first VAD feed happens on
    # the second frame (its gated output completes the first 512-sample window), origin 1000 - 2 x 20.
    origin = 1000.0 - 2 * 20
    assert [(e.kind, e.at_ms) for e in events] == [("speech_start", origin + 100.0 - LATENCY_MS)]


async def test_inbound_path_feeds_raw_samples_without_noise_cancellation() -> None:
    pcm = _noise(0.4, -25.0)
    vad = ScriptedVad([_event("speech_start", 100.0)])
    assert PipelineConfig().noise_cancellation is False
    events = await _run_inbound(PipelineConfig(fmt=L16_16000), vad, pcm)
    assert np.array_equal(np.concatenate(vad.fed), pcm)
    assert [(e.kind, e.at_ms) for e in events] == [("speech_start", 980.0 + 100.0)]


@pytest_asyncio.fixture
async def executor() -> AsyncIterator[VadExecutor]:
    async with VadExecutor(1) as pool:
        yield pool


def _overlap_ms(detected: list[tuple[float, float]], start: float, end: float) -> float:
    return sum(max(0.0, min(end, d_end) - max(start, d_start)) for d_start, d_end in detected)


async def test_gated_silero_finds_the_speech_segments_under_stationary_noise(executor: VadExecutor) -> None:
    speech = read_fixture("speech.wav")
    noise = _noise(speech.size / RATE, -30.0, seed=5)
    noisy = np.clip(speech.astype(np.int32) + noise, -32_768, 32_767).astype(np.int16)
    gated = GatedVad(SileroVad(SileroModel.load(MODELS_DIR), executor))
    detected: list[tuple[float, float]] = []
    opened: float | None = None
    for event in await _feed(gated, noisy):
        if event.kind == "speech_start":
            opened = event.at_ms
        elif event.kind == "speech_end" and opened is not None:
            detected.append((opened, event.at_ms))
            opened = None
    if opened is not None:
        detected.append((opened, math.inf))
    truth = sum(end - start for start, end in SPEECH_SEGMENTS_MS)
    covered = sum(_overlap_ms(detected, start, end) for start, end in SPEECH_SEGMENTS_MS)
    assert gated.gate.profile_ready
    assert len(detected) == len(SPEECH_SEGMENTS_MS)
    assert covered / truth >= 0.95
