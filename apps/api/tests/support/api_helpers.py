"""Shared API test helpers: settings without `.env`, fake dependency containers, and an ASGI test client."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from asgi_lifespan import LifespanManager
from fastapi import FastAPI

from va_api.settings import ApiSettings
from va_common.health import CheckFn


def make_settings(**overrides: Any) -> ApiSettings:
    kwargs: dict[str, Any] = {"_env_file": None, **overrides}
    return ApiSettings(**kwargs)


async def ok_check() -> None:
    return None


@dataclass
class FakeChecks:
    """A container factory whose readiness checks are the given callables; records lifecycle and calls."""

    checks: dict[str, CheckFn] = field(
        default_factory=lambda: {"postgres": ok_check, "redis": ok_check, "temporal": ok_check}
    )
    opened: int = 0
    closed: int = 0
    calls: int = 0

    def readiness_checks(self) -> dict[str, CheckFn]:
        self.calls += 1
        return dict(self.checks)

    @asynccontextmanager
    async def __call__(self, _settings: ApiSettings) -> AsyncIterator[FakeChecks]:
        self.opened += 1
        try:
            yield self
        finally:
            self.closed += 1


def failing(exc: Exception) -> Callable[[], Awaitable[None]]:
    async def check() -> None:
        raise exc

    return check


@asynccontextmanager
async def make_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan and yield a client; app exceptions become 500 responses, as under uvicorn."""
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


def sample_app(container: FakeChecks | None = None, **settings_overrides: Any) -> FastAPI:
    """The production app plus the test-only sample routers, with fake dependencies and no observability."""
    from sample_routes import envelope_sample, legacy_sample  # noqa: PLC0415 - avoids a helper import cycle

    from va_api.main import ROUTERS, create_app  # noqa: PLC0415

    return create_app(
        make_settings(**settings_overrides),
        container_factory=container if container is not None else FakeChecks(),
        init_observability=False,
        routers=(*ROUTERS, legacy_sample, envelope_sample),
    )
