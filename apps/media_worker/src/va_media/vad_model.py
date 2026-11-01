"""Silero VAD model verification for the readiness check ``vad`` (specs/27-observability.md §9).

The model file must exist next to ``MANIFEST.json`` (written by ``scripts/fetch_models.sh``) and match its
pinned sha256 (``va_voice.vad_model.verify_model_file``). Hashing runs in a worker thread so the event loop
never blocks.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from va_voice.vad_model import MANIFEST_NAME, MODEL_NAME, VadModelError, verify_model_file

__all__ = ["MANIFEST_NAME", "MODEL_NAME", "VadModelError", "VadStatus", "verify_vad_model"]


async def verify_vad_model(models_dir: Path) -> None:
    """Raise :class:`VadModelError` unless the model in ``models_dir`` matches ``MANIFEST.json``."""
    await asyncio.to_thread(verify_model_file, models_dir)


@dataclass(slots=True)
class VadStatus:
    loaded: bool = False
    error: str | None = None

    async def check(self) -> None:
        """Readiness check ``vad``: fails until the model was verified."""
        if not self.loaded:
            raise VadModelError(self.error or "VAD model not loaded yet")
