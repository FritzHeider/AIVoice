"""Stored call values → Call Object values (specs/19 §4, specs/05 §8, §9; Decision P05-T16a).

Pure functions; statuses and ``ended_by`` values are the specs/03 ``calls`` enumerations as plain strings
(va_domain does not import va_db).
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any, Final

from va_domain.call_object.models import (
    BlandAnsweredBy,
    BlandCallEndedBy,
    BlandCallStatus,
    CallLatency,
    Transcript,
)

__all__ = [
    "BLAND_STATUS_FILTER",
    "CALL_ENDED_BY",
    "bland_answered_by",
    "bland_status",
    "concatenated_transcript",
    "dollars",
    "latency_summary",
    "minutes",
]

_BLAND_STATUS: Final[Mapping[str, BlandCallStatus]] = {
    "scheduled": "scheduled",
    "queued": "queued",
    "dialing": "in-progress",
    "ringing": "in-progress",
    "in_progress": "in-progress",
    "transferring": "in-progress",
    "transferred": "completed",
    "completed": "completed",
    "failed": "failed",
    "canceled": "canceled",
    "no_answer": "no-answer",
    "busy": "busy",
}

BLAND_STATUS_FILTER: Final[Mapping[str, tuple[str, ...]]] = {
    bland: tuple(status for status, mapped in _BLAND_STATUS.items() if mapped == bland)
    for bland in dict.fromkeys(_BLAND_STATUS.values())
}
"""Bland status → the stored statuses it covers (the inverse of :func:`bland_status`)."""

CALL_ENDED_BY: Final[Mapping[str, BlandCallEndedBy]] = {
    "agent": "ASSISTANT",
    "user": "USER",
    "system": "SYSTEM",
    "api": "API",
    "guard_rail": "GUARD_RAIL",
    "timeout": "TIMEOUT",
    "error": "ERROR",
}
"""``calls.ended_by`` → Bland ``call_ended_by`` (specs/05 §9)."""

_ANSWERED_BY: Final[Mapping[str, BlandAnsweredBy]] = {
    "human": "human",
    "voicemail": "voicemail",
    "ivr": "ivr",
    "unknown": "unknown",
    "no_answer": "no-answer",
    "fax": "fax",
}

_SECONDS_PER_MINUTE: Final = 60
_MICROS_PER_DOLLAR: Final = 1_000_000
_DECIMALS: Final = 6
_P50: Final = 50
_P95: Final = 95


def bland_status(status: str) -> BlandCallStatus:
    """The Bland form of a stored call status (``KeyError`` for an unknown status)."""
    return _BLAND_STATUS[status]


def bland_answered_by(answered_by: str | None) -> BlandAnsweredBy | None:
    """The Bland form of ``calls.answered_by`` (``no_answer`` → ``no-answer``)."""
    return None if answered_by is None else _ANSWERED_BY[answered_by]


def minutes(seconds: Decimal | float) -> float:
    """Seconds as minutes, rounded to 6 decimals."""
    return round(float(seconds) / _SECONDS_PER_MINUTE, _DECIMALS)


def dollars(micros: int) -> float:
    """Micro-dollars as dollars, rounded to 6 decimals."""
    return round(micros / _MICROS_PER_DOLLAR, _DECIMALS)


def concatenated_transcript(transcripts: Sequence[Transcript]) -> str:
    """Bland's ``concatenated_transcript``: ``"{role}: {text}\\n"`` per turn, joined by one space."""
    return " ".join(f"{turn.user}: {turn.text}\n" for turn in transcripts)


def _nearest_rank(ordered: Sequence[int], percentile: int) -> int:
    return ordered[max(math.ceil(percentile / 100 * len(ordered)), 1) - 1]


def _mean(values: Sequence[float]) -> int | None:
    return round(sum(values) / len(values)) if values else None


def _numbers(latencies: Sequence[Mapping[str, Any]], key: str) -> list[float]:
    return [
        value
        for latency in latencies
        if isinstance(value := latency.get(key), int | float) and not isinstance(value, bool)
    ]


def latency_summary(latencies: Iterable[Mapping[str, Any]]) -> CallLatency:
    """Summary of the turns' ``latency`` dicts: nearest-rank p50/p95 of ``e2e_ms`` (``turns`` = turns that
    have one), rounded means of ``llm_ttft_ms`` and ``tts_ttfa_ms`` over the turns that have them."""
    measured = list(latencies)
    e2e = sorted(round(value) for value in _numbers(measured, "e2e_ms"))
    return CallLatency(
        p50_e2e_ms=_nearest_rank(e2e, _P50) if e2e else None,
        p95_e2e_ms=_nearest_rank(e2e, _P95) if e2e else None,
        avg_llm_ttft_ms=_mean(_numbers(measured, "llm_ttft_ms")),
        avg_tts_ttfa_ms=_mean(_numbers(measured, "tts_ttfa_ms")),
        turns=len(e2e),
    )
