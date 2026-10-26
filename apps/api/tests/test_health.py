"""Health endpoints and request metrics (specs/27-observability.md §3.1, §9)."""

from __future__ import annotations

import asyncio

from api_helpers import FakeChecks, failing, make_client, make_settings, ok_check, sample_app
from prometheus_client import CollectorRegistry

from va_api import __version__
from va_api.main import create_app


async def test_healthz_does_not_touch_dependencies() -> None:
    container = FakeChecks()
    async with make_client(sample_app(container)) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
    assert container.calls == 0


async def test_v1_health_body_exact() -> None:
    container = FakeChecks(checks={"redis": failing(ConnectionError("down"))})
    async with make_client(sample_app(container)) as client:
        response = await client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert container.calls == 0


async def test_readyz_all_ok() -> None:
    async with make_client(sample_app()) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "checks": {"postgres": "ok", "redis": "ok", "temporal": "ok"},
        "version": __version__,
    }


async def test_readyz_redis_down_503() -> None:
    container = FakeChecks(
        checks={
            "postgres": ok_check,
            "redis": failing(ConnectionError("10.0.0.7:6379 refused")),
            "temporal": ok_check,
        }
    )
    async with make_client(sample_app(container)) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body == {
        "status": "unavailable",
        "checks": {"postgres": "ok", "redis": "error", "temporal": "ok"},
        "failing": ["redis"],
        "version": __version__,
    }
    assert "refused" not in response.text
    assert response.headers["x-request-id"]


async def test_readyz_timeout() -> None:
    async def slow() -> None:
        await asyncio.Event().wait()

    container = FakeChecks(checks={"postgres": slow, "redis": ok_check})
    app = create_app(
        make_settings(), container_factory=container, init_observability=False, readiness_timeout_s=0.05
    )
    async with make_client(app) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == {"postgres": "timeout", "redis": "ok"}
    assert response.json()["failing"] == ["postgres"]


async def test_readyz_checks_run_concurrently() -> None:
    started: list[str] = []
    gate = asyncio.Event()

    async def make(name: str) -> None:
        started.append(name)
        if len(started) == 3:
            gate.set()
        await gate.wait()

    async def postgres() -> None:
        await make("postgres")

    async def redis() -> None:
        await make("redis")

    async def temporal() -> None:
        await make("temporal")

    container = FakeChecks(checks={"postgres": postgres, "redis": redis, "temporal": temporal})
    async with make_client(sample_app(container)) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert sorted(started) == ["postgres", "redis", "temporal"]


def sample(registry: CollectorRegistry, name: str, labels: dict[str, str]) -> float | None:
    return registry.get_sample_value(name, labels)


async def test_metrics_counts_requests() -> None:
    app = sample_app()
    async with make_client(app) as client:
        await client.get("/v1/health")
        await client.get("/v1/health")
        await client.get("/does/not/exist")
        await client.get("/v1/calls/_t/boom")
        exposition = await client.get("/metrics")
    registry = app.state.metrics.registry
    assert (
        sample(registry, "va_http_requests_total", {"route": "/v1/health", "method": "GET", "status": "200"})
        == 2
    )
    assert (
        sample(registry, "va_http_requests_total", {"route": "unmatched", "method": "GET", "status": "404"})
        == 1
    )
    assert (
        sample(
            registry,
            "va_http_requests_total",
            {"route": "/v1/calls/_t/boom", "method": "GET", "status": "500"},
        )
        == 1
    )
    assert (
        sample(registry, "va_http_request_duration_ms_count", {"route": "/v1/health", "method": "GET"}) == 2
    )
    assert exposition.status_code == 200
    assert exposition.headers["content-type"].startswith("text/plain")
    assert 'va_http_requests_total{method="GET",route="/v1/health",status="200"} 2.0' in exposition.text


async def test_probe_routes_not_in_schema() -> None:
    paths = sample_app().openapi()["paths"]
    for probe in ("/healthz", "/readyz", "/metrics"):
        assert probe not in paths
    assert "/v1/health" in paths
