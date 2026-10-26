"""Telephony edge test fixtures: isolated environment and log reset."""

from __future__ import annotations

from pathlib import Path

import pytest
from service_helpers import isolate_env, reset_logging

from va_edge.settings import EdgeSettings

__all__ = ["reset_logging"]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    isolate_env(monkeypatch, tmp_path, EdgeSettings)
