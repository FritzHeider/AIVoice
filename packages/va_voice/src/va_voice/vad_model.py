"""Silero VAD v5 model: manifest check and the shared ONNX session (specs/07-media-pipeline.md §5.3).

The model file must sit next to ``MANIFEST.json`` (written by ``scripts/fetch_models.sh``) and match its
pinned sha256; :meth:`SileroModel.load` verifies it before onnxruntime reads a byte. One :class:`SileroModel`
serves every stream of a process (an onnxruntime session is safe to run from several threads); the recurrent
state and the 64-sample context belong to each stream (:class:`va_voice.vad.SileroVad`).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Final, Protocol

import numpy as np
import onnxruntime as ort
from numpy.typing import NDArray

__all__ = [
    "CONTEXT_SAMPLES",
    "INPUT_NAMES",
    "MANIFEST_NAME",
    "MODEL_INPUT_SAMPLES",
    "MODEL_NAME",
    "OUTPUT_NAMES",
    "SAMPLE_RATE",
    "STATE_LAYERS",
    "STATE_SHAPE",
    "STATE_UNITS",
    "WINDOW_SAMPLES",
    "SileroModel",
    "VadModel",
    "VadModelError",
    "initial_state",
    "verify_model_file",
]

MODEL_NAME: Final = "silero_vad"
MANIFEST_NAME: Final = "MANIFEST.json"

SAMPLE_RATE: Final = 16_000
WINDOW_SAMPLES: Final = 512
CONTEXT_SAMPLES: Final = 64
MODEL_INPUT_SAMPLES: Final = CONTEXT_SAMPLES + WINDOW_SAMPLES
STATE_LAYERS: Final = 2
STATE_UNITS: Final = 128
STATE_SHAPE: Final = (STATE_LAYERS, 1, STATE_UNITS)
"""The recurrent state of one stream; a batch of ``B`` streams is ``(2, B, 128)``."""
INPUT_NAMES: Final = ("input", "state", "sr")
OUTPUT_NAMES: Final = ("output", "stateN")

# onnxruntime logs warnings (e.g. unused initializers) to stderr at load; only errors are of interest.
_ORT_LOG_ERROR: Final = 3
_SAMPLE_RATE_INPUT: Final = np.array(SAMPLE_RATE, dtype=np.int64)
_BATCH_DIMS: Final = 2


class VadModelError(RuntimeError):
    """The VAD model is missing, unlisted, mismatches its checksum, or has an unexpected signature."""


class VadModel(Protocol):
    """One Silero step over a batch of independent streams (each row is one stream's next window)."""

    def infer_batch(
        self, samples: NDArray[np.float32], state: NDArray[np.float32]
    ) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
        """Speech probabilities ``(B,)`` of ``samples`` ``(B, 576)`` (64 context + 512 new per row, in
        [-1, 1]) and the next states ``(2, B, 128)`` for the current states ``state`` ``(2, B, 128)``."""
        ...


def initial_state() -> NDArray[np.float32]:
    """The zero recurrent state a stream starts (and resets) with."""
    return np.zeros(STATE_SHAPE, dtype=np.float32)


def _manifest_entry(models_dir: Path) -> dict[str, Any]:
    manifest_path = models_dir / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        msg = f"{manifest_path} is unreadable"
        raise VadModelError(msg) from exc
    models = manifest.get("models") if isinstance(manifest, dict) else None
    for entry in models if isinstance(models, list) else []:
        if isinstance(entry, dict) and entry.get("name") == MODEL_NAME:
            return entry
    msg = f"{MODEL_NAME} is not listed in {manifest_path}"
    raise VadModelError(msg)


def verify_model_file(models_dir: Path) -> Path:
    """Path of the verified model file in ``models_dir``; raises :class:`VadModelError` otherwise.

    Blocking (reads and hashes the file): async callers run it in a worker thread.
    """
    entry = _manifest_entry(models_dir)
    model_path = models_dir / Path(str(entry.get("path", ""))).name
    if not model_path.is_file():
        msg = f"{model_path} is missing (run scripts/fetch_models.sh)"
        raise VadModelError(msg)
    with model_path.open("rb") as fh:
        digest = hashlib.file_digest(fh, "sha256").hexdigest()
    if digest != entry.get("sha256"):
        msg = f"{model_path} does not match the manifest sha256"
        raise VadModelError(msg)
    return model_path


def _check_signature(session: Any) -> None:
    inputs = tuple(str(node.name) for node in session.get_inputs())
    outputs = tuple(str(node.name) for node in session.get_outputs())
    if inputs != INPUT_NAMES or outputs != OUTPUT_NAMES:
        msg = f"unexpected Silero VAD signature: inputs {inputs}, outputs {outputs}"
        raise VadModelError(msg)


class SileroModel:
    """The process-wide Silero VAD session (single-threaded per inference, sequential execution)."""

    __slots__ = ("_session",)

    def __init__(self, session: Any) -> None:
        # ``session`` is an onnxruntime.InferenceSession (onnxruntime ships no type information).
        _check_signature(session)
        self._session = session

    @classmethod
    def load(cls, models_dir: Path) -> SileroModel:
        """Verify ``models_dir`` against its manifest, then open the model on the CPU provider.

        Blocking (hashes and parses the file): call it from a worker thread or at startup before serving.
        """
        model_path = verify_model_file(models_dir)
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.log_severity_level = _ORT_LOG_ERROR
        try:
            session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        except Exception as exc:
            msg = f"{model_path} could not be loaded by onnxruntime"
            raise VadModelError(msg) from exc
        return cls(session)

    def infer(
        self, samples: NDArray[np.float32], state: NDArray[np.float32]
    ) -> tuple[float, NDArray[np.float32]]:
        """Run one 32 ms window of one stream (``samples`` = 64 context + 512 new samples, ``state``
        ``(2, 1, 128)``): a batch of one."""
        if samples.shape != (MODEL_INPUT_SAMPLES,) or state.shape != STATE_SHAPE:
            expected = f"expected samples {(MODEL_INPUT_SAMPLES,)} and state {STATE_SHAPE}"
            msg = f"{expected}, got {samples.shape}, {state.shape}"
            raise ValueError(msg)
        probabilities, next_state = self.infer_batch(samples.reshape(1, MODEL_INPUT_SAMPLES), state)
        return float(probabilities[0]), next_state

    def infer_batch(
        self, samples: NDArray[np.float32], state: NDArray[np.float32]
    ) -> tuple[NDArray[np.float32], NDArray[np.float32]]:
        """One ``session.run`` over ``B`` streams; see :class:`VadModel`."""
        batch = samples.shape[0] if samples.ndim == _BATCH_DIMS else 0
        state_shape = (STATE_LAYERS, batch, STATE_UNITS)
        if batch < 1 or samples.shape[1] != MODEL_INPUT_SAMPLES or state.shape != state_shape:
            expected = f"expected samples (B, {MODEL_INPUT_SAMPLES}) and state (2, B, 128) with B >= 1"
            msg = f"{expected}, got {samples.shape}, {state.shape}"
            raise ValueError(msg)
        feeds = {
            "input": np.ascontiguousarray(samples, dtype=np.float32),
            "state": np.ascontiguousarray(state, dtype=np.float32),
            "sr": _SAMPLE_RATE_INPUT,
        }
        output, next_state = self._session.run(list(OUTPUT_NAMES), feeds)
        probabilities: NDArray[np.float32] = np.asarray(output, dtype=np.float32).reshape(batch)
        updated: NDArray[np.float32] = np.asarray(next_state, dtype=np.float32).reshape(state_shape)
        return probabilities, updated
