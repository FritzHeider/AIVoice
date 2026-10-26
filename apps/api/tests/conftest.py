"""API test fixtures: isolated environment, log reset, and default settings."""

from __future__ import annotations

from pathlib import Path

import pytest
from api_helpers import make_settings
from service_helpers import isolate_env, reset_logging

from va_api.settings import ApiSettings

__all__ = ["reset_logging"]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("POD_ID", raising=False)
    isolate_env(monkeypatch, tmp_path, ApiSettings)


@pytest.fixture
def settings() -> ApiSettings:
    return make_settings()
