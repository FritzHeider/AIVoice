"""Pure checks of the e2e harness (specs/30 §3.5 ``assertions``; Decision P05-T17b under specs/30 §5).

:func:`evaluate` checks a scenario's ``assertions`` block against what the platform and the simulator recorded
for one call: the Call Object (``GET /v1/calls/{id}``), the call's event log (``GET /v1/calls/{id}/events``),
and the FakeTelnyx timeline (``GET /sim/telnyx/calls/{ccid}/timeline``). It returns one message per failed
check (empty = all passed), so a test reports every failure at once.

Until the webhook dispatcher exists (phase 06), ``webhook_events`` is checked against the event log: event
rows carry ``category``/``message``/``data`` but no type, so a type is recognised by its message when it has a
fixed one (:data:`EVENT_MESSAGES`), else by its category when no other event type shares that category.
"""

from __future__ import annotations

import itertools
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from va_events import DEFAULT_CATEGORY, ENDED_MESSAGE, EventType
from va_sim.scenario import Assertions
from va_workflows.activities.post_call import COMPLETED_MESSAGE

__all__ = [
    "ANSWERED_BY",
    "EVENT_MESSAGES",
    "TurnOverhead",
    "alternating_roles",
    "evaluate",
    "event_present",
    "latency_summaries",
    "nearest_rank",
    "turn_overheads",
]

# Scenario `answered_by` → Call Object `answered_by` (Bland form).
ANSWERED_BY: Final[Mapping[str, str]] = {"human": "human", "machine": "voicemail"}
# Event types whose rows have a fixed message.
EVENT_MESSAGES: Final[Mapping[str, str]] = {
    EventType.CALL_COMPLETED.value: COMPLETED_MESSAGE,
    EventType.CALL_ENDED.value: ENDED_MESSAGE,
}
_LATENCY_CATEGORY: Final = "latency"
_SUMMARY_PREFIX: Final = "E2E:"
_VENDOR_KEYS: Final = ("eot_ms", "stt_final_ms", "llm_ttft_ms", "tts_ttfa_ms")
_CATEGORY_USES: Final = Counter(category for category in DEFAULT_CATEGORY.values() if category is not None)


def nearest_rank(values: Iterable[float], percentile: float) -> float:
    """The nearest-rank percentile (``percentile`` in (0, 100]) of a non-empty sample."""
    ordered = sorted(values)
    if not ordered:
        message = "nearest_rank needs at least one value"
        raise ValueError(message)
    rank = max(1, math.ceil(percentile / 100 * len(ordered)))
    return ordered[rank - 1]


def alternating_roles(transcripts: Sequence[Mapping[str, Any]]) -> bool:
    """Whether consecutive transcript turns of the user and the assistant never repeat a speaker."""
    roles = [turn["user"] for turn in transcripts if turn["user"] in {"user", "assistant"}]
    return all(first != second for first, second in itertools.pairwise(roles))


def event_present(event_type: str, events: Sequence[Mapping[str, Any]]) -> bool | None:
    """Whether an event of ``event_type`` is in the log; ``None`` when rows cannot identify that type."""
    message = EVENT_MESSAGES.get(event_type)
    if message is not None:
        return any(event["message"] == message for event in events)
    try:
        category = DEFAULT_CATEGORY[EventType(event_type)]
    except ValueError:
        return None
    if category is None or _CATEGORY_USES[category] != 1:
        return None
    return any(event["category"] == category for event in events)


