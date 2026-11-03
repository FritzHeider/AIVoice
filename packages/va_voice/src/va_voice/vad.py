"""Silero voice activity detection per call leg (specs/07-media-pipeline.md §5.3).

:class:`SileroVad` takes decoded 16 kHz PCM16 in any chunk size, cuts it into 512-sample (32 ms) windows and
hands every window to the process's :class:`VadExecutor`, never running inference on the event loop. The
executor coalesces the windows of all legs that arrive in the same event-loop turn into one batched
:meth:`~va_voice.vad_model.VadModel.infer_batch` call on its thread pool (sized by ``MEDIA_VAD_THREADS``, at
most that many batches in flight, :data:`MAX_BATCH` windows each; specs/07 §5.3 Decision P05-T17e2), so a pod
pays one thread hop per batch instead of one per leg feed. :class:`VadDecider` turns probabilities into
events with hysteresis, the 3-window start rule, and the energy floor. Event times are stream audio time
(samples fed), so results do not depend on wall-clock scheduling.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from types import TracebackType
from typing import Final, Literal, Self

import numpy as np
from numpy.typing import NDArray

from va_voice.audio import pcm_to_float32, rms_dbfs
from va_voice.vad_model import CONTEXT_SAMPLES, SAMPLE_RATE, WINDOW_SAMPLES, VadModel, initial_state

type _Result = tuple[float, NDArray[np.float32]]

__all__ = [
    "CLOSED_MESSAGE",
    "ENERGY_FLOOR_DBFS",
    "MAX_BATCH",
    "SILENCE_THRESHOLD",
    "SPEECH_THRESHOLD",
    "START_WINDOWS",
    "WINDOW_MS",
    "SileroVad",
    "VadDecider",
    "VadEvent",
    "VadEventKind",
    "VadExecutor",
]

WINDOW_MS: Final = WINDOW_SAMPLES * 1000 / SAMPLE_RATE
SPEECH_THRESHOLD: Final = 0.5
SILENCE_THRESHOLD: Final = 0.35
START_WINDOWS: Final = 3
ENERGY_FLOOR_DBFS: Final = -55.0
MAX_BATCH: Final = 64
"""Windows per batched inference; more pending windows are split over several batches."""
CLOSED_MESSAGE: Final = "VAD executor closed"

VadEventKind = Literal["speech_start", "speech_end", "probability"]


@dataclass(frozen=True, slots=True)
class VadEvent:
    """A VAD observation in stream time (ms since the stream start or the last reset).

    ``probability``: one per window, ``at_ms`` = window start, ``speech`` = the window's decision after
    hysteresis and the energy floor. ``speech_start`` (``SpeechStarted``): ``at_ms`` = start of the first of
    the ``START_WINDOWS`` consecutive speech windows. ``speech_end`` (``SilenceStarted``, the end-of-speech
    candidate): ``at_ms`` = end of the last speech window; a later ``speech_start`` means speech resumed.
    """

    kind: VadEventKind
    at_ms: float
    probability: float
    speech: bool


class VadDecider:
    """Pure decision logic over per-window probabilities and levels (no model, no I/O)."""

    __slots__ = ("_last_voiced_end_ms", "_run", "_run_start_ms", "_speaking", "_speech")

    def __init__(self) -> None:
        self._speech = False
        self._speaking = False
        self._run = 0
        self._run_start_ms = 0.0
        self._last_voiced_end_ms = 0.0

    @property
    def speech(self) -> bool:
        """Decision of the last window."""
        return self._speech

    @property
    def speaking(self) -> bool:
        """True between a ``speech_start`` and the following ``speech_end``."""
        return self._speaking

    def reset(self) -> None:
        self._speech = False
        self._speaking = False
        self._run = 0
        self._run_start_ms = 0.0
        self._last_voiced_end_ms = 0.0

    def _decide(self, probability: float, level_dbfs: float) -> bool:
        if level_dbfs < ENERGY_FLOOR_DBFS:
            return False
        if probability >= SPEECH_THRESHOLD:
            return True
        if probability < SILENCE_THRESHOLD:
            return False
        return self._speech

    def step(self, at_ms: float, probability: float, level_dbfs: float) -> list[VadEvent]:
        """Events for the window at ``at_ms`` with model ``probability`` and RMS ``level_dbfs``."""
        speech = self._decide(probability, level_dbfs)
        self._speech = speech
        events = [VadEvent("probability", at_ms, probability, speech)]
        if speech:
            if self._run == 0:
                self._run_start_ms = at_ms
            self._run += 1
            self._last_voiced_end_ms = at_ms + WINDOW_MS
            if not self._speaking and self._run >= START_WINDOWS:
                self._speaking = True
                events.append(VadEvent("speech_start", self._run_start_ms, probability, True))
        else:
            self._run = 0
            if self._speaking:
                self._speaking = False
                events.append(VadEvent("speech_end", self._last_voiced_end_ms, probability, False))
        return events


@dataclass(slots=True)
class _Request:
    """One window of one stream waiting for a batch: model input ``(576,)`` and state ``(2, 1, 128)``."""

    samples: NDArray[np.float32]
    state: NDArray[np.float32]
    future: asyncio.Future[_Result]


def _fail_closed(future: asyncio.Future[_Result]) -> None:
    if not future.done():
        future.set_exception(RuntimeError(CLOSED_MESSAGE))


class VadExecutor:
    """The process's VAD thread pool and window batcher, awaited from the event loop.

    :meth:`infer` queues a window; the first request of an event-loop turn schedules a flush for the next
    turn, which stacks the queued windows of a model (up to :data:`MAX_BATCH`) into one ``infer_batch`` on
    the pool. While :attr:`threads` batches are in flight new windows accumulate and go out when one
    completes, so an idle pod adds no wait and a busy one batches more. A cancelled waiter's window may
    still be computed; its result is discarded. Requests are independent: the pool computes on stacked
    copies and never touches a stream's state.
    """

    __slots__ = (
        "_batches",
        "_closed",
        "_flush_scheduled",
        "_in_flight",
        "_pending",
        "_pool",
        "_threads",
        "_windows",
    )

    def __init__(self, threads: int) -> None:
        if threads < 1:
            msg = f"threads must be >= 1, got {threads}"
            raise ValueError(msg)
        self._threads = threads
        self._pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="va-vad")
        self._pending: dict[int, tuple[VadModel, list[_Request]]] = {}
        self._flush_scheduled = False
        self._in_flight = 0
        self._closed = False
        self._batches = 0
        self._windows = 0

    @property
    def threads(self) -> int:
        return self._threads

    @property
    def batches(self) -> int:
        """Batched inferences submitted so far."""
        return self._batches

    @property
    def windows(self) -> int:
        """Windows submitted in those batches."""
        return self._windows

    @property
    def in_flight(self) -> int:
        """Batches running (or queued) on the pool right now."""
        return self._in_flight

    async def run[T](self, fn: Callable[[], T]) -> T:
        """Result of ``fn()`` computed on a pool thread."""
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn)

    async def infer(
        self, model: VadModel, samples: NDArray[np.float32], state: NDArray[np.float32]
    ) -> _Result:
        """``(probability, next_state)`` of one stream window, computed in a batch with other streams'."""
        if self._closed:
            raise RuntimeError(CLOSED_MESSAGE)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[_Result] = loop.create_future()
        entry = self._pending.setdefault(id(model), (model, []))
        entry[1].append(_Request(samples, state, future))
        self._schedule(loop)
        return await future

    def _schedule(self, loop: asyncio.AbstractEventLoop) -> None:
        if not self._flush_scheduled and self._in_flight < self._threads:
            self._flush_scheduled = True
            loop.call_soon(self._flush, loop)

    def _flush(self, loop: asyncio.AbstractEventLoop) -> None:
        self._flush_scheduled = False
        while self._pending and self._in_flight < self._threads and not self._closed:
            key = next(iter(self._pending))
            model, queued = self._pending[key]
            live = [request for request in queued if not request.future.done()]
            batch, rest = live[:MAX_BATCH], live[MAX_BATCH:]
            if rest:
                self._pending[key] = (model, rest)
            else:
                del self._pending[key]
            if batch:
                self._submit(loop, model, batch)

    def _submit(self, loop: asyncio.AbstractEventLoop, model: VadModel, batch: list[_Request]) -> None:
        samples = np.stack([request.samples for request in batch])
        state = np.concatenate([request.state for request in batch], axis=1)
        self._in_flight += 1
        self._batches += 1
        self._windows += len(batch)
        work = loop.run_in_executor(self._pool, model.infer_batch, samples, state)
        work.add_done_callback(functools.partial(self._resolve, loop, batch))

    def _resolve(
        self,
        loop: asyncio.AbstractEventLoop,
        batch: list[_Request],
        work: asyncio.Future[tuple[NDArray[np.float32], NDArray[np.float32]]],
    ) -> None:
        self._in_flight -= 1
        if work.cancelled():
            for request in batch:
                _fail_closed(request.future)
        elif (error := work.exception()) is not None:
            for request in batch:
                if not request.future.done():
                    request.future.set_exception(error)
        else:
            probabilities, states = work.result()
            for index, request in enumerate(batch):
                if not request.future.done():
                    result = (float(probabilities[index]), states[:, index : index + 1].copy())
                    request.future.set_result(result)
        if self._pending and not self._closed:
            self._schedule(loop)

    def _fail_pending(self) -> None:
        pending, self._pending = self._pending, {}
        for _, queued in pending.values():
            for request in queued:
                future = request.future
                try:
                    future.get_loop().call_soon_threadsafe(_fail_closed, future)
                except RuntimeError:
                    continue  # its event loop is closed: nobody waits any more

    def close(self) -> None:
        """Stop the pool: queued windows fail with ``RuntimeError``, queued work is cancelled, running
        inference finishes (blocks a few ms)."""
        self._closed = True
        self._fail_pending()
        self._pool.shutdown(wait=True, cancel_futures=True)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self._closed = True
        self._fail_pending()
        await asyncio.to_thread(self.close)


