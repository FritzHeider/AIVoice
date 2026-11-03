"""VadDecider rules and SileroVad stream handling over a scripted model (specs/07-media-pipeline.md §5.3)."""

from __future__ import annotations

import asyncio
import itertools
import threading
from collections.abc import Callable, Iterator, Sequence

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from numpy.typing import NDArray
from vad_fakes import ScriptedModel

from va_voice.audio import pcm_to_float32
from va_voice.vad import (
    CLOSED_MESSAGE,
    ENERGY_FLOOR_DBFS,
    MAX_BATCH,
    START_WINDOWS,
    WINDOW_MS,
    SileroVad,
    VadDecider,
    VadEvent,
    VadExecutor,
)
from va_voice.vad_model import CONTEXT_SAMPLES, STATE_SHAPE, WINDOW_SAMPLES

LOUD = -20.0  # a level well above the energy floor


def _edges(events: Sequence[VadEvent]) -> list[tuple[str, float]]:
    return [(e.kind, e.at_ms) for e in events if e.kind != "probability"]


def _run(
    decider: VadDecider, probabilities: Sequence[float], levels: Sequence[float] | None = None
) -> list[VadEvent]:
    events: list[VadEvent] = []
    for index, probability in enumerate(probabilities):
        level = LOUD if levels is None else levels[index]
        events.extend(decider.step(index * WINDOW_MS, probability, level))
    return events


def test_window_constants() -> None:
    assert WINDOW_MS == 32.0
    assert START_WINDOWS == 3
    assert ENERGY_FLOOR_DBFS == -55.0


@pytest.mark.parametrize(
    ("probabilities", "decisions"),
    [
        # p ≥ 0.5 speech, p < 0.35 silence, the band in between keeps the previous decision.
        ([0.5, 0.4, 0.35, 0.34, 0.4, 0.49], [True, True, True, False, False, False]),
        ([0.45, 0.6, 0.349, 0.36], [False, True, False, False]),
        ([0.99, 0.0, 0.5], [True, False, True]),
    ],
)
def test_hysteresis(probabilities: list[float], decisions: list[bool]) -> None:
    events = _run(VadDecider(), probabilities)
    assert [e.speech for e in events if e.kind == "probability"] == decisions


def test_probability_events_carry_window_start_and_probability() -> None:
    events = _run(VadDecider(), [0.1, 0.7])
    assert events == [VadEvent("probability", 0.0, 0.1, False), VadEvent("probability", 32.0, 0.7, True)]


def test_two_speech_windows_do_not_start_speech() -> None:
    decider = VadDecider()
    assert _edges(_run(decider, [0.9, 0.9, 0.1, 0.9, 0.9, 0.1])) == []
    assert not decider.speaking


def test_three_speech_windows_start_at_first_window_start() -> None:
    decider = VadDecider()
    events = _run(decider, [0.1, 0.1, 0.8, 0.4, 0.9])
    # Window 3 (p=0.4) is in the band and keeps "speech"; the run started at window 2 (64 ms).
    assert _edges(events) == [("speech_start", 64.0)]
    assert events[-1] == VadEvent("speech_start", 64.0, 0.9, True)
    assert decider.speaking
    assert decider.speech


def test_end_candidate_at_end_of_last_speech_window() -> None:
    decider = VadDecider()
    events = _run(decider, [0.9, 0.9, 0.9, 0.9, 0.2, 0.1])
    assert _edges(events) == [("speech_start", 0.0), ("speech_end", 128.0)]
    assert events[-2] == VadEvent("speech_end", 128.0, 0.2, False)
    assert not decider.speaking


def test_resumed_speech_starts_again_after_three_windows() -> None:
    events = _run(VadDecider(), [0.9, 0.9, 0.9, 0.1, 0.9, 0.9, 0.1, 0.9, 0.9, 0.9, 0.9])
    assert _edges(events) == [("speech_start", 0.0), ("speech_end", 96.0), ("speech_start", 224.0)]


def test_energy_floor_forces_non_speech() -> None:
    levels = [LOUD, LOUD, LOUD, -55.01, LOUD, -80.0, float("-inf")]
    events = _run(VadDecider(), [0.99] * len(levels), levels)
    assert [e.speech for e in events if e.kind == "probability"] == [
        True,
        True,
        True,
        False,
        True,
        False,
        False,
    ]
    assert _edges(events) == [("speech_start", 0.0), ("speech_end", 96.0)]


