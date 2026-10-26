"""Silero VAD model verification for the readiness check ``vad`` (specs/27-observability.md §9).

The model file must exist next to ``MANIFEST.json`` (written by ``scripts/fetch_models.sh``) and match its
pinned sha256. Hashing runs in a worker thread so the event loop never blocks.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

__all__ = ["MANIFEST_NAME", "MODEL_NAME", "VadModelError", "VadStatus", "verify_vad_model"]

MODEL_NAME: Final = "silero_vad"
MANIFEST_NAME: Final = "MANIFEST.json"


class VadModelError(RuntimeError):
    """The VAD model is missing, unlisted in the manifest, or does not match its checksum."""


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


def _verify(models_dir: Path) -> None:
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


async def verify_vad_model(models_dir: Path) -> None:
    """Raise :class:`VadModelError` unless the model in ``models_dir`` matches ``MANIFEST.json``."""
    await asyncio.to_thread(_verify, models_dir)


@dataclass(slots=True)
class VadStatus:
    loaded: bool = False
    error: str | None = None

    async def check(self) -> None:
        """Readiness check ``vad``: fails until the model was verified."""
        if not self.loaded:
            raise VadModelError(self.error or "VAD model not loaded yet")