class SileroVad:
    """VAD of one call leg: per-stream recurrent state and context over a shared model and executor.

    Feeds are serialised (an ``asyncio.Lock``), so events keep audio order even when feeds overlap. A feed
    awaits its windows one by one (the recurrent state chains them); state and context are committed only
    when all of them completed, so a cancelled :meth:`feed` drops its audio without corrupting the stream.
    """

    __slots__ = ("_context", "_decider", "_executor", "_lock", "_model", "_pending", "_state", "_windows")

    def __init__(self, model: VadModel, executor: VadExecutor) -> None:
        self._model = model
        self._executor = executor
        self._lock = asyncio.Lock()
        self._decider = VadDecider()
        self._state = initial_state()
        self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
        self._pending = np.zeros(0, dtype=np.int16)
        self._windows = 0

    @property
    def elapsed_ms(self) -> float:
        """Stream time covered by the windows processed so far."""
        return self._windows * WINDOW_MS

    @property
    def pending_samples(self) -> int:
        """Samples waiting for a complete 512-sample window."""
        return int(self._pending.size)

    @property
    def speaking(self) -> bool:
        return self._decider.speaking

    async def feed(self, pcm16k: NDArray[np.int16]) -> list[VadEvent]:
        """Events for every window ``pcm16k`` completes (mono int16, 16 kHz; leftover samples wait)."""
        if pcm16k.dtype != np.int16 or pcm16k.ndim != 1:
            msg = f"expected 1-D int16 samples, got {pcm16k.dtype} with shape {pcm16k.shape}"
            raise ValueError(msg)
        async with self._lock:
            pending = np.concatenate([self._pending, pcm16k])
            ready = (pending.size // WINDOW_SAMPLES) * WINDOW_SAMPLES
            self._pending = pending[ready:]
            if ready == 0:
                return []
            samples = pending[:ready]
            floats = pcm_to_float32(samples)
            state, context = self._state, self._context
            windows: list[tuple[float, float]] = []
            for start in range(0, ready, WINDOW_SAMPLES):
                window = floats[start : start + WINDOW_SAMPLES]
                probability, state = await self._executor.infer(
                    self._model, np.concatenate([context, window]), state
                )
                context = window[-CONTEXT_SAMPLES:]
                windows.append((probability, rms_dbfs(samples[start : start + WINDOW_SAMPLES])))
            self._state, self._context = state, context.copy()
            events: list[VadEvent] = []
            for probability, level in windows:
                events.extend(self._decider.step(self.elapsed_ms, probability, level))
                self._windows += 1
            return events

    async def reset(self) -> None:
        """Back to the initial state (zero state/context, no pending audio, time 0) for a new call leg."""
        async with self._lock:
            self._decider.reset()
            self._state = initial_state()
            self._context = np.zeros(CONTEXT_SAMPLES, dtype=np.float32)
            self._pending = np.zeros(0, dtype=np.int16)
            self._windows = 0
