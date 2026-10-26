"""One-call observability bootstrap (specs/27-observability.md header, §1, §2, §3, §8).

Every service calls :func:`init` at startup and :meth:`Observability.shutdown` on graceful shutdown. ``init``
configures structured logging, the OpenTelemetry tracer provider, the service metrics registry, and Sentry
(only when ``SENTRY_DSN`` is set).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, TextIO, cast

import sentry_sdk
import structlog
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration
from sentry_sdk.types import Event, Hint

from va_common.logging import RedactionPolicy, configure_logging, redact
from va_common.masking import mask_phones_in_text
from va_common.metrics import Service, ServiceMetrics, build_metrics
from va_common.settings import BaseServiceSettings
from va_common.tracing import setup_tracing, shutdown_tracing

__all__ = ["SENTRY_FLUSH_TIMEOUT_S", "Observability", "init", "init_sentry", "sentry_before_send"]

SENTRY_FLUSH_TIMEOUT_S: Final = 2.0
_CONTEXT_TAGS: Final = ("org_id", "call_id")
_USER_PII_KEYS: Final = ("email", "ip_address")
_MAPPING_SECTIONS: Final = ("extra", "contexts", "request", "logentry")
_STACK_SECTIONS: Final = ("exception", "threads")


@dataclass(slots=True)
class Observability:
    """Handles created by :func:`init`."""

    tracer_provider: TracerProvider
    metrics: ServiceMetrics | None
    sentry_enabled: bool

    def shutdown(self) -> None:
        """Flush and stop tracing, then flush Sentry when it was enabled."""
        shutdown_tracing(self.tracer_provider)
        if self.sentry_enabled:
            sentry_sdk.flush(timeout=SENTRY_FLUSH_TIMEOUT_S)


def init(  # noqa: PLR0913 - test seams (processor, globals, environ, stream) are keyword-only
    service_name: str,
    settings: BaseServiceSettings,
    *,
    version: str = "unknown",
    service: Service | None = None,
    span_processor: SpanProcessor | None = None,
    set_global: bool = True,
    environ: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> Observability:
    """Configure logging, tracing, metrics (when ``service`` is given), and Sentry for this process."""
    configure_logging(settings, version=version, stream=stream, environ=environ)
    provider = setup_tracing(
        service_name,
        settings,
        version=version,
        span_processor=span_processor,
        set_global=set_global,
        environ=environ,
    )
    metrics = build_metrics(service) if service is not None else None
    return Observability(
        tracer_provider=provider, metrics=metrics, sentry_enabled=init_sentry(settings, version=version)
    )


def init_sentry(settings: BaseServiceSettings, *, version: str) -> bool:
    """Initialise ``sentry-sdk`` when ``SENTRY_DSN`` is set; return whether it was initialised.

    Tracing stays with OpenTelemetry (``traces_sample_rate=0``); events pass through the log redaction and
    never carry user email or IP. The FastAPI integration enables itself when FastAPI is installed.
    """
    dsn = settings.sentry_dsn.strip()
    if not dsn:
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=settings.va_env.value,
        release=version,
        traces_sample_rate=0.0,
        send_default_pii=False,
        integrations=[AsyncioIntegration(), HttpxIntegration()],
        before_send=sentry_before_send(RedactionPolicy.from_settings(settings)),
    )
    return True


def _redact_section(event: Event, key: str, policy: RedactionPolicy) -> None:
    fields = cast("dict[str, Any]", event)
    section = fields.get(key)
    if isinstance(section, Mapping):
        fields[key] = redact(section, policy)


def _redact_message(event: Event, policy: RedactionPolicy) -> None:
    message = event.get("message")
    if isinstance(message, str) and policy.mask_phones:
        event["message"] = mask_phones_in_text(message)


def _redact_frames(stacktrace: object, policy: RedactionPolicy) -> None:
    frames = stacktrace.get("frames") if isinstance(stacktrace, Mapping) else None
    if not isinstance(frames, list):
        return
    for frame in frames:
        if isinstance(frame, dict) and isinstance(frame.get("vars"), Mapping):
            frame["vars"] = redact(frame["vars"], policy)


def _redact_stack_entry(entry: dict[str, Any], policy: RedactionPolicy) -> None:
    value = entry.get("value")
    if isinstance(value, str) and policy.mask_phones:
        entry["value"] = mask_phones_in_text(value)
    _redact_frames(entry.get("stacktrace"), policy)


def _stack_values(event: Event, key: str) -> list[Any]:
    section = cast("dict[str, Any]", event).get(key)
    values = section.get("values") if isinstance(section, Mapping) else None
    return values if isinstance(values, list) else []


def _redact_stacks(event: Event, policy: RedactionPolicy) -> None:
    for key in _STACK_SECTIONS:
        for entry in _stack_values(event, key):
            if isinstance(entry, dict):
                _redact_stack_entry(entry, policy)


def _redact_breadcrumbs(event: Event, policy: RedactionPolicy) -> None:
    breadcrumbs = event.get("breadcrumbs")
    values = breadcrumbs.get("values") if isinstance(breadcrumbs, Mapping) else breadcrumbs
    if not isinstance(values, list):
        return
    for index, crumb in enumerate(values):
        if isinstance(crumb, Mapping):
            values[index] = redact(crumb, policy)


def _strip_user_pii(event: Event) -> None:
    user = event.get("user")
    if isinstance(user, dict):
        for key in _USER_PII_KEYS:
            user.pop(key, None)


def _tag_context(event: Event) -> None:
    bound = structlog.contextvars.get_contextvars()
    tags: dict[str, Any] = dict(event.get("tags") or {})
    for key in _CONTEXT_TAGS:
        if key in bound:
            tags.setdefault(key, str(bound[key]))
    if tags:
        event["tags"] = tags


def sentry_before_send(policy: RedactionPolicy) -> Callable[[Event, Hint], Event | None]:
    """Sentry ``before_send`` hook (specs/27-observability.md §8).

    Applies the log redaction to ``extra``, ``contexts``, ``request``, ``logentry`` (message and params),
    breadcrumbs, and every exception/thread stack frame's local ``vars``; masks phone numbers in ``message``
    and each exception ``value``; drops user email/IP; and tags the bound ``org_id``/``call_id``. Structural
    fields (exception ``type``/``module``, frame ``filename``/``function``/``lineno``) are left untouched.
    """

    def before_send(event: Event, _hint: Hint) -> Event | None:
        for key in _MAPPING_SECTIONS:
            _redact_section(event, key, policy)
        _redact_message(event, policy)
        _redact_stacks(event, policy)
        _redact_breadcrumbs(event, policy)
        _strip_user_pii(event)
        _tag_context(event)
        return event

    return before_send
