"""Isolate settings tests from the developer's environment and any `.env` in the repository."""

from __future__ import annotations

from pathlib import Path

import pytest

SETTINGS_ENV_VARS = (
    "VA_ENV",
    "LOG_LEVEL",
    "LOG_TRANSCRIPTS",
    "LOG_FORMAT",
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_TRACES_SAMPLER_ARG",
    "SENTRY_DSN",
)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in SETTINGS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
