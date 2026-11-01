"""Telnyx client metrics (specs/27-observability.md §3.6).

The client runs in several services, so its metrics are registered by the library in whichever registry the
process passes (one :class:`TelnyxMetrics` per registry), not in the per-service
``va_common.metrics.CATALOG``.
"""

from __future__ import annotations

from typing import Final, Literal

from prometheus_client import CollectorRegistry, Counter, Histogram

from va_common.metrics import LAT_MS

__all__ = ["DURATION_METRIC", "REQUESTS_METRIC", "Outcome", "TelnyxMetrics"]

type Outcome = Literal["ok", "already_ended", "carrier_error", "rate_limited", "transport_error"]

REQUESTS_METRIC: Final = "va_telnyx_requests_total"
DURATION_METRIC: Final = "va_telnyx_request_duration_ms"


class TelnyxMetrics:
    """``va_telnyx_requests_total{operation, outcome}`` and ``va_telnyx_request_duration_ms{operation}``."""

    def __init__(self, registry: CollectorRegistry) -> None:
        self.requests = Counter(
            REQUESTS_METRIC, "Telnyx API operations by outcome", ("operation", "outcome"), registry=registry
        )
        self.duration = Histogram(
            DURATION_METRIC,
            "Telnyx API operation duration in milliseconds, retries and rate-limit waits included",
            ("operation",),
            registry=registry,
            buckets=LAT_MS,
        )

    def observe(self, operation: str, outcome: Outcome, duration_ms: float) -> None:
        self.requests.labels(operation=operation, outcome=outcome).inc()
        self.duration.labels(operation=operation).observe(duration_ms)
