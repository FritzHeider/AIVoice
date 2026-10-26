"""Shared API test helpers: settings without `.env`, fake dependency containers, and an ASGI test client."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from service_helpers import FakeContainer, failing, make_client, ok_check, settings_of

from va_api.settings import ApiSettings
from va_common.health import CheckFn

__all__ = ["FakeChecks", "failing", "make_client", "make_settings", "ok_check", "sample_app"]


def make_settings(**overrides: Any) -> ApiSettings:
    return settings_of(ApiSettings, **overrides)


@dataclass
class FakeChecks(FakeContainer):
    """The api's default fake container (Postgres, Redis, Temporal all ok); counts readiness lookups."""

    checks: Mapping[str, CheckFn] = field(
        default_factory=lambda: {"postgres": ok_check, "redis": ok_check, "temporal": ok_check}
    )
    calls: int = 0

    def readiness_checks(self) -> dict[str, CheckFn]:
        self.calls += 1
        return super().readiness_checks()


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
