"""Prometheus metrics catalog and registries (specs/27-observability.md §3).

:data:`CATALOG` holds every metric of specs/27 §3.1-§3.5 keyed by service. Each service process builds its own
``CollectorRegistry`` with :func:`build_metrics` and serves it at ``/metrics`` via :func:`metrics_app` or
:func:`render_latest`. The prometheus_client global ``REGISTRY`` is never used.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    GC_COLLECTOR,
    PLATFORM_COLLECTOR,
    PROCESS_COLLECTOR,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    make_asgi_app,
)
from prometheus_client.registry import Collector

__all__ = [
    "CATALOG",
    "LAT_MS",
    "ASGIApp",
    "MetricKind",
    "MetricSpec",
    "Service",
    "ServiceMetrics",
    "build_metrics",
    "metrics_app",
    "new_registry",
    "render_latest",
]

LAT_MS: Final[tuple[float, ...]] = (
    5,
    10,
    25,
    50,
    75,
    100,
    150,
    200,
    300,
    400,
    500,
    650,
    800,
    1000,
    1300,
    1600,
    2000,
    3000,
    5000,
    10000,
)

type ASGIApp = Callable[
    [
        MutableMapping[str, Any],
        Callable[[], Awaitable[MutableMapping[str, Any]]],
        Callable[[Any], Awaitable[None]],
    ],
    Awaitable[None],
]


class MetricKind(StrEnum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"


class Service(StrEnum):
    """Services exposing ``/metrics`` (specs/27-observability.md §3)."""

    API = "api"
    TELEPHONY_EDGE = "telephony_edge"
    MEDIA_WORKER = "media_worker"
    WORKER = "worker"
    WEBHOOK_DISPATCHER = "webhook_dispatcher"
    EVENTS_SINK = "events_sink"


@dataclass(frozen=True, slots=True)
class MetricSpec:
    """One catalog row: exposition name, type, label names, and help text."""

    name: str
    kind: MetricKind
    doc: str
    labels: tuple[str, ...] = ()


def _c(name: str, doc: str, *labels: str) -> MetricSpec:
    return MetricSpec(name, MetricKind.COUNTER, doc, labels)


def _g(name: str, doc: str, *labels: str) -> MetricSpec:
    return MetricSpec(name, MetricKind.GAUGE, doc, labels)


def _h(name: str, doc: str, *labels: str) -> MetricSpec:
    return MetricSpec(name, MetricKind.HISTOGRAM, doc, labels)


CATALOG: Final[Mapping[Service, tuple[MetricSpec, ...]]] = {
    Service.API: (
        _c("va_http_requests_total", "HTTP requests served", "route", "method", "status"),
        _h("va_http_request_duration_ms", "HTTP request duration in ms", "route", "method"),
        _c("va_calls_created_total", "Calls created", "direction", "runner_type", "env"),
        _c("va_calls_rejected_total", "Call creations rejected, by error code", "code"),
        _c("va_rate_limited_total", "Requests rejected by rate limiting", "bucket", "plan"),
        _c("va_idempotent_replays_total", "Responses replayed for a repeated Idempotency-Key"),
        _g("va_sse_connections", "Open server-sent event connections", "stream"),
        _c("va_mcp_tool_calls_total", "MCP tool invocations", "tool", "status"),
    ),
    Service.TELEPHONY_EDGE: (
        _c("va_telnyx_webhooks_total", "Telnyx webhooks received", "event_type", "result"),
        _h("va_telnyx_webhook_processing_ms", "Telnyx webhook processing time in ms", "event_type"),
        _c("va_inbound_calls_total", "Inbound calls by outcome", "result"),
        _c("va_amd_results_total", "Answering machine detection results", "result"),
        _c("va_sms_inbound_total", "Inbound SMS by outcome", "result"),
    ),
    Service.MEDIA_WORKER: (
        _g("va_media_active_calls", "Calls attached to this pod (HPA metric)", "transport"),
        _c("va_media_calls_started_total", "Media sessions started"),
        _c("va_media_calls_ended_total", "Media sessions ended", "end_reason"),
        _c("va_media_admission_rejected_total", "Media sessions refused at admission", "reason"),
        _h("va_turn_e2e_ms", "Voice-to-voice turn latency in ms", "org_tier"),
        _h("va_turn_eot_ms", "End-of-turn detection latency in ms"),
        _h("va_stt_final_ms", "Speech-to-text final transcript latency in ms"),
        _h("va_llm_ttft_ms", "LLM time to first token in ms", "model", "purpose"),
        _c("va_llm_tokens_total", "LLM tokens", "model", "direction"),
        _c("va_llm_errors_total", "LLM request errors", "model", "kind"),
        _h("va_tts_ttfa_ms", "Text-to-speech time to first audio in ms", "model"),
        _c("va_tts_characters_total", "Characters sent to text-to-speech", "model"),
        _c("va_tts_cache_hits_total", "Text-to-speech cache hits"),
        _c("va_tts_cache_misses_total", "Text-to-speech cache misses"),
        _c("va_vendor_ws_reconnects_total", "Vendor WebSocket reconnects", "vendor"),
        _c("va_vendor_errors_total", "Vendor errors", "vendor", "code"),
        _c("va_barge_in_total", "Caller barge-ins", "honored"),
        _h("va_barge_in_reaction_ms", "Barge-in reaction time in ms"),
        _c("va_audio_underruns_total", "Outbound audio underruns"),
        _c("va_audio_reorder_total", "Inbound audio frames reordered"),
        _c("va_tool_calls_total", "Custom tool calls", "status_class"),
        _h("va_tool_duration_ms", "Custom tool call duration in ms"),
        _c("va_route_decisions_total", "Pathway routing decisions", "kind"),
        _c("va_guard_rail_triggers_total", "Guard rail triggers", "guard_rail", "action"),
        _h("va_event_loop_lag_ms", "Event loop lag measured by a 100 ms ticker, in ms"),
        _g("va_media_draining", "1 while the pod drains, else 0"),
    ),
    Service.WORKER: (
        _c("va_workflow_started_total", "Temporal workflows started", "workflow"),
        _c("va_workflow_failed_total", "Temporal workflows failed", "workflow"),
        _h("va_activity_duration_ms", "Temporal activity duration in ms", "activity"),
        _c("va_activity_failures_total", "Temporal activity failures", "activity", "error_type"),
        _c("va_dial_total", "Outbound dial attempts", "result"),
        _h("va_postcall_duration_ms", "Post-call step duration in ms", "step"),
        _c("va_batch_dispatch_total", "Batch call dispatch decisions", "result"),
        _c("va_kb_documents_processed_total", "Knowledge base documents processed", "status"),
        _g("va_billing_reconcile_drift_micros", "Billing reconciliation drift in micro-dollars"),
        _g("va_credit_low_balance_orgs", "Organizations below their low-balance threshold"),
    ),
    Service.WEBHOOK_DISPATCHER: (
        _c("va_webhook_deliveries_total", "Webhook delivery outcomes", "result", "format"),
        _h("va_webhook_delivery_ms", "Webhook delivery duration in ms"),
        _g("va_webhook_backlog", "Webhook deliveries pending and due now"),
        _g("va_webhook_oldest_pending_seconds", "Age of the oldest pending webhook delivery in seconds"),
    ),
    Service.EVENTS_SINK: (
        _c("va_events_sink_inserted_total", "Rows inserted into ClickHouse", "table"),
        _g(
            "va_events_sink_lag_seconds", "Age of the oldest unacknowledged stream entry in seconds", "stream"
        ),
        _c("va_events_sink_errors_total", "ClickHouse insert errors", "table"),
    ),
}


def new_registry(*, process_collectors: bool = True) -> CollectorRegistry:
    """A fresh registry, with the process, platform, and GC collectors unless ``process_collectors=False``."""
    registry = CollectorRegistry()
    if process_collectors:
        for collector in (PROCESS_COLLECTOR, PLATFORM_COLLECTOR, GC_COLLECTOR):
            registry.register(collector)
    return registry


def _create(spec: MetricSpec, registry: CollectorRegistry) -> Counter | Gauge | Histogram:
    if spec.kind is MetricKind.COUNTER:
        return Counter(spec.name, spec.doc, spec.labels, registry=registry)
    if spec.kind is MetricKind.GAUGE:
        return Gauge(spec.name, spec.doc, spec.labels, registry=registry)
    return Histogram(spec.name, spec.doc, spec.labels, registry=registry, buckets=LAT_MS)


class ServiceMetrics:
    """Every catalog metric of one service, registered in ``registry``; look them up by exposition name."""

    def __init__(self, service: Service, registry: CollectorRegistry) -> None:
        self.service = service
        self.registry = registry
        self._metrics = {spec.name: _create(spec, registry) for spec in CATALOG[service]}

    def _get[M: (Counter, Gauge, Histogram)](self, name: str, kind: type[M]) -> M:
        try:
            metric = self._metrics[name]
        except KeyError:
            msg = f"metric {name!r} is not in the {self.service.value} catalog"
            raise KeyError(msg) from None
        if not isinstance(metric, kind):
            msg = f"metric {name!r} is a {type(metric).__name__.lower()}, not a {kind.__name__.lower()}"
            raise TypeError(msg)
        return metric

    def counter(self, name: str) -> Counter:
        return self._get(name, Counter)

    def gauge(self, name: str) -> Gauge:
        return self._get(name, Gauge)

    def histogram(self, name: str) -> Histogram:
        return self._get(name, Histogram)


def build_metrics(service: Service, registry: CollectorRegistry | None = None) -> ServiceMetrics:
    """Register ``service``'s catalog in ``registry`` (default: a fresh :func:`new_registry`)."""
    return ServiceMetrics(service, registry if registry is not None else new_registry())


def metrics_app(registry: Collector) -> ASGIApp:
    """ASGI app serving ``registry`` in the Prometheus text format (mount at ``/metrics``)."""
    app: ASGIApp = make_asgi_app(registry)
    return app


def render_latest(registry: Collector) -> tuple[bytes, str]:
    """``(body, content_type)`` for a hand-written ``/metrics`` route."""
    return generate_latest(registry), CONTENT_TYPE_LATEST
