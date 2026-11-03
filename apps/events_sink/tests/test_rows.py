"""Event → ClickHouse row mapping (specs/03 §12; specs/19 §1 Decision P06-T05)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import orjson
import pytest
from ch_support import spec_columns
from sink_helpers import AT, ORG, completed_event, turn_event, usage_event_of

from va_common.time import FrozenClock
from va_events import EventType, new_event
from va_events_sink.rows import LATENCY_COLUMNS, TABLE_COLUMNS, RowError, rows_for_event

CALL = uuid.UUID("0193c5d2-0000-7000-8000-0000000000c1")


def _as_dict(table: str, row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(TABLE_COLUMNS[table], row, strict=True))


@pytest.mark.parametrize("table", sorted(TABLE_COLUMNS))
def test_column_order_matches_the_spec(table: str) -> None:
    assert TABLE_COLUMNS[table] == tuple(name for name, _ in spec_columns()[table])


def test_a_call_event_gets_a_call_events_row_with_json_data() -> None:
    event = new_event(
        EventType.CALL_TOOL,
        org_id=ORG,
        clock=FrozenClock(AT),
        call_id=CALL,
        level="warning",
        message="tool failed",
        data={"tool_name": "lookup", "status_code": 500},
    ).model_copy(update={"seq": 7})
    rows = rows_for_event(event)
    assert list(rows) == ["call_events"]
    (row,) = rows["call_events"]
    values = _as_dict("call_events", row)
    assert {k: v for k, v in values.items() if k != "data"} == {
        "org_id": ORG,
        "call_id": CALL,
        "seq": 7,
        "category": "tool",
        "level": "warning",
        "message": "tool failed",
        "created_at": AT,
    }
    assert orjson.loads(values["data"]) == {"tool_name": "lookup", "status_code": 500}


def test_an_explicit_category_wins_over_the_default() -> None:
    event = new_event(EventType.CALL_ERROR, org_id=ORG, clock=FrozenClock(AT), call_id=CALL, category="tool")
    (row,) = rows_for_event(event.model_copy(update={"seq": 1}))["call_events"]
    assert _as_dict("call_events", row)["category"] == "tool"


@pytest.mark.parametrize(
    ("event_type", "call_id", "seq"),
    [
        (EventType.CALL_TOOL, CALL, None),  # not yet sequenced
        (EventType.BATCH_CREATED, CALL, 3),  # no category
        (EventType.NUMBER_PURCHASED, None, None),  # not a call event
    ],
)
def test_events_without_a_postgres_row_produce_nothing(
    event_type: EventType, call_id: uuid.UUID | None, seq: int | None
) -> None:
    event = new_event(event_type, org_id=ORG, clock=FrozenClock(AT), call_id=call_id)
    assert rows_for_event(event.model_copy(update={"seq": seq})) == {}


def test_a_user_turn_has_null_latency() -> None:
    rows = rows_for_event(turn_event(CALL, 2, role="user", text="I need a table", node_id="greet"))
    assert sorted(rows) == ["call_events", "call_turns"]
    values = _as_dict("call_turns", rows["call_turns"][0])
    assert values == {
        "org_id": ORG,
        "call_id": CALL,
        "turn_index": 2,
        "role": "user",
        "node_id": "greet",
        "text_len": 14,
        "interrupted": 0,
        **dict.fromkeys(LATENCY_COLUMNS),
        "created_at": AT,
    }


def test_an_assistant_turn_carries_its_latency_subset() -> None:
    latency: dict[str, Any] = {"eot_ms": 300, "e2e_ms": 742, "tts_ttfa_ms": -3, "unknown": 1}
    event = turn_event(CALL, 3, latency=latency, interrupted=True)
    values = _as_dict("call_turns", rows_for_event(event)["call_turns"][0])
    assert (values["node_id"], values["interrupted"], values["text_len"]) == ("", 1, len("Hello there"))
    assert {k: values[k] for k in LATENCY_COLUMNS} == {
        **dict.fromkeys(LATENCY_COLUMNS),
        "eot_ms": 300,
        "e2e_ms": 742,
        "tts_ttfa_ms": 0,
    }


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        ({"role": "user"}, "call_turns:invalid_data"),
        ({"turn_index": 1, "role": 5}, "call_turns:invalid_data"),
        ({"turn_index": 1, "role": "user", "latency": {"e2e_ms": "fast"}}, "call_turns:invalid_data"),
        ({"turn_index": True, "role": "user"}, "call_turns:invalid_data"),
    ],
)
def test_malformed_turn_data_is_a_row_error(data: dict[str, Any], reason: str) -> None:
    event = new_event(EventType.CALL_TURN, org_id=ORG, clock=FrozenClock(AT), call_id=CALL, data=data)
    with pytest.raises(RowError) as caught:
        rows_for_event(event.model_copy(update={"seq": 1}))
    assert caught.value.reason == reason


def test_a_turn_without_a_call_id_is_a_row_error() -> None:
    event = new_event(EventType.CALL_TURN, org_id=ORG, clock=FrozenClock(AT), data={"turn_index": 1})
    with pytest.raises(RowError, match="call_turns:no_call_id"):
        rows_for_event(event)


def test_call_completed_becomes_a_calls_fact_row() -> None:
    rows = rows_for_event(completed_event(CALL, 30))
    assert sorted(rows) == ["call_events", "calls_fact"]
    values = _as_dict("calls_fact", rows["calls_fact"][0])
    assert values == {
        "org_id": ORG,
        "call_id": CALL,
        "direction": "outbound",
        "status": "completed",
        "answered_by": "human",
        "runner_type": "task",
        "pathway_id": None,
        "agent_id": None,
        "experiment_arm_id": None,
        "batch_id": None,
        "disposition_tag": "INTERESTED",
        "end_reason": "agent_end_call",
        "connected_seconds": 141.0,
        "transfer_seconds": 0.0,
        "price_micros": 282_000,
        "created_at": datetime(2026, 10, 24, 18, 20, 1, tzinfo=UTC),
        "ended_at": datetime(2026, 10, 24, 18, 22, 30, tzinfo=UTC),
    }


RUNNER = uuid.UUID("0193c5d2-0000-7000-8000-0000000000f1")


@pytest.mark.parametrize(
    ("over", "runner_type"),
    [
        ({"agent_id": str(RUNNER), "persona_id": str(RUNNER), "pathway_id": str(RUNNER)}, "agent"),
        ({"persona_id": str(RUNNER), "pathway_id": str(RUNNER)}, "persona"),
        ({"pathway_id": str(RUNNER)}, "pathway"),
        ({}, "task"),
    ],
)
def test_runner_type_precedence(over: dict[str, Any], runner_type: str) -> None:
    (row,) = rows_for_event(completed_event(CALL, 1, **over))["calls_fact"]
    assert _as_dict("calls_fact", row)["runner_type"] == runner_type


def test_nulls_become_empty_strings_and_fractions_round() -> None:
    batch = uuid.uuid4()
    over: dict[str, Any] = {
        "answered_by": None,
        "disposition_tag": None,
        "end_reason": None,
        "end_at": None,
        "batch_id": str(batch),
        "call_length": 1.0000004,
        "transfer_duration": 0.5,
        "price": 0.1234565,
    }
    values = _as_dict("calls_fact", rows_for_event(completed_event(CALL, 1, **over))["calls_fact"][0])
    assert (values["answered_by"], values["disposition_tag"], values["end_reason"]) == ("", "", "")
    assert (values["ended_at"], values["batch_id"]) == (None, batch)
    assert (values["connected_seconds"], values["transfer_seconds"], values["price_micros"]) == (
        60.0,
        30.0,
        123_456,
    )


def test_an_invalid_call_object_is_a_row_error() -> None:
    event = new_event(
        EventType.CALL_COMPLETED, org_id=ORG, clock=FrozenClock(AT), call_id=CALL, data={"call_id": str(CALL)}
    )
    with pytest.raises(RowError, match="calls_fact:invalid_call_object"):
        rows_for_event(event.model_copy(update={"seq": 1}))


def test_a_call_object_of_another_call_is_a_row_error() -> None:
    event = completed_event(CALL, 1)
    other = event.model_copy(update={"call_id": uuid.uuid4()})
    with pytest.raises(RowError, match="calls_fact:call_mismatch"):
        rows_for_event(other)


def test_billing_usage_becomes_a_usage_row() -> None:
    rows = rows_for_event(usage_event_of())
    assert list(rows) == ["usage_events_raw"]
    assert _as_dict("usage_events_raw", rows["usage_events_raw"][0]) == {
        "org_id": ORG,
        "meter": "call_connected_seconds",
        "quantity": 61.5,
        "amount_micros": 92_250,
        "cost_micros": 4_100,
        "reference_type": "call",
        "reference_id": "0193c5d2-7b1e-7a10-9a52-1f3b0d4e6a77",
        "occurred_at": AT,
    }


def test_invalid_usage_data_is_a_row_error() -> None:
    event = usage_event_of()
    broken = event.model_copy(update={"data": {**event.data, "meter": "minutes"}})
    with pytest.raises(RowError, match="usage_events_raw:invalid_data"):
        rows_for_event(broken)
