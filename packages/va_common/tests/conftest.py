"""Isolate tests from the developer environment, any repository `.env`, and each other's log config."""

from __future__ import annotations

from pathlib import Path

import pytest
from service_helpers import isolate_env, reset_logging

from va_common.settings import CryptoSettings, DatabaseSettings, RedisSettings, TemporalSettings

__all__ = ["reset_logging"]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("POD_ID", raising=False)
    isolate_env(monkeypatch, tmp_path, CryptoSettings, RedisSettings, DatabaseSettings, TemporalSettings)
