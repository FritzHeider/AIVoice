"""The api dependency container and readiness against unreachable services."""

from __future__ import annotations

import pytest
from api_helpers import make_settings
from service_helpers import readiness_of, track_closes

from va_api.container import Container, build_container
from va_api.main import create_app

UNREACHABLE_PG = "postgresql+asyncpg://va:va@127.0.0.1:1/va"
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"
UNREACHABLE_TEMPORAL = "127.0.0.1:1"


async def test_build_container_opens_and_closes(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = track_closes(monkeypatch)
    settings = make_settings(database_url=UNREACHABLE_PG, redis_url=UNREACHABLE_REDIS, database_pool_size=3)
    async with build_container(settings) as container:
        assert isinstance(container, Container)
        assert container.engine.pool.size() == 3  # type: ignore[attr-defined]
        assert container.temporal.host == settings.temporal_host
        assert container.temporal.namespace == settings.temporal_namespace
        assert set(container.readiness_checks()) == {"postgres", "redis", "temporal"}
        assert closed == []
    assert closed == ["redis", "engine"]


async def test_readyz_real_container_reports_unreachable() -> None:
    settings = make_settings(
        database_url=UNREACHABLE_PG, redis_url=UNREACHABLE_REDIS, temporal_host=UNREACHABLE_TEMPORAL
    )
    status, body = await readiness_of(create_app(settings, init_observability=False, readiness_timeout_s=1.0))
    assert status == 503
    assert body["failing"] == ["postgres", "redis", "temporal"]
    assert set(body["checks"].values()) <= {"error", "timeout"}
