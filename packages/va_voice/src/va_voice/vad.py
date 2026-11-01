"""Silero voice activity detection per call leg (specs/07-media-pipeline.md §5.3).

:class:`SileroVad` takes decoded 16 kHz PCM16 in any chunk size, cuts it into 512-sample (32 ms) windows and
runs the shared :class:`~va_voice.vad_model.VadModel` for every window in a dedicated thread pool
(:class:`VadExecutor`, sized by ``MEDIA_VAD_THREADS``): one pool hop per :meth:`SileroVad.feed`, never
inference on the event loop. :class:`VadDecider` turns probabilities into events with hysteresis, the 3-window
start rule, and the energy floor. Event times are stream audio time (samples fed), so results do not depend on
wall-clock scheduling.
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

__all__ = [
    "ENERGY_FLOOR_DBFS",
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


class VadExecutor:
    """The process's VAD thread pool; every inference runs here, awaited from the event loop."""

    __slots__ = ("_pool", "_threads")

    def __init__(self, threads: int) -> None:
        if threads < 1:
            msg = f"threads must be >= 1, got {threads}"
            raise ValueError(msg)
        self._threads = threads
        self._pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="va-vad")

    @property
    def threads(self) -> int:
        return self._threads

    async def run[T](self, fn: Callable[[], T]) -> T:
        """Result of ``fn()`` computed on a pool thread."""
        return await asyncio.get_running_loop().run_in_executor(self._pool, fn)

    def close(self) -> None:
        """Stop the pool: queued work is cancelled, running inference finishes (blocks a few ms)."""
        self._pool.shutdown(wait=True, cancel_futures=True)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await asyncio.to_thread(self.close)


@dataclass(frozen=True, slots=True)
class _Inference:
    """Pool-thread result of a batch: per-window (probability, level) and the stream state after it."""

    windows: list[tuple[float, float]]
    state: NDArray[np.float32]
    context: NDArray[np.float32]


def _infer_windows(
    model: VadModel, samples: NDArray[np.int16], state: NDArray[np.float32], context: NDArray[np.float32]
) -> _Inference:
    floats = pcm_to_float32(samples)
    results: list[tuple[float, float]] = []
    for start in range(0, samples.size, WINDOW_SAMPLES):
        window = floats[start : start + WINDOW_SAMPLES]
        probability, state = model.infer(np.concatenate([context, window]), state)
        context = window[-CONTEXT_SAMPLES:].copy()
        results.append((probability, rms_dbfs(samples[start : start + WINDOW_SAMPLES])))
    return _Inference(results, state, context)


class SileroVad:
    """VAD of one call leg: per-stream recurrent state and context over a shared model and executor.

    Feeds are serialised (an ``asyncio.Lock``), so events keep audio order even when feeds overlap. The pool
    computes on copies of the state; it is committed on the loop only when the batch completes, so a cancelled
    :meth:`feed` drops its audio without corrupting the stream.
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
            job = functools.partial(_infer_windows, self._model, pending[:ready], self._state, self._context)
            inference = await self._executor.run(job)
            self._state, self._context = inference.state, inference.context
            events: list[VadEvent] = []
            for probability, level in inference.windows:
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