def test_energy_floor_is_inclusive_at_the_threshold() -> None:
    events = _run(VadDecider(), [0.99], [ENERGY_FLOOR_DBFS])
    assert events[0].speech


def test_energy_floor_breaks_hysteresis_hold() -> None:
    events = _run(VadDecider(), [0.9, 0.4, 0.4], [LOUD, -60.0, LOUD])
    assert [e.speech for e in events if e.kind == "probability"] == [True, False, False]


def test_decider_reset() -> None:
    decider = VadDecider()
    _run(decider, [0.9, 0.9, 0.9])
    decider.reset()
    assert (decider.speaking, decider.speech) == (False, False)
    # The band no longer holds speech after a reset, and a new start needs three fresh windows.
    assert _edges(_run(decider, [0.4, 0.9, 0.9, 0.9])) == [("speech_start", 32.0)]


@pytest.fixture
def executor() -> Iterator[VadExecutor]:
    pool = VadExecutor(2)
    yield pool
    pool.close()


def _ramp(n: int, start: int = 0) -> NDArray[np.int16]:
    """Loud, distinguishable samples (well above the energy floor)."""
    values = (np.arange(start, start + n) % 2000) + 1000
    return values.astype(np.int16)


async def test_windows_complete_across_feeds_and_leftover_is_kept(executor: VadExecutor) -> None:
    model = ScriptedModel()
    vad = SileroVad(model, executor)
    assert await vad.feed(_ramp(300)) == []
    assert vad.pending_samples == 300
    assert executor.batches == 0
    events = await vad.feed(_ramp(400, 300))
    assert [e.at_ms for e in events if e.kind == "probability"] == [0.0]
    assert vad.pending_samples == 188
    assert vad.elapsed_ms == 32.0
    np.testing.assert_array_equal(model.inputs[0][CONTEXT_SAMPLES:], pcm_to_float32(_ramp(WINDOW_SAMPLES)))


async def test_context_is_previous_window_tail_and_state_is_threaded(executor: VadExecutor) -> None:
    model = ScriptedModel()
    vad = SileroVad(model, executor)
    audio = _ramp(3 * WINDOW_SAMPLES)
    await vad.feed(audio)
    floats = pcm_to_float32(audio)
    assert [x.shape for x in model.inputs] == [(CONTEXT_SAMPLES + WINDOW_SAMPLES,)] * 3
    np.testing.assert_array_equal(model.inputs[0][:CONTEXT_SAMPLES], np.zeros(CONTEXT_SAMPLES, np.float32))
    for index in (1, 2):
        np.testing.assert_array_equal(
            model.inputs[index][:CONTEXT_SAMPLES],
            floats[index * WINDOW_SAMPLES - CONTEXT_SAMPLES : index * WINDOW_SAMPLES],
        )
    assert [float(s.flat[0]) for s in model.states] == [0.0, 1.0, 2.0]
    assert all(s.shape == STATE_SHAPE for s in model.states)
    # The state survives into the next feed.
    await vad.feed(_ramp(WINDOW_SAMPLES))
    assert float(model.states[3].flat[0]) == 3.0


async def test_inference_runs_on_the_vad_pool_one_batch_per_window_of_a_lone_stream(
    executor: VadExecutor,
) -> None:
    model = ScriptedModel()
    vad = SileroVad(model, executor)
    events = await vad.feed(_ramp(5 * WINDOW_SAMPLES))
    assert (executor.batches, executor.windows, model.batch_sizes) == (5, 5, [1] * 5)
    assert len(model.threads) == 5
    assert all(name.startswith("va-vad") for name in model.threads)
    assert threading.current_thread().name not in model.threads
    assert _edges(events) == [("speech_start", 0.0)]
    assert vad.speaking


@settings(max_examples=40, deadline=None)
@given(cuts=st.lists(st.integers(min_value=0, max_value=6 * WINDOW_SAMPLES), max_size=12))
def test_any_chunking_gives_the_same_events(cuts: list[int]) -> None:
    script: Callable[[int], float] = lambda i: (0.1, 0.9, 0.9, 0.9, 0.4, 0.2)[i % 6]  # noqa: E731
    audio = _ramp(6 * WINDOW_SAMPLES + 100)
    bounds = [0, *sorted(cuts), audio.size]

    async def scenario() -> tuple[list[VadEvent], list[VadEvent]]:
        async with VadExecutor(2) as pool:
            whole = await SileroVad(ScriptedModel(script), pool).feed(audio)
            chunked_vad = SileroVad(ScriptedModel(script), pool)
            chunked: list[VadEvent] = []
            for lo, hi in itertools.pairwise(bounds):
                chunked.extend(await chunked_vad.feed(audio[lo:hi]))
            return whole, chunked

    whole, chunked = asyncio.run(scenario())
    assert chunked == whole
    assert len([e for e in whole if e.kind == "probability"]) == 6


