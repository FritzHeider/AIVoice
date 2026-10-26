"""Webhook dispatcher settings, probes (specs/27-observability.md §9: Postgres only), and entry point."""

from __future__ import annotations

from typing import Any

import pytest
from service_helpers import UNREACHABLE, FakeContainer, capture_serve, make_client, ok_check, settings_of

from va_common.service import Bind
from va_webhooks import __version__
from va_webhooks import main as dispatcher_main
from va_webhooks.main import build_container, create_app
from va_webhooks.settings import DispatcherSettings


def test_defaults_match_spec() -> None:
    settings = settings_of(DispatcherSettings)
    assert settings.service_name == "webhook-dispatcher"
    assert settings.webhook_allow_private is True
    assert settings.database_pool_size == 10


@pytest.mark.parametrize(
    ("env", "expected"), [("local", True), ("test", True), ("staging", False), ("production", False)]
)
def test_private_targets_forced_off_outside_dev(
    monkeypatch: pytest.MonkeyPatch, env: str, expected: bool
) -> None:
    monkeypatch.setenv("VA_ENV", env)
    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE", "true")
    assert settings_of(DispatcherSettings).webhook_allow_private is expected


def test_private_targets_can_be_disabled_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE", "false")
    assert settings_of(DispatcherSettings).webhook_allow_private is False


def dispatcher_app(container: FakeContainer) -> Any:
    return create_app(settings_of(DispatcherSettings), container_factory=container, init_observability=False)


async def test_probes_and_metrics() -> None:
    container = FakeContainer({"postgres": ok_check})
    async with make_client(dispatcher_app(container)) as client:
        live, ready = await client.get("/healthz"), await client.get("/readyz")
        metrics = await client.get("/metrics")
    assert live.json() == {"status": "ok", "version": __version__}
    assert ready.json() == {"status": "ok", "checks": {"postgres": "ok"}, "version": __version__}
    assert "# TYPE va_webhook_backlog gauge" in metrics.text
    assert container.closed == 1


async def test_readyz_postgres_unreachable_503_and_redis_not_checked() -> None:
    settings = settings_of(
        DispatcherSettings, database_url=UNREACHABLE["database_url"], redis_url=UNREACHABLE["redis_url"]
    )
    async with make_client(create_app(settings, init_observability=False)) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"] == {"postgres": "error"}


async def test_build_container_checks_postgres_only() -> None:
    async with build_container(settings_of(DispatcherSettings)) as deps:
        assert list(deps.readiness_checks()) == ["postgres"]


def test_main_parses_args_and_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = capture_serve(monkeypatch, dispatcher_main)
    assert dispatcher_main.main(["--host", "127.0.0.1"]) == 0
    assert [bind for _, bind, _ in seen] == [Bind("127.0.0.1", 9400)]
