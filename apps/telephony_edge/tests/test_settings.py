"""Edge settings defaults (specs/02-repo-layout-and-conventions.md §6)."""

from __future__ import annotations

import pytest
from service_helpers import settings_of

from va_edge.settings import EdgeSettings


def test_defaults_match_spec() -> None:
    settings = settings_of(EdgeSettings)
    assert settings.service_name == "telephony-edge"
    assert settings.database_url == "postgresql+asyncpg://va:va@localhost:5432/va"
    assert settings.database_pool_size == 10
    assert settings.redis_url == "redis://localhost:6379/0"
    assert (settings.temporal_host, settings.temporal_namespace) == ("localhost:7233", "default")
    assert settings.temporal_task_queue == "va-main"
    assert (settings.edge_public_host, settings.media_public_host) == ("localhost:8100", "localhost:8200")


def test_environment_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EDGE_PUBLIC_HOST", "edge.example.ngrok.app")
    monkeypatch.setenv("DATABASE_POOL_SIZE", "3")
    settings = settings_of(EdgeSettings)
    assert settings.edge_public_host == "edge.example.ngrok.app"
    assert settings.database_pool_size == 3