async def test_concurrent_feeds_keep_audio_order(executor: VadExecutor) -> None:
    model = ScriptedModel()
    vad = SileroVad(model, executor)
    chunks = [_ramp(WINDOW_SAMPLES, k * WINDOW_SAMPLES) for k in range(8)]
    results = await asyncio.gather(*(vad.feed(chunk) for chunk in chunks))
    starts = [e.at_ms for events in results for e in events if e.kind == "probability"]
    assert starts == [k * WINDOW_MS for k in range(8)]
    for k, samples in enumerate(model.inputs):
        np.testing.assert_array_equal(samples[CONTEXT_SAMPLES:], pcm_to_float32(chunks[k]))


async def test_reset_returns_to_initial_state(executor: VadExecutor) -> None:
    model = ScriptedModel()
    vad = SileroVad(model, executor)
    await vad.feed(_ramp(3 * WINDOW_SAMPLES + 10))
    assert vad.speaking
    await vad.reset()
    assert (vad.speaking, vad.elapsed_ms, vad.pending_samples) == (False, 0.0, 0)
    events = await vad.feed(_ramp(WINDOW_SAMPLES))
    assert events[0].at_ms == 0.0
    np.testing.assert_array_equal(model.inputs[-1][:CONTEXT_SAMPLES], np.zeros(CONTEXT_SAMPLES, np.float32))
    assert float(model.states[-1].flat[0]) == 0.0


async def test_cancelled_feed_leaves_the_stream_consistent(executor: VadExecutor) -> None:
    release = threading.Event()
    entered = threading.Event()

    def slow(index: int) -> float:
        if index == 0:
            entered.set()
            release.wait(5)
        return 0.9

    model = ScriptedModel(slow)
    vad = SileroVad(model, executor)
    task = asyncio.create_task(vad.feed(_ramp(WINDOW_SAMPLES)))
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    # The cancelled window is dropped: time and state did not advance.
    assert vad.elapsed_ms == 0.0
    events = await vad.feed(_ramp(WINDOW_SAMPLES))
    assert [e.at_ms for e in events] == [0.0]
    assert float(model.states[-1].flat[0]) == 0.0


async def test_silent_input_is_never_speech_whatever_the_model_says(executor: VadExecutor) -> None:
    vad = SileroVad(ScriptedModel(lambda _: 1.0), executor)
    events = await vad.feed(np.zeros(4 * WINDOW_SAMPLES, dtype=np.int16))
    assert [e.speech for e in events] == [False] * 4


@pytest.mark.parametrize(
    "samples",
    [np.zeros(512, dtype=np.float32), np.zeros((2, 256), dtype=np.int16), np.zeros(512, dtype=np.int32)],
)
async def test_feed_rejects_other_shapes_and_types(
    executor: VadExecutor, samples: NDArray[np.generic]
) -> None:
    vad = SileroVad(ScriptedModel(), executor)
    with pytest.raises(ValueError, match="1-D int16"):
        await vad.feed(samples)  # type: ignore[arg-type]


def test_executor_rejects_empty_pool() -> None:
    with pytest.raises(ValueError, match="threads"):
        VadExecutor(0)


async def test_executor_context_manager_shuts_the_pool_down() -> None:
    async with VadExecutor(3) as pool:
        assert pool.threads == 3
        assert await pool.run(lambda: threading.current_thread().name.startswith("va-vad"))
    with pytest.raises(RuntimeError):
        await pool.run(lambda: None)
    with pytest.raises(RuntimeError, match=CLOSED_MESSAGE):
        await pool.infer(ScriptedModel(), np.zeros(CONTEXT_SAMPLES + WINDOW_SAMPLES, np.float32), _state())


def _state(value: float = 0.0) -> NDArray[np.float32]:
    return np.full(STATE_SHAPE, value, dtype=np.float32)


def _window(value: float) -> NDArray[np.float32]:
    return np.full(CONTEXT_SAMPLES + WINDOW_SAMPLES, value, dtype=np.float32)


