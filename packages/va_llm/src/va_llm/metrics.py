"""Gateway metrics (specs/27-observability.md §3.3 and §3.6; Decision P03-T13a rule 10).

The gateway runs in several services, so its own ``va_llm_`` rows of §3.6 are registered in the registry
the process passes. In the media-worker the process also passes its catalog :class:`ServiceMetrics`, which
feeds the §3.3 ``va_llm_tokens_total`` and ``va_llm_errors_total`` series.
"""

from __future__ import annotations

from typing import Final, Literal

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from va_common.metrics import LAT_MS, ServiceMetrics
from va_llm.breaker import BREAKER_STATE_VALUES, BreakerState
from va_llm.errors import ErrorKind

__all__ = [
    "BREAKER_METRIC",
    "DURATION_METRIC",
    "REQUESTS_METRIC",
    "SPECULATION_METRIC",
    "AttemptOutcome",
    "LlmMetrics",
]

type AttemptOutcome = Literal[
    "ok",
    "timeout",
    "rate_limited",
    "server_error",
    "client_error",
    "transport_error",
    "parse_error",
    "breaker_open",
]

REQUESTS_METRIC: Final = "va_llm_requests_total"
DURATION_METRIC: Final = "va_llm_request_duration_ms"
BREAKER_METRIC: Final = "va_llm_breaker_state"
SPECULATION_METRIC: Final = "va_llm_speculation_total"


class LlmMetrics:
    """The §3.6 ``va_llm_`` metrics in ``registry`` plus, when given, the §3.3 catalog counters."""

    def __init__(self, registry: CollectorRegistry, catalog: ServiceMetrics | None = None) -> None:
        self._requests = Counter(
            REQUESTS_METRIC,
            "LLM requests (one per attempt) by outcome",
            labelnames=("call_type", "model", "outcome"),
            registry=registry,
        )
        self._duration = Histogram(
            DURATION_METRIC,
            "LLM gateway call duration in milliseconds, retries and fallback included",
            labelnames=("call_type",),
            buckets=LAT_MS,
            registry=registry,
        )
        self._breaker = Gauge(
            BREAKER_METRIC,
            "LLM circuit breaker state (0 closed, 1 half-open, 2 open)",
            labelnames=("provider", "model"),
            registry=registry,
        )
        self.speculation = Counter(
            SPECULATION_METRIC,
            "Speculative dialogue generations by outcome",
            labelnames=("outcome",),
            registry=registry,
        )
        self._tokens = catalog.counter("va_llm_tokens_total") if catalog is not None else None
        self._errors = catalog.counter("va_llm_errors_total") if catalog is not None else None

    def attempt(self, call_type: str, model: str, outcome: AttemptOutcome) -> None:
        self._requests.labels(call_type=call_type, model=model, outcome=outcome).inc()

    def duration(self, call_type: str, ms: float) -> None:
        self._duration.labels(call_type=call_type).observe(ms)

    def breaker_state(self, provider_id: str, model: str, state: BreakerState) -> None:
        self._breaker.labels(provider=provider_id, model=model).set(BREAKER_STATE_VALUES[state])

    def tokens(self, model: str, prompt_tokens: int, completion_tokens: int) -> None:
        if self._tokens is not None:
            self._tokens.labels(model=model, direction="in").inc(prompt_tokens)
            self._tokens.labels(model=model, direction="out").inc(completion_tokens)

    def error(self, model: str, kind: ErrorKind) -> None:
        if self._errors is not None:
            self._errors.labels(model=model, kind=kind).inc()
