"""Isolate tests from the developer environment, any repository `.env`, and each other's log config."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

SETTINGS_ENV_VARS = (
    "VA_ENV",
    "LOG_LEVEL",
    "LOG_TRANSCRIPTS",
    "LOG_FORMAT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_TRACES_SAMPLER_ARG",
    "SENTRY_DSN",
    "POD_ID",
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
