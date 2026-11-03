"""A scripted stand-in for the Silero model (va_voice.vad_model.VadModel) for VAD and pipeline tests."""

from __future__ import annotations

import threading
from collections.abc import Callable

import numpy as np
from numpy.typing import NDArray

__all__ = ["ScriptedModel"]


def _always_speech(_: int) -> float:
    return 0.9


class ScriptedModel:
    """A thread-safe fake Silero over batches.

    Each row's next state is its state + 1, so ``state.flat[0]`` counts the windows its stream has seen; the
    probability of a row is ``probabilities(that count)``, which keeps scripts per stream even when windows
    of several streams share a batch. Records every row's input/state, the batch sizes and the pool thread
    names. ``on_batch`` runs on the pool thread before a batch is computed; ``fail_next`` makes the next
    batch raise :class:`OSError`.
    """

    def __init__(self, probabilities: Callable[[int], float] = _always_speech) -> None:
        self._probabilities = probabilities
        self._lock = threading.Lock()
        self.inputs: list[NDArray[np.float32]] = []
        self.states: list[NDArray[np.float32]] = []
        self.threads: list[str] = []
        self.batch_sizes: list[int] = []
        self.in_flight_seen: list[int] = []
        self.on_batch: Callable[[], object] | None = None
        self.fail_next = False

    def infer_batch(
        self, samples: NDArray[np.float32], state: NDArray[np.float32]
    ) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
        if self.on_batch is not None:
            self.on_batch()
        with self._lock:
            if self.fail_next:
                self.fail_next = False
                message = "scripted model failure"
                raise OSError(message)
            self.batch_sizes.append(int(samples.shape[0]))
            for row in range(samples.shape[0]):
                self.inputs.append(samples[row].copy())
                self.states.append(state[:, row : row + 1].copy())
                self.threads.append(threading.current_thread().name)
        steps = [int(state[0, row, 0]) for row in range(samples.shape[0])]
        probabilities = np.array([self._probabilities(step) for step in steps], dtype=np.float32)
        return probabilities, state + 1
