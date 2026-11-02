"""Telnyx REST settings and the shared client pool (specs/02-repo-layout-and-conventions.md §6 rows
``TELNYX_API_BASE``/``TELNYX_API_KEY``; specs/06-telephony-telnyx.md §2).

Mixed into the settings of every service that sends Call Control commands (worker, media-worker). Defaults are
the ``.env.example`` dev values (FakeTelnyx on port 9100). Staging and production refuse an empty or simulator
platform key at construction; errors name the variable, never its value.

:func:`telnyx_http` builds the shared HTTP/2 pool of specs/06 §2 and :func:`telnyx_factory` the
:class:`~va_telnyx.factory.TelnyxClientFactory` over it (platform key, BYO keys through a resolver, one shared
rate limiter).
"""

from __future__ import annotations

from typing import Final

import httpx
from pydantic import SecretStr, model_validator
from pydantic_settings import SettingsConfigDict
from redis.asyncio import Redis

from va_common.http import http_client
from va_common.settings import BaseServiceSettings, Environment
from va_common.time import Clock
from va_telnyx.factory import KeyResolver, TelnyxClientFactory
from va_telnyx.rate_limit import TelnyxRateLimiter

__all__ = [
    "SIM_TELNYX_API_KEY",
    "TELNYX_HTTP_TIMEOUT",
    "TELNYX_MAX_CONNECTIONS",
    "TELNYX_MAX_KEEPALIVE",
    "TelnyxApiSettings",
    "telnyx_factory",
    "telnyx_http",
]

SIM_TELNYX_API_KEY: Final = "sim-telnyx-key"
"""The FakeTelnyx platform key (the ``TELNYX_API_KEY`` dev value); refused in staging and production."""

TELNYX_HTTP_TIMEOUT: Final = httpx.Timeout(connect=2.0, read=8.0, write=5.0, pool=2.0)
TELNYX_MAX_CONNECTIONS: Final = 200
TELNYX_MAX_KEEPALIVE: Final = 50

_DEPLOYED: Final = frozenset({Environment.STAGING, Environment.PRODUCTION})


class TelnyxApiSettings(BaseServiceSettings):
    """The Telnyx REST base URL and the platform master key (validation errors never echo the input)."""

    model_config = SettingsConfigDict(hide_input_in_errors=True)

    telnyx_api_base: str = "http://localhost:9100/v2"
    telnyx_api_key: SecretStr = SecretStr(SIM_TELNYX_API_KEY)

    @model_validator(mode="after")
    def _require_platform_key_outside_dev(self) -> TelnyxApiSettings:
        if self.va_env not in _DEPLOYED:
            return self
        key = self.telnyx_api_key.get_secret_value().strip()
        if not key:
            msg = f"TELNYX_API_KEY must be configured when VA_ENV={self.va_env.value}"
            raise ValueError(msg)
        if key == SIM_TELNYX_API_KEY:
            msg = f"TELNYX_API_KEY must not be the simulator key when VA_ENV={self.va_env.value}"
            raise ValueError(msg)
        return self


def telnyx_http(settings: BaseServiceSettings) -> httpx.AsyncClient:
    """The shared Telnyx pool: HTTP/2, the specs/06 §2 timeouts and limits; the caller closes it."""
    return http_client(
        settings,
        timeout=TELNYX_HTTP_TIMEOUT,
        http2=True,
        max_connections=TELNYX_MAX_CONNECTIONS,
        max_keepalive=TELNYX_MAX_KEEPALIVE,
    )


def telnyx_factory(
    settings: TelnyxApiSettings,
    *,
    http: httpx.AsyncClient,
    redis: Redis,
    resolve_key: KeyResolver,
    clock: Clock,
) -> TelnyxClientFactory:
    """Platform client on ``TELNYX_API_KEY``, BYO clients through ``resolve_key``, one shared rate limiter.

    No Telnyx metrics: they are not in the worker or media-worker catalogs (specs/27 §3) yet.
    """
    return TelnyxClientFactory(
        base_url=settings.telnyx_api_base,
        http=http,
        limiter=TelnyxRateLimiter(redis, clock=clock),
        platform_api_key=settings.telnyx_api_key.get_secret_value(),
        resolve_key=resolve_key,
        clock=clock,
    )