def latency_summaries(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The data of each per-turn ``"E2E: {n}ms"`` ``call.latency`` event, in log order."""
    return [
        event["data"]
        for event in events
        if event["category"] == _LATENCY_CATEGORY and str(event["message"]).startswith(_SUMMARY_PREFIX)
    ]


class TurnOverhead:
    """One caller turn: the simulator's voice-to-voice latency and the platform's share of it."""

    __slots__ = ("overhead_ms", "sim_ms", "vendor_ms")

    def __init__(self, sim_ms: int, vendor_ms: int) -> None:
        self.sim_ms = sim_ms
        self.vendor_ms = vendor_ms
        self.overhead_ms = sim_ms - vendor_ms

    def __repr__(self) -> str:
        return (
            f"TurnOverhead(sim_ms={self.sim_ms}, vendor_ms={self.vendor_ms}, overhead_ms={self.overhead_ms})"
        )


def turn_overheads(timeline: Mapping[str, Any], events: Sequence[Mapping[str, Any]]) -> list[TurnOverhead]:
    """Platform overhead per answered caller utterance (specs/30 §5 Decision P05-T17b).

    The n-th timeline utterance with a measured ``latency_ms`` pairs with the n-th latency summary; overhead =
    sim ``latency_ms`` - (``eot_ms`` + ``stt_final_ms`` + ``llm_ttft_ms`` + ``tts_ttfa_ms``) of that summary
    (missing stages count 0). ``eot_ms`` is the endpointer's deliberate silence wait, so it is policy time.
    """
    measured = [u["latency_ms"] for u in timeline["utterances"] if u.get("latency_ms") is not None]
    summaries = latency_summaries(events)
    return [
        TurnOverhead(int(sim_ms), sum(int(summary.get(key) or 0) for key in _VENDOR_KEYS))
        for sim_ms, summary in zip(measured, summaries, strict=False)
    ]


def _transcript_text(call: Mapping[str, Any]) -> str:
    return "\n".join(str(turn["text"]) for turn in call.get("transcripts") or [])


def _field_checks(assertions: Assertions, call: Mapping[str, Any]) -> list[str]:
    answered_by = None if assertions.answered_by is None else ANSWERED_BY[assertions.answered_by]
    expected: list[tuple[str, str | None]] = [
        ("status", assertions.call_status),
        ("end_reason", assertions.end_reason),
        ("answered_by", answered_by),
    ]
    return [
        f"{name}: expected {wanted!r}, got {call.get(name)!r}"
        for name, wanted in expected
        if wanted is not None and call.get(name) != wanted
    ]


def _transcript_checks(assertions: Assertions, call: Mapping[str, Any]) -> list[str]:
    failures: list[str] = []
    turns = len(call.get("transcripts") or [])
    if assertions.min_turns is not None and turns < assertions.min_turns:
        failures.append(f"min_turns: expected at least {assertions.min_turns}, got {turns}")
    text = _transcript_text(call).lower()
    failures.extend(
        f"transcript_contains: {needle!r} not in the transcript"
        for needle in assertions.transcript_contains
        if needle.lower() not in text
    )
    return failures


def _variable_checks(assertions: Assertions, call: Mapping[str, Any]) -> list[str]:
    variables = call.get("variables") or {}
    return [
        f"variables.{name}: expected {value!r}, got {variables.get(name)!r}"
        for name, value in assertions.variables.items()
        if variables.get(name) != value
    ]


def _latency_checks(assertions: Assertions, timeline: Mapping[str, Any]) -> list[str]:
    if assertions.max_e2e_ms_p95 is None:
        return []
    latencies = [u["latency_ms"] for u in timeline["utterances"] if u.get("latency_ms") is not None]
    if not latencies:
        return ["max_e2e_ms_p95: the timeline has no measured utterance latency"]
    p95 = nearest_rank(latencies, 95)
    if p95 > assertions.max_e2e_ms_p95:
        return [f"max_e2e_ms_p95: p95 {p95} ms > {assertions.max_e2e_ms_p95} ms ({latencies})"]
    return []


def _event_checks(assertions: Assertions, events: Sequence[Mapping[str, Any]]) -> list[str]:
    failures: list[str] = []
    for event_type in assertions.webhook_events:
        present = event_present(event_type, events)
        if present is None:
            failures.append(f"webhook_events: {event_type!r} cannot be identified in the event log")
        elif not present:
            failures.append(f"webhook_events: no {event_type!r} event")
    return failures


def evaluate(
    assertions: Assertions,
    call: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    timeline: Mapping[str, Any],
) -> list[str]:
    """Every failed check of ``assertions`` as a readable message (empty when all hold)."""
    return [
        *_field_checks(assertions, call),
        *_transcript_checks(assertions, call),
        *_variable_checks(assertions, call),
        *_latency_checks(assertions, timeline),
        *_event_checks(assertions, events),
    ]
