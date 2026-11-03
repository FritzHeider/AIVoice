"""Webhook dispatcher settings, container, probes (specs/27-observability.md §9), and entry point."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

import pytest
from service_helpers import (
    UNREACHABLE,
    FakeContainer,
    capture_serve,
    make_client,
    ok_check,
    settings_of,
    track_closes,
)

from va_common.email import InMemoryEmailSender, SmtpEmailSender
from va_common.service import Bind
from va_common.settings import Environment
from va_common.time import SystemClock
from va_webhooks import __version__
from va_webhooks import main as dispatcher_main
from va_webhooks.container import DispatcherContainer, build_container, email_sender_for
from va_webhooks.dispatcher import Dispatcher
from va_webhooks.main import DISPATCH_TASK, create_app
from va_webhooks.settings import DispatcherSettings


def keyed(**overrides: Any) -> DispatcherSettings:
    return settings_of(DispatcherSettings, local_master_key=secrets.token_hex(32), **overrides)


def test_defaults_match_spec() -> None:
    settings = settings_of(DispatcherSettings)
    assert settings.service_name == "webhook-dispatcher"
    assert settings.webhook_allow_private is True
    assert settings.database_pool_size == 10
    assert settings.dashboard_base_url == "http://localhost:3000"
    assert settings.kms_provider.value == "local"
    assert settings.smtp_url.get_secret_value() == "smtp://localhost:1025"


@pytest.mark.parametrize(
    ("env", "expected"), [("local", True), ("test", True), ("staging", False), ("production", False)]
)
def test_private_targets_forced_off_outside_dev(
    monkeypatch: pytest.MonkeyPatch, env: str, expected: bool
) -> None:
    monkeypatch.setenv("VA_ENV", env)
    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE", "true")
    monkeypatch.setenv("SMTP_URL", "smtps://relay.voiceagents.test:465")
    if env == "production":
        monkeypatch.setenv("KMS_PROVIDER", "aws")
        monkeypatch.setenv("AWS_KMS_KEY_ID", "arn:aws:kms:us-east-1:111122223333:key/test")
    assert settings_of(DispatcherSettings).webhook_allow_private is expected


def test_private_targets_can_be_disabled_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WEBHOOK_ALLOW_PRIVATE", "false")
    assert settings_of(DispatcherSettings).webhook_allow_private is False


def test_email_sender_is_in_memory_only_under_test() -> None:
    clock = SystemClock()
    assert isinstance(email_sender_for(keyed(va_env=Environment.TEST), clock), InMemoryEmailSender)
    assert isinstance(email_sender_for(keyed(va_env=Environment.LOCAL), clock), SmtpEmailSender)


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
    assert "# TYPE va_webhook_deliveries_total counter" in metrics.text
    assert container.closed == 1


async def test_readyz_postgres_unreachable_503_and_redis_not_checked() -> None:
    settings = keyed(database_url=UNREACHABLE["database_url"], redis_url=UNREACHABLE["redis_url"])
    app = create_app(settings, init_observability=False)
    async with make_client(app) as client:
        response = await client.get("/readyz")
        running = app.state.dispatch.task is not None and not app.state.dispatch.task.done()
    assert response.status_code == 503
    assert response.json()["checks"] == {"postgres": "error"}
    assert running
    assert app.state.dispatch.task.get_name() == DISPATCH_TASK


async def test_build_container_checks_postgres_only_and_closes_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = track_closes(monkeypatch)
    async with build_container(keyed()) as container:
        assert isinstance(container, DispatcherContainer)
        assert isinstance(container.dispatcher, Dispatcher)
        assert list(container.readiness_checks()) == ["postgres"]
    assert {"engine", "redis", "http"} <= set(closed)


async def test_build_container_refuses_without_a_master_key() -> None:
    with pytest.raises(RuntimeError, match="LOCAL_MASTER_KEY"):
        async with build_container(settings_of(DispatcherSettings)):
            pytest.fail("the container must not open")


async def test_drain_stops_the_loop() -> None:
    settings = keyed(database_url=UNREACHABLE["database_url"], redis_url=UNREACHABLE["redis_url"])
    app = create_app(settings, init_observability=False)
    async with make_client(app):
        task = app.state.dispatch.task
        async with asyncio.timeout(10):
            await app.state.dispatch.drain()
        assert task.done()
        assert task.exception() is None


def test_main_parses_args_and_serves_with_drain(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = capture_serve(monkeypatch, dispatcher_main)
    assert dispatcher_main.main(["--host", "127.0.0.1"]) == 0
    ((app, bind, kwargs),) = seen
    assert bind == Bind("127.0.0.1", 9400)
    assert kwargs["drain"] == app.state.dispatch.drain
    assert kwargs["stop"] is app.state.stop
