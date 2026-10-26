"""Application lifespan: observability and the dependency container (specs/01-architecture.md §1)."""

from __future__ import annotations

import importlib
import importlib.metadata
import socket
from typing import Any

import pytest
from api_helpers import FakeChecks, make_client, make_settings

import va_api
from va_api import main
from va_api.main import create_app
from va_common import observability
from va_common.observability import Observability


class FakeObservability:
    def __init__(self) -> None:
        self.shutdowns = 0

    def shutdown(self) -> None:
        self.shutdowns += 1


async def test_observability_init_and_shutdown_called(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    obs = FakeObservability()

    def fake_init(service_name: str, settings: object, **kwargs: Any) -> Observability | FakeObservability:
        calls.append((service_name, kwargs))
        return obs

    monkeypatch.setattr(observability, "init", fake_init)
    app = create_app(make_settings(), container_factory=FakeChecks())
    async with make_client(app) as client:
        assert (await client.get("/healthz")).status_code == 200
        assert obs.shutdowns == 0
    assert calls == [("api", {"version": va_api.__version__})]
    assert obs.shutdowns == 1


async def test_observability_shut_down_when_container_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    obs = FakeObservability()
    monkeypatch.setattr(observability, "init", lambda *_a, **_k: obs)

    def failing_factory(_settings: object) -> Any:
        msg = "cannot open"
        raise RuntimeError(msg)

    app = create_app(make_settings(), container_factory=failing_factory)
    with pytest.raises(RuntimeError, match="cannot open"):
        async with make_client(app):
            pass
    assert obs.shutdowns == 1


async def test_container_closed_on_shutdown() -> None:
    container = FakeChecks()
    app = create_app(make_settings(), container_factory=container, init_observability=False)
    async with make_client(app) as client:
        await client.get("/readyz")
        assert (container.opened, container.closed) == (1, 0)
        assert app.state.container is container
    assert (container.opened, container.closed) == (1, 1)


def test_module_app_imports_without_io(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_network(*_args: object, **_kwargs: object) -> None:
        msg = "network access at import"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", no_network)
    reloaded = importlib.reload(main)
    assert reloaded.app.title == "VoiceAgents API"
    assert reloaded.app.state.settings.service_name == "api"


def test_version_matches_metadata() -> None:
    assert va_api.__version__ == importlib.metadata.version("va-api")
    assert create_app(make_settings(), init_observability=False).version == va_api.__version__
