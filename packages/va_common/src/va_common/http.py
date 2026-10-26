"""Outbound HTTP client factory (MASTER_PROMPT §4.7 network timeouts, specs/27-observability.md §2).

Every service builds its ``httpx.AsyncClient`` with :func:`http_client`: the timeout is a required argument,
redirects are never followed implicitly, connection limits are explicit, and requests are traced with W3C
``traceparent`` propagation. Never construct ``httpx.AsyncClient`` directly.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import httpx
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.trace import TracerProvider

from va_common.settings import BaseServiceSettings

__all__ = ["DEFAULT_CONNECT_TIMEOUT_S", "build_timeout", "http_client", "user_agent"]

DEFAULT_CONNECT_TIMEOUT_S: Final = 5.0


def build_timeout(timeout: float | httpx.Timeout) -> httpx.Timeout:
    """Turn a total timeout in seconds into an ``httpx.Timeout`` whose connect phase is capped at 5 s."""
    if isinstance(timeout, httpx.Timeout):
        return timeout
    if timeout <= 0:
        msg = f"timeout must be positive, got {timeout}"
        raise ValueError(msg)
    return httpx.Timeout(timeout, connect=min(timeout, DEFAULT_CONNECT_TIMEOUT_S))


def user_agent(settings: BaseServiceSettings, version: str) -> str:
    """``voiceagents-<service>/<version>``."""
    return f"voiceagents-{settings.service_name}/{version}"


def http_client(  # noqa: PLR0913 - one keyword per client knob keeps call sites explicit
    settings: BaseServiceSettings,
    *,
    timeout: float | httpx.Timeout,
    http2: bool = False,
    base_url: str = "",
    headers: Mapping[str, str] | None = None,
    max_connections: int = 100,
    max_keepalive: int = 20,
    transport: httpx.AsyncBaseTransport | None = None,
    tracer_provider: TracerProvider | None = None,
    version: str = "unknown",
) -> httpx.AsyncClient:
    """Build an instrumented ``httpx.AsyncClient``; the caller owns it and must ``aclose()`` it."""
    client = httpx.AsyncClient(
        timeout=build_timeout(timeout),
        http2=http2,
        base_url=base_url,
        headers={"User-Agent": user_agent(settings, version), **(headers or {})},
        limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_keepalive),
        follow_redirects=False,
        transport=transport,
    )
    HTTPXClientInstrumentor.instrument_client(client, tracer_provider=tracer_provider)
    return client
