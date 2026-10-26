"""API test fixtures: isolated environment, log reset, and default settings."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from api_helpers import make_settings

from va_api.settings import ApiSettings

SETTINGS_ENV_VARS = (
    "VA_ENV",
    "LOG_LEVEL",
    "LOG_TRANSCRIPTS",
    "LOG_FORMAT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_TRACES_SAMPLER_ARG",
    "SENTRY_DSN",
    "POD_ID",
    "REDIS_URL",
    "DATABASE_URL",
    "DATABASE_POOL_SIZE",
    "TEMPORAL_HOST",
    "TEMPORAL_NAMESPACE",
    "PUBLIC_API_BASE_URL",
    "DASHBOARD_BASE_URL",
    "OPENAPI_PUBLIC_SERVER_URL",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture(autouse=True)
def reset_logging() -> Iterator[None]:
    """Undo `configure_logging`: structlog defaults, bound context, the root handler, and the root level."""
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    for handler in [h for h in root.handlers if h not in handlers]:
        root.removeHandler(handler)
        handler.close()
    root.setLevel(level)


@pytest.fixture
def settings() -> ApiSettings:
    return make_settings()
