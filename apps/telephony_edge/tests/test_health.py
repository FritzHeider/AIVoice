"""Edge probes and metrics (specs/27-observability.md §3.2, §9)."""

from __future__ import annotations

from typing import Any

import pytest
from service_helpers import (
    UNREACHABLE,
    FakeContainer,
    failing,
    make_client,
    ok_check,
    readiness_of,
    settings_of,
)

from va_common import observability
from va_edge import __version__
from va_edge.main import build_container, create_app
from va_edge.settings import EdgeSettings

ALL_OK = {"postgres": ok_check, "redis": ok_check, "temporal": ok_check}


def edge_app(container: FakeContainer | None = None, **kwargs: Any) -> Any:
    factory = container if container is not None else FakeContainer(ALL_OK)
    return create_app(
        settings_of(EdgeSettings), container_factory=factory, init_observability=False, **kwargs
    )


async def test_healthz_ok_without_dependencies() -> None:
    container = FakeContainer({"redis": failing(ConnectionError("down"))})
    async with make_client(edge_app(container)) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}
    assert __version__ == "0.1.0"


async def test_readyz_all_ok() -> None:
    async with make_client(edge_app()) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"] == {"postgres": "ok", "redis": "ok", "temporal": "ok"}


async def test_readyz_real_container_reports_unreachable_dependencies() -> None:
    settings = settings_of(EdgeSettings, **UNREACHABLE)
    status, body = await readiness_of(create_app(settings, init_observability=False))
    assert (status, body["failing"]) == (503, ["postgres", "redis", "temporal"])
    assert set(body["checks"].values()) <= {"error", "timeout"}


async def test_metrics_exposes_edge_catalog() -> None:
    async with make_client(edge_app()) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert "# TYPE va_telnyx_webhooks_total counter" in response.text


async def test_no_public_openapi() -> None:
    async with make_client(edge_app()) as client:
        assert (await client.get("/openapi.json")).status_code == 404
        assert (await client.get("/docs")).status_code == 404


async def test_container_closed_on_shutdown() -> None:
    container = FakeContainer(ALL_OK)
    async with make_client(edge_app(container)):
        assert (container.opened, container.closed) == (1, 0)
    assert container.closed == 1


async def test_build_container_registers_checks_in_spec_order() -> None:
    async with build_container(settings_of(EdgeSettings)) as deps:
        assert list(deps.readiness_checks()) == ["postgres", "redis", "temporal"]


async def test_observability_init_and_shutdown_called(monkeypatch: pytest.MonkeyPatch) -> None:
    names: list[str] = []
    shutdowns: list[bool] = []

    class Obs:
        def shutdown(self) -> None:
            shutdowns.append(True)

    def fake_init(name: str, _settings: object, **_kwargs: Any) -> Obs:
        names.append(name)
        return Obs()

    monkeypatch.setattr(observability, "init", fake_init)
    app = create_app(settings_of(EdgeSettings), container_factory=FakeContainer(ALL_OK))
    async with make_client(app):
        assert names == ["telephony-edge"]
    assert shutdowns == [True]
