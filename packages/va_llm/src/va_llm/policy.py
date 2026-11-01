"""Per call type timeouts, retries, and fallback (specs/10-llm-gateway.md §2.1; Decision P03-T13a rule 5).

``timeout_s`` is the total budget on the primary target (retries and their waits included) unless
``per_attempt`` is set (analysis: 60 s for every attempt). Timeouts are retried only for per-attempt
policies: for the others the budget is spent. The fallback target, when the policy has one, gets one attempt
with a fresh ``timeout_s``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal, get_args

from va_llm.providers import ModelRole

__all__ = ["CALL_TYPES", "POLICIES", "CallPolicy", "CallType"]

type CallType = Literal[
    "dialogue", "router", "loop_condition", "extraction", "voicemail", "guard_rails", "analysis"
]

CALL_TYPES: Final[tuple[CallType, ...]] = get_args(CallType.__value__)


@dataclass(frozen=True, slots=True)
class CallPolicy:
    """``retry_delays_s`` holds one wait per retry (``0`` = immediate)."""

    role: ModelRole
    timeout_s: float
    retry_delays_s: tuple[float, ...]
    per_attempt: bool
    falls_back: bool


_CLASSIFIER_RETRY: Final = (0.0,)

POLICIES: Final[Mapping[CallType, CallPolicy]] = MappingProxyType(
    {
        "dialogue": CallPolicy("dialogue", 2.5, (), per_attempt=False, falls_back=True),
        "router": CallPolicy("router", 1.5, _CLASSIFIER_RETRY, per_attempt=False, falls_back=False),
        "loop_condition": CallPolicy("router", 1.5, _CLASSIFIER_RETRY, per_attempt=False, falls_back=False),
        "extraction": CallPolicy("router", 3.0, _CLASSIFIER_RETRY, per_attempt=False, falls_back=False),
        "voicemail": CallPolicy("router", 1.5, _CLASSIFIER_RETRY, per_attempt=False, falls_back=False),
        "guard_rails": CallPolicy("router", 2.0, _CLASSIFIER_RETRY, per_attempt=False, falls_back=False),
        "analysis": CallPolicy("analysis", 60.0, (2.0, 8.0), per_attempt=True, falls_back=True),
    }
)
