"""Media worker test helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fakeredis
from redis.asyncio import Redis
from service_helpers import FakeContainer, ok_check, settings_of
from starlette.applications import Starlette

from va_common.health import CheckFn
from va_media.loop_lag import LoopLagMonitor
from va_media.main import create_app
from va_media.settings import MediaSettings


def write_models_dir(
    root: Path, model: bytes, *, sha256: str | None = None, name: str = "silero_vad"
) -> Path:
    """A models directory with ``silero_vad.onnx`` and a ``MANIFEST.json`` entry (``sha256`` overrides)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "silero_vad.onnx").write_bytes(model)
    entry = {
        "name": name,
        "path": "models/silero_vad.onnx",
        "sha256": sha256 if sha256 is not None else hashlib.sha256(model).hexdigest(),
    }
    (root / "MANIFEST.json").write_text(json.dumps({"version": 1, "models": [entry]}), encoding="utf-8")
    return root


@dataclass
class FakeMediaContainer(FakeContainer):
    """A media container with an in-memory Redis and a passing ``redis`` check by default."""

    checks: Mapping[str, CheckFn] = field(default_factory=lambda: {"redis": ok_check})
    redis: Redis = field(default_factory=fakeredis.FakeAsyncRedis)


def media_app(
    models_dir: Path,
    container: FakeMediaContainer | None = None,
    *,
    monitor: LoopLagMonitor | None = None,
    **overrides: Any,
) -> Starlette:
    """The media app with fake dependencies, no observability, and an idle lag monitor (1 h ticks)."""
    return create_app(
        settings_of(MediaSettings, pod_id="pod-test", **overrides),
        container_factory=container if container is not None else FakeMediaContainer(),
        init_observability=False,
        models_dir=models_dir,
        monitor=monitor if monitor is not None else LoopLagMonitor(interval_s=3600),
    )
