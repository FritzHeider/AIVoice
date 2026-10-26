"""Media worker test fixtures: isolated environment, log reset, and a verified VAD model directory."""

from __future__ import annotations

from pathlib import Path

import pytest
from media_helpers import write_models_dir
from service_helpers import isolate_env, reset_logging

from va_media.settings import MediaSettings

__all__ = ["reset_logging"]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_env(monkeypatch, tmp_path, MediaSettings)


@pytest.fixture
def models_dir(tmp_path: Path) -> Path:
    return write_models_dir(tmp_path / "models", b"onnx-model-bytes")