async def test_windows_of_concurrent_legs_share_one_batch(executor: VadExecutor) -> None:
    script: Callable[[int], float] = lambda i: (0.1, 0.9, 0.9, 0.9, 0.4, 0.2)[i % 6]  # noqa: E731
    audio = _ramp(6 * WINDOW_SAMPLES)
    legs = 20
    alone = []
    for _ in range(legs):
        alone.append(await SileroVad(ScriptedModel(script), executor).feed(audio))
    batches_before = executor.batches
    shared = ScriptedModel(script)
    vads = [SileroVad(shared, executor) for _ in range(legs)]
    together = await asyncio.gather(*(vad.feed(audio) for vad in vads))
    assert together == alone
    # Every window position of the 20 legs went out as one batch of 20.
    assert shared.batch_sizes == [legs] * 6
    assert executor.batches - batches_before == 6


async def test_batch_rows_keep_their_own_state_and_samples(executor: VadExecutor) -> None:
    model = ScriptedModel()
    results = await asyncio.gather(*(executor.infer(model, _window(k / 10), _state(k)) for k in range(5)))
    assert model.batch_sizes == [5]
    for k, (_, state) in enumerate(results):
        assert state.shape == STATE_SHAPE
        assert float(state.flat[0]) == k + 1
        np.testing.assert_array_equal(model.inputs[k], _window(k / 10))


async def test_a_lone_window_is_not_held_back() -> None:
    async with VadExecutor(1) as pool:
        model = ScriptedModel()
        probability, state = await pool.infer(model, _window(0.0), _state())
    assert (float(state.flat[0]), model.batch_sizes) == (1.0, [1])
    assert probability == pytest.approx(0.9)


async def test_more_windows_than_a_batch_holds_are_split_and_in_flight_is_bounded() -> None:
    async with VadExecutor(2) as pool:
        model = ScriptedModel()
        model.on_batch = lambda: model.in_flight_seen.append(pool.in_flight)
        count = 2 * MAX_BATCH + 10
        await asyncio.gather(*(pool.infer(model, _window(0.0), _state()) for _ in range(count)))
    assert sum(model.batch_sizes) == count
    assert max(model.batch_sizes) == MAX_BATCH
    assert len(model.batch_sizes) == 3
    assert max(model.in_flight_seen) <= 2


async def test_a_failing_batch_fails_each_waiter_and_the_executor_keeps_working(
    executor: VadExecutor,
) -> None:
    model = ScriptedModel()
    model.fail_next = True
    results = await asyncio.gather(
        *(executor.infer(model, _window(0.0), _state()) for _ in range(3)), return_exceptions=True
    )
    assert [type(r) for r in results] == [OSError] * 3
    probability, _ = await executor.infer(model, _window(0.0), _state())
    assert probability == pytest.approx(0.9)


async def test_a_cancelled_waiter_does_not_disturb_its_batch(executor: VadExecutor) -> None:
    release = threading.Event()
    model = ScriptedModel()
    model.on_batch = lambda: release.wait(5)
    first = asyncio.create_task(executor.infer(model, _window(0.1), _state(1)))
    second = asyncio.create_task(executor.infer(model, _window(0.2), _state(2)))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    first.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    _, state = await second
    assert float(state.flat[0]) == 3.0


async def test_a_cancelled_waiter_before_the_flush_is_left_out(executor: VadExecutor) -> None:
    model = ScriptedModel()
    first = asyncio.create_task(executor.infer(model, _window(0.1), _state(1)))
    second = asyncio.create_task(executor.infer(model, _window(0.2), _state(2)))
    await asyncio.sleep(0)  # both queued, flush scheduled for the next turn
    first.cancel()
    _, state = await second
    assert first.cancelled()
    assert model.batch_sizes == [1]
    assert float(state.flat[0]) == 3.0


async def test_closing_fails_queued_windows() -> None:
    pool = VadExecutor(1)
    release, entered = threading.Event(), threading.Event()
    model = ScriptedModel()

    def hold() -> None:
        entered.set()
        release.wait(5)

    model.on_batch = hold
    running = asyncio.create_task(pool.infer(model, _window(0.0), _state()))
    await asyncio.to_thread(entered.wait, 5)
    assert pool.in_flight == 1
    queued = asyncio.create_task(pool.infer(model, _window(0.0), _state()))
    await asyncio.sleep(0)
    closing = asyncio.create_task(pool.__aexit__(None, None, None))
    with pytest.raises(RuntimeError, match=CLOSED_MESSAGE):
        await queued
    release.set()
    await closing
    assert (await running)[0] == pytest.approx(0.9)
