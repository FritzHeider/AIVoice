"""Request targets and per-attempt bookkeeping shared by ``complete`` and ``stream_dialogue``
(Decision P03-T13a rules 6-10, Decision P03-T13b rule 12)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

import httpx

from va_common.logging import get_logger
from va_llm.errors import (
    LlmError,
    LlmHttpError,
    LlmProtocolError,
    LlmRateLimitedError,
    LlmTimeoutError,
)
from va_llm.metrics import AttemptOutcome, LlmMetrics
from va_llm.providers import LlmProvider, ProviderRegistry

__all__ = [
    "RETRYABLE_STATUSES",
    "Target",
    "attempt_outcome",
    "platform_fallback",
    "record_failure",
    "sdk_backstop",
]

RETRYABLE_STATUSES: Final = frozenset({500, 502, 503, 504})
_SDK_READ_S: Final = 30.0
_HTTP_SERVER: Final = 500

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Target:
    """Where one attempt goes: a provider and one of its models."""

    provider: LlmProvider
    model: str

    def same_as(self, other: Target) -> bool:
        return self.provider.id == other.provider.id and self.model == other.model


def platform_fallback(registry: ProviderRegistry) -> Target:
    """The platform provider's ``fallback`` model (the fallback of every provider, specs/10 §2.1)."""
    platform = registry.platform
    return Target(platform, platform.models["fallback"])


def attempt_outcome(error: LlmError) -> AttemptOutcome:
    """The ``va_llm_requests_total`` outcome of a failed attempt."""
    match error:
        case LlmTimeoutError():
            return "timeout"
        case LlmRateLimitedError():
            return "rate_limited"
        case LlmHttpError(status=status):
            return "server_error" if status >= _HTTP_SERVER else "client_error"
        case LlmProtocolError():
            return "parse_error"
        case _:
            return "transport_error"


def sdk_backstop(limit_s: float) -> httpx.Timeout:
    """The SDK's own timeout: a backstop behind the clock deadline, never shorter than it."""
    return httpx.Timeout(connect=2.0, read=max(_SDK_READ_S, limit_s + 1.0), write=5.0, pool=2.0)


def record_failure(metrics: LlmMetrics, call_type: str, target: Target, error: LlmError) -> None:
    """Count and log one failed attempt."""
    metrics.attempt(call_type, target.model, attempt_outcome(error))
    metrics.error(target.model, error.error_kind)
    _log.warning(
        "llm.attempt_failed",
        call_type=call_type,
        provider=target.provider.id,
        model=target.model,
        kind=error.error_kind,
        status=error.status if isinstance(error, LlmHttpError) else None,
    )
