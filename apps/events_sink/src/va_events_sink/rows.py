"""Mapping of domain events onto ClickHouse rows (specs/03-data-model.md §12; specs/19 §1 Decision P06-T05).

:func:`rows_for_event` is pure. One event may produce rows in several tables:

- ``va.call_events``: every call event that has a ``call_events`` row in Postgres (:func:`va_events.row_for`:
  ``call_id``, ``seq``, and a category), ``data`` as JSON text.
- ``va.call_turns``: ``call.turn`` events; the latency columns come from the optional ``data.latency`` object
  (assistant turns, specs/19 §2), else NULL.
- ``va.calls_fact``: ``call.completed`` events, whose ``data`` is the Call Object (specs/19 §4).
- ``va.usage_events_raw``: ``billing.usage`` events (:class:`va_events.UsageEventData`).

Malformed data of a mapped type raises :class:`RowError`; the sink parks that event (the batch goes on).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Annotated, Any, Final

from pydantic import BaseModel, BeforeValidator, ConfigDict, StrictBool, StrictInt, StrictStr, ValidationError

from va_common.json import dumps
from va_domain.call_object import CallObject
from va_events import DomainEvent, EventType, UsageEventData, row_for

__all__ = ["LATENCY_COLUMNS", "TABLE_COLUMNS", "Row", "RowError", "TableRows", "rows_for_event"]

LATENCY_COLUMNS: Final = (
    "eot_ms",
    "stt_final_ms",
    "llm_ttft_ms",
    "tts_ttfa_ms",
    "e2e_ms",
    "route_ms",
    "tool_ms",
)

TABLE_COLUMNS: Final[dict[str, tuple[str, ...]]] = {
    "call_events": ("org_id", "call_id", "seq", "category", "level", "message", "data", "created_at"),
    "call_turns": (
        "org_id",
        "call_id",
        "turn_index",
        "role",
        "node_id",
        "text_len",
        "interrupted",
        *LATENCY_COLUMNS,
        "created_at",
    ),
    "calls_fact": (
        "org_id",
        "call_id",
        "direction",
        "status",
        "answered_by",
        "runner_type",
        "pathway_id",
        "agent_id",
        "experiment_arm_id",
        "batch_id",
        "disposition_tag",
        "end_reason",
        "connected_seconds",
        "transfer_seconds",
        "price_micros",
        "created_at",
        "ended_at",
    ),
    "usage_events_raw": (
        "org_id",
        "meter",
        "quantity",
        "amount_micros",
        "cost_micros",
        "reference_type",
        "reference_id",
        "occurred_at",
    ),
}
"""Column order of every table the sink writes (specs/03 §12 declaration order)."""

type Row = tuple[Any, ...]
type TableRows = dict[str, list[Row]]

_MICROS_PER_DOLLAR: Final = 1_000_000
_SECONDS_PER_MINUTE: Final = 60


class RowError(ValueError):
    """An event of a mapped type whose data cannot be mapped; ``reason`` names the table and the problem."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _non_negative(value: object) -> object:
    """Latency values below 0 (clock skew) are stored as 0; non-integers fail validation."""
    if isinstance(value, int) and not isinstance(value, bool) and value < 0:
        return 0
    return value


type _Millis = Annotated[StrictInt, BeforeValidator(_non_negative)]


class _TurnLatency(BaseModel):
    model_config = ConfigDict(extra="ignore")

    eot_ms: _Millis | None = None
    stt_final_ms: _Millis | None = None
    llm_ttft_ms: _Millis | None = None
    tts_ttfa_ms: _Millis | None = None
    e2e_ms: _Millis | None = None
    route_ms: _Millis | None = None
    tool_ms: _Millis | None = None


class _TurnData(BaseModel):
    model_config = ConfigDict(extra="ignore")

    turn_index: Annotated[StrictInt, BeforeValidator(_non_negative)]
    role: StrictStr
    text: StrictStr = ""
    node_id: StrictStr | None = None
    interrupted: StrictBool = False
    latency: _TurnLatency | None = None


def _call_event_row(event: DomainEvent) -> Row | None:
    row = row_for(event)
    if row is None:
        return None
    return (
        row.org_id,
        row.call_id,
        row.seq,
        row.category,
        row.level,
        row.message,
        dumps(row.data).decode(),
        row.created_at,
    )


def _call_id(event: DomainEvent, table: str) -> uuid.UUID:
    if event.call_id is None:
        raise RowError(f"{table}:no_call_id")
    return event.call_id


def _turn_row(event: DomainEvent) -> Row:
    call_id = _call_id(event, "call_turns")
    try:
        turn = _TurnData.model_validate(event.data)
    except ValidationError as exc:
        raise RowError("call_turns:invalid_data") from exc
    latency = turn.latency or _TurnLatency()
    return (
        event.org_id,
        call_id,
        turn.turn_index,
        turn.role,
        turn.node_id or "",
        len(turn.text),
        int(turn.interrupted),
        *(getattr(latency, column) for column in LATENCY_COLUMNS),
        event.occurred_at,
    )


def _runner_type(call: CallObject) -> str:
    if call.agent_id is not None:
        return "agent"
    if call.persona_id is not None:
        return "persona"
    if call.pathway_id is not None:
        return "pathway"
    return "task"


def _fact_row(event: DomainEvent) -> Row:
    call_id = _call_id(event, "calls_fact")
    try:
        call = CallObject.model_validate(event.data)
    except ValidationError as exc:
        raise RowError("calls_fact:invalid_call_object") from exc
    if call.call_id != call_id or call.org_id != event.org_id:
        raise RowError("calls_fact:call_mismatch")
    return (
        call.org_id,
        call.call_id,
        call.direction,
        call.status,
        call.answered_by or "",
        _runner_type(call),
        call.pathway_id,
        call.agent_id,
        None,  # the Call Object carries the arm label only (specs/19 §1 Decision P06-T05)
        call.batch_id,
        call.disposition_tag or "",
        call.end_reason or "",
        round(call.call_length * _SECONDS_PER_MINUTE, 3),
        round(call.transfer_duration * _SECONDS_PER_MINUTE, 3),
        round(call.price * _MICROS_PER_DOLLAR),
        call.created_at,
        call.end_at,
    )


def _usage_row(event: DomainEvent) -> Row:
    try:
        usage = UsageEventData.model_validate(event.data)
    except ValidationError as exc:
        raise RowError("usage_events_raw:invalid_data") from exc
    return (
        event.org_id,
        usage.meter,
        float(usage.quantity),
        usage.amount_micros,
        usage.cost_micros,
        usage.reference_type,
        usage.reference_id,
        usage.occurred_at,
    )


_TYPED_ROWS: Final[dict[EventType, tuple[str, Callable[[DomainEvent], Row]]]] = {
    EventType.CALL_TURN: ("call_turns", _turn_row),
    EventType.CALL_COMPLETED: ("calls_fact", _fact_row),
    EventType.BILLING_USAGE: ("usage_events_raw", _usage_row),
}


def rows_for_event(event: DomainEvent) -> TableRows:
    """Every ClickHouse row of ``event`` by table (empty for events no table mirrors)."""
    rows: TableRows = {}
    call_event = _call_event_row(event)
    if call_event is not None:
        rows["call_events"] = [call_event]
    typed = _TYPED_ROWS.get(event.type)
    if typed is not None:
        table, build = typed
        rows[table] = [build(event)]
    return rows
