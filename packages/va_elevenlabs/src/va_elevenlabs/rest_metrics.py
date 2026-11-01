"""ElevenLabs REST client metrics (specs/27-observability.md §3.6; Decision 2026-11-01 P03-T12 rule 7).

Like the Telnyx client, the REST client runs in several services (api, worker), so it registers its metrics
in whichever registry the process passes: one :class:`ElevenLabsRestMetrics` per registry.
"""

from __future__ import annotations

from typing import Final, Literal

from prometheus_client import CollectorRegistry, Counter, Histogram

from va_common.metrics import LAT_MS

__all__ = ["REST_DURATION_METRIC", "REST_REQUESTS_METRIC", "ElevenLabsRestMetrics", "RestOutcome"]

type RestOutcome = Literal[
    "ok", "client_error", "auth_error", "rate_limited", "server_error", "transport_error"
]

REST_REQUESTS_METRIC: Final = "va_elevenlabs_requests_total"
REST_DURATION_METRIC: Final = "va_elevenlabs_request_duration_ms"


class ElevenLabsRestMetrics:
    """``va_elevenlabs_requests_total{operation, outcome}`` and
    ``va_elevenlabs_request_duration_ms{operation}``."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self._requests = Counter(
            REST_REQUESTS_METRIC,
            "ElevenLabs REST operations by final outcome",
            labelnames=("operation", "outcome"),
            registry=registry,
        )
        self._duration = Histogram(
            REST_DURATION_METRIC,
            "ElevenLabs REST operation duration in milliseconds, retries included",
            labelnames=("operation",),
            buckets=LAT_MS,
            registry=registry,
        )

    def observe(self, operation: str, outcome: RestOutcome, duration_ms: float) -> None:
        self._requests.labels(operation=operation, outcome=outcome).inc()
        self._duration.labels(operation=operation).observe(duration_ms)
