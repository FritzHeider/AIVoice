"""Helpers shared by every service's tests: isolated settings, fake containers, and an ASGI test client.

Service test suites import their autouse fixtures from here (``from service_helpers import reset_logging``)
and build settings with :func:`settings_of` so a developer ``.env`` never leaks into tests.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Final, Self

import httpx
import pytest
import structlog
import uvicorn
from asgi_lifespan import LifespanManager
from pydantic_settings import BaseSettings
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from va_common.health import CheckFn
from va_common.logging import configure_logging
from va_common.service import Bind, ManagedServer
from va_common.settings import BaseServiceSettings


def settings_of[S: BaseSettings](cls: type[S], **overrides: Any) -> S:
    """``cls`` built from ``overrides`` and the environment only (no ``.env`` file)."""
    kwargs: dict[str, Any] = {"_env_file": None, **overrides}
    return cls(**kwargs)


def isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *classes: type[BaseSettings]) -> None:
    """Unset the environment variable of every field of ``classes`` and ``chdir`` to ``tmp_path``."""
    for cls in classes:
        for name in cls.model_fields:
            monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.chdir(tmp_path)


# Dependency URLs nothing listens on (port 1 refuses connections immediately).
UNREACHABLE: Final[Mapping[str, str]] = {
    "database_url": "postgresql+asyncpg://va:va@127.0.0.1:1/va",
    "redis_url": "redis://127.0.0.1:1/0",
    "temporal_host": "127.0.0.1:1",
    "clickhouse_url": "http://127.0.0.1:1/va",
}


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


async def ok_check() -> None:
    return None


def failing(exc: Exception) -> Callable[[], Awaitable[None]]:
    async def check() -> None:
        raise exc

    return check


@dataclass
class FakeContainer:
    """A container factory whose readiness checks are the given callables; records its lifecycle."""

    checks: Mapping[str, CheckFn] = field(default_factory=dict)
    opened: int = 0
    closed: int = 0

    def readiness_checks(self) -> dict[str, CheckFn]:
        return dict(self.checks)

    @asynccontextmanager
    async def __call__(self, _settings: object) -> AsyncIterator[Self]:
        self.opened += 1
        try:
            yield self
        finally:
            self.closed += 1


@asynccontextmanager
async def make_client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield a client; app exceptions become 500 responses, as under uvicorn."""
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def json_log_stream() -> io.StringIO:
    """Configure JSON logging into a fresh stream (pair with the ``reset_logging`` fixture)."""
    stream = io.StringIO()
    configure_logging(settings_of(BaseServiceSettings, log_format="json"), stream=stream, environ={}, pod="p")
    return stream


def log_events(stream: io.StringIO) -> list[dict[str, Any]]:
    """The structured log lines written to ``stream`` (non-JSON lines skipped)."""
    events: list[dict[str, Any]] = []
    for line in stream.getvalue().splitlines():
        if line.startswith("{"):
            events.append(json.loads(line))
    return events


def capture_serve(
    monkeypatch: pytest.MonkeyPatch, module: ModuleType
) -> list[tuple[Any, Bind, dict[str, Any]]]:
    """Replace ``module.serve`` with a recorder of ``(app, bind, kwargs)``; it returns exit code 0."""
    seen: list[tuple[Any, Bind, dict[str, Any]]] = []

    def fake_serve(app: Any, bind: Bind, **kwargs: Any) -> int:
        seen.append((app, bind, kwargs))
        return 0

    monkeypatch.setattr(module, "serve", fake_serve)
    return seen


class RecordingServer(ManagedServer):
    """Signals readiness once uvicorn has bound its socket."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.ready = asyncio.Event()

    async def startup(self, sockets: Any = None) -> None:
        await super().startup(sockets)
        if self.started:
            self.ready.set()

    @property
    def url(self) -> str:
        host, port = self.servers[0].sockets[0].getsockname()[:2]
        return f"http://{host}:{port}"


def track_closes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record ``engine``/``redis``/``http`` each time an engine, Redis client, or HTTP client is closed."""
    closed: list[str] = []
    real_dispose, real_aclose, real_http_aclose = AsyncEngine.dispose, Redis.aclose, httpx.AsyncClient.aclose

    async def dispose(self: AsyncEngine, close: bool = True) -> None:
        closed.append("engine")
        await real_dispose(self, close)

    async def aclose(self: Redis, close_connection_pool: bool | None = None) -> None:
        closed.append("redis")
        await real_aclose(self, close_connection_pool)

    async def http_aclose(self: httpx.AsyncClient) -> None:
        closed.append("http")
        await real_http_aclose(self)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    monkeypatch.setattr(Redis, "aclose", aclose)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", http_aclose)
    return closed


async def readiness_of(app: Any) -> tuple[int, dict[str, Any]]:
    """``(status, body)`` of ``GET /readyz`` with the app's lifespan running."""
    async with make_client(app) as client:
        response = await client.get("/readyz")
    body: dict[str, Any] = response.json()
    return response.status_code, body
