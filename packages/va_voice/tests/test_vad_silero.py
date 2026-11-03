"""SileroVad with the real ONNX model (specs/07-media-pipeline.md §5.3, §22; specs/30 §3.1).

Needs ``models/silero_vad.onnx`` (``make setup`` / ``scripts/fetch_models.sh``; CI fetches it first).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import statistics
import time
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio
from numpy.random import PCG64
from numpy.typing import NDArray
from vad_fixtures import FIXTURE_DIR, SPEECH_SEGMENTS_MS, build_fixtures, read_fixture

from va_sim.audio import MARKER_MS, synthesize_utterance
from va_voice.audio import Resampler, pcm_to_float32, ulaw_decode
from va_voice.vad import WINDOW_MS, SileroVad, VadEvent, VadExecutor
from va_voice.vad_model import (
    MANIFEST_NAME,
    MODEL_INPUT_SAMPLES,
    STATE_LAYERS,
    STATE_SHAPE,
    STATE_UNITS,
    SileroModel,
    VadModelError,
    initial_state,
    verify_model_file,
)

MODELS_DIR = Path(__file__).resolve().parents[3] / "models"
FRAME_SAMPLES = 320  # 20 ms at 16 kHz, the size the media worker feeds


@pytest.fixture(scope="module")
def model() -> SileroModel:
    return SileroModel.load(MODELS_DIR)


@pytest_asyncio.fixture
async def executor() -> AsyncIterator[VadExecutor]:
    async with VadExecutor(2) as pool:
        yield pool


async def _events(vad: SileroVad, pcm: NDArray[np.int16], frame: int = FRAME_SAMPLES) -> list[VadEvent]:
    events: list[VadEvent] = []
    for start in range(0, pcm.size, frame):
        events.extend(await vad.feed(pcm[start : start + frame]))
    return events


def _edges(events: Sequence[VadEvent]) -> list[tuple[str, float]]:
    return [(e.kind, e.at_ms) for e in events if e.kind != "probability"]


def _windows(events: Sequence[VadEvent]) -> list[VadEvent]:
    return [e for e in events if e.kind == "probability"]


@pytest.fixture
def models_copy(tmp_path: Path) -> Iterator[Path]:
    for name in ("silero_vad.onnx", MANIFEST_NAME):
        shutil.copy(MODELS_DIR / name, tmp_path / name)
    yield tmp_path


def test_vad_fixtures_are_reproducible() -> None:
    for name, data in build_fixtures().items():
        assert (FIXTURE_DIR / name).read_bytes() == data, f"{name} is stale: run vad_fixtures.py --write"


def test_load_verifies_the_manifest(models_copy: Path) -> None:
    assert verify_model_file(models_copy) == models_copy / "silero_vad.onnx"
    with (models_copy / "silero_vad.onnx").open("ab") as fh:
        fh.write(b"\0")
    with pytest.raises(VadModelError, match="does not match the manifest sha256"):
        SileroModel.load(models_copy)


def test_load_rejects_an_unlisted_model(models_copy: Path) -> None:
    manifest = json.loads((models_copy / MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["models"][0]["name"] = "other"
    (models_copy / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(VadModelError, match="not listed"):
        SileroModel.load(models_copy)


def test_load_rejects_a_file_onnxruntime_cannot_read(models_copy: Path) -> None:
    garbage = b"not an onnx model"
    (models_copy / "silero_vad.onnx").write_bytes(garbage)
    manifest = json.loads((models_copy / MANIFEST_NAME).read_text(encoding="utf-8"))
    manifest["models"][0]["sha256"] = hashlib.sha256(garbage).hexdigest()
    (models_copy / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(VadModelError, match="could not be loaded"):
        SileroModel.load(models_copy)


class _Node:
    def __init__(self, name: str) -> None:
        self.name = name


class _WrongSession:
    def get_inputs(self) -> list[_Node]:
        return [_Node("input"), _Node("h"), _Node("c"), _Node("sr")]

    def get_outputs(self) -> list[_Node]:
        return [_Node("output"), _Node("hn"), _Node("cn")]


def test_model_rejects_another_signature() -> None:
    # Silero v4 had separate h/c states; only the v5 signature is accepted.
    with pytest.raises(VadModelError, match="unexpected Silero VAD signature"):
        SileroModel(_WrongSession())


def test_infer_shapes_and_state(model: SileroModel) -> None:
    speech = pcm_to_float32(read_fixture("speech.wav"))
    state = initial_state()
    probabilities = []
    start = int(SPEECH_SEGMENTS_MS[0][0] * 16)
    for k in range(10):
        window = speech[start + k * 512 - 64 : start + (k + 1) * 512]
        probability, next_state = model.infer(window, state)
        assert next_state.shape == STATE_SHAPE
        assert next_state.dtype == np.float32
        assert not np.array_equal(next_state, state)
        state = next_state
        probabilities.append(probability)
    assert all(0.0 <= p <= 1.0 for p in probabilities)
    assert max(probabilities) > 0.9


def test_infer_rejects_wrong_shapes(model: SileroModel) -> None:
    with pytest.raises(ValueError, match="expected samples"):
        model.infer(np.zeros(512, np.float32), initial_state())
    with pytest.raises(ValueError, match="expected samples"):
        model.infer(np.zeros(MODEL_INPUT_SAMPLES, np.float32), np.zeros((2, 128), np.float32))


def test_infer_batch_equals_independent_streams(model: SileroModel) -> None:
    speech = pcm_to_float32(read_fixture("speech.wav"))
    start = int(SPEECH_SEGMENTS_MS[0][0] * 16)
    rows, states = [], []
    for stream in range(5):
        # Distinct states: stream k has seen k windows of speech before its batched window.
        state = initial_state()
        for k in range(stream):
            _, state = model.infer(speech[start + k * 512 : start + k * 512 + MODEL_INPUT_SAMPLES], state)
        offset = start + stream * 512
        rows.append(speech[offset : offset + MODEL_INPUT_SAMPLES])
        states.append(state)
    probabilities, next_states = model.infer_batch(np.stack(rows), np.concatenate(states, axis=1))
    assert probabilities.shape == (5,)
    assert next_states.shape == (STATE_LAYERS, 5, STATE_UNITS)
    for stream in range(5):
        alone, alone_state = model.infer(rows[stream], states[stream])
        np.testing.assert_allclose(probabilities[stream], alone, atol=1e-5)
        np.testing.assert_allclose(next_states[:, stream : stream + 1], alone_state, atol=1e-5)


@pytest.mark.parametrize(
    ("samples", "state"),
    [
        (np.zeros(MODEL_INPUT_SAMPLES, np.float32), np.zeros((2, 1, 128), np.float32)),
        (np.zeros((2, 512), np.float32), np.zeros((2, 2, 128), np.float32)),
        (np.zeros((3, MODEL_INPUT_SAMPLES), np.float32), np.zeros((2, 2, 128), np.float32)),
        (np.zeros((0, MODEL_INPUT_SAMPLES), np.float32), np.zeros((2, 0, 128), np.float32)),
    ],
)
def test_infer_batch_rejects_wrong_shapes(
    model: SileroModel, samples: NDArray[np.float32], state: NDArray[np.float32]
) -> None:
    with pytest.raises(ValueError, match="expected samples"):
        model.infer_batch(samples, state)


async def test_concurrent_streams_batch_and_match_alone(model: SileroModel, executor: VadExecutor) -> None:
    speech = read_fixture("speech.wav")[: 16_000 * 2]
    alone = await _events(SileroVad(model, executor), speech)
    batches_before = executor.batches
    vads = [SileroVad(model, executor) for _ in range(10)]
    together = await asyncio.gather(*(_events(vad, speech) for vad in vads))
    for events in together:
        assert [(e.kind, e.at_ms, e.speech) for e in events] == [(e.kind, e.at_ms, e.speech) for e in alone]
        for got, want in zip(events, alone, strict=True):
            assert got.probability == pytest.approx(want.probability, abs=1e-5)
    assert executor.batches - batches_before < len(_windows(alone)) * len(vads)


async def test_speech_fixture_segments(model: SileroModel, executor: VadExecutor) -> None:
    edges = _edges(await _events(SileroVad(model, executor), read_fixture("speech.wav")))
    assert [kind for kind, _ in edges] == ["speech_start", "speech_end"] * len(SPEECH_SEGMENTS_MS)
    for index, (start_ms, end_ms) in enumerate(SPEECH_SEGMENTS_MS):
        detected_start, detected_end = edges[2 * index][1], edges[2 * index + 1][1]
        # Measured: starts +2..+12 ms (window granularity), ends +56..+98 ms (the model's hangover).
        assert abs(detected_start - start_ms) <= 50
        assert 0 <= detected_end - end_ms <= 150


@pytest.mark.parametrize(
    ("name", "max_probability"),
    # Measured maxima: silence 0.01, office noise 0.09, DTMF 0.15.
    [("silence.wav", 0.05), ("noise.wav", 0.25), ("dtmf.wav", 0.3)],
)
async def test_non_speech_fixtures(
    model: SileroModel, executor: VadExecutor, name: str, max_probability: float
) -> None:
    events = await _events(SileroVad(model, executor), read_fixture(name))
    windows = _windows(events)
    assert len(windows) >= 25
    assert _edges(events) == []
    assert not any(e.speech for e in windows)
    assert max(e.probability for e in windows) < max_probability


async def test_streams_on_one_model_are_independent(model: SileroModel, executor: VadExecutor) -> None:
    speech, noise = read_fixture("speech.wav"), read_fixture("noise.wav")
    alone_speech = await _events(SileroVad(model, executor), speech)
    alone_noise = await _events(SileroVad(model, executor), noise)
    first, second = SileroVad(model, executor), SileroVad(model, executor)
    mixed_speech: list[VadEvent] = []
    mixed_noise: list[VadEvent] = []

    async def run(vad: SileroVad, pcm: NDArray[np.int16], out: list[VadEvent]) -> None:
        for start in range(0, pcm.size, FRAME_SAMPLES):
            out.extend(await vad.feed(pcm[start : start + FRAME_SAMPLES]))
            await asyncio.sleep(0)

    await asyncio.gather(run(first, speech, mixed_speech), run(second, noise, mixed_noise))
    assert mixed_speech == alone_speech
    assert mixed_noise == alone_noise


async def test_reset_restarts_the_stream(model: SileroModel, executor: VadExecutor) -> None:
    speech = read_fixture("speech.wav")
    vad = SileroVad(model, executor)
    first = await _events(vad, speech)
    await vad.reset()
    assert await _events(vad, speech) == first


async def test_synth_detected_by_silero(model: SileroModel, executor: VadExecutor) -> None:
    """specs/30 §3.1: ≥ 90 % of 32 ms windows in the voiced body of 20 random utterances are speech."""
    rng = PCG64(30_031)
    alphabet = "abcdefghijklmnopqrstuvwxyz     "
    for utterance in range(20):
        draws = rng.random_raw(64)
        length = 20 + int(draws[0] % 41)
        f0_hz = 100.0 + float(draws[1] % 121)
        seed = int(draws[2] % 1_000_000)
        text = "v" + "".join(alphabet[int(d % len(alphabet))] for d in draws[3 : 3 + length - 1])
        wire = await asyncio.to_thread(synthesize_utterance, utterance + 1, text, f0_hz=f0_hz, seed=seed)
        resampler = Resampler(8_000, 16_000)
        pcm = np.concatenate([resampler.process(ulaw_decode(wire)), resampler.flush()])
        windows = _windows(await _events(SileroVad(model, executor), pcm))
        body_start, body_end = MARKER_MS, MARKER_MS + len(text) * 70
        inside = [e for e in windows if e.at_ms >= body_start and e.at_ms + WINDOW_MS <= body_end]
        detected = sum(e.speech for e in inside) / len(inside)
        assert detected >= 0.9, (utterance, text, f0_hz, seed, detected)


def test_inference_cost_per_window(model: SileroModel) -> None:
    """specs/07 §21: VAD ≈ 0.3 ms per 32 ms window; the phase budget is 2 ms of CPU per 20 ms frame."""
    speech = pcm_to_float32(read_fixture("speech.wav"))
    state = initial_state()
    timings = []
    windows = (speech.size - MODEL_INPUT_SAMPLES) // 512
    for k in range(300):
        offset = (k % windows) * 512
        window = speech[offset : offset + MODEL_INPUT_SAMPLES]
        started = time.perf_counter()
        _, state = model.infer(window, state)
        timings.append(time.perf_counter() - started)
    assert statistics.median(timings) < 0.002
