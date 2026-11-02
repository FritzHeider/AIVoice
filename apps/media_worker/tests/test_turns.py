"""``TurnRecorder`` and ``CallPersistence``: pipeline callbacks → ``call_turns`` ops and ``call.turn`` /
``call.interrupt`` / ``call.latency`` events (specs/07 §8 steps 2, 6, 7; specs/19 §2; specs/07 §8 Decision
P05-T11a)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from event_helpers import publisher_rig
from media_helpers import call_context
from service_helpers import json_log_stream, log_events

from va_common.time import FrozenClock
from va_db.call_turns import CallTurnRow, LatencyMerge, TurnDelete, TurnOp
from va_domain.call_context import CallContext
from va_events import DomainEvent, EventType
from va_media.turns import EVENT_MESSAGE_CHARS, CallPersistence, TurnRecorder
from va_voice.latency import LatencyEvent, TurnLatency, TurnReport, latency_events
from va_voice.pipeline import AgentTurn, InterruptRecord, UserTurn

ORIGIN_MS = 50_000.0


class RecordingTurns:
    def __init__(self, *, fail_flush: bool = False) -> None:
        self.ops: list[TurnOp] = []
        self.flushed: list[uuid.UUID] = []
        self.fail_flush = fail_flush

    def add(self, op: TurnOp) -> None:
        self.ops.append(op)

    async def flush_call(self, call_id: uuid.UUID) -> int:
        if self.fail_flush:
            raise ConnectionError
        self.flushed.append(call_id)
        return len(self.ops)


class RecordingEvents:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[DomainEvent] = []
        self.fail = fail

    async def publish(self, event: DomainEvent) -> DomainEvent:
        if self.fail:
            raise ConnectionError
        self.events.append(event)
        return event


def recorder(
    context: CallContext, *, offset_ms: float = 0.0, events: Any = None
) -> tuple[TurnRecorder, RecordingTurns, RecordingEvents]:
    turns = RecordingTurns()
    sink = events if events is not None else RecordingEvents()
    rec = TurnRecorder(
        context,
        turns=turns,
        events=sink,
        clock=FrozenClock(),
        origin_ms=ORIGIN_MS,
        answered_offset_ms=offset_ms,
    )
    return rec, turns, sink


def user_turn(index: int, text: str, start: float, end: float, **values: Any) -> UserTurn:
    return UserTurn(index=index, text=text, start_ms=ORIGIN_MS + start, end_ms=ORIGIN_MS + end, **values)


def interrupt(turn_index: int = 2) -> InterruptRecord:
    return InterruptRecord(
        turn_index=turn_index,
        at_ms=ORIGIN_MS + 2000,
        speech_start_ms=ORIGIN_MS + 1500,
        clear_ms=ORIGIN_MS + 2040.4,
        reaction_ms=540.4,
        clear_delay_ms=40.4,
        played_ms=1210.6,
        played_chars=18,
        total_chars=42,
        level=2,
        reason="speech",
    )


async def test_user_turn_row_and_event_are_relative_to_the_answer() -> None:
    context = call_context(uuid.uuid4())
    rec, turns, events = recorder(context, offset_ms=1500.0)
    rec.on_user_turn(user_turn(1, "I would like a table", 1000.4, 2600.6, language_code="en"))
    await rec.drain()
    assert turns.ops == [
        CallTurnRow(
            org_id=context.org_id,
            call_id=context.call_id,
            turn_index=1,
            role="user",
            text="I would like a table",
            start_ms=2500,
            end_ms=4101,
            language_code="en",
        )
    ]
    (event,) = events.events
    assert (event.type, event.org_id, event.call_id) == (EventType.CALL_TURN, context.org_id, context.call_id)
    assert event.message == "I would like a table"
    assert event.data == {
        "turn_index": 1,
        "role": "user",
        "text": "I would like a table",
        "node_id": None,
        "interrupted": False,
    }


async def test_times_before_the_answer_clamp_to_zero() -> None:
    rec, turns, _ = recorder(call_context(uuid.uuid4()), offset_ms=-300.0)
    rec.on_user_turn(user_turn(1, "hi", -200.0, 100.0))
    await rec.drain()
    (row,) = turns.ops
    assert isinstance(row, CallTurnRow)
    assert (row.start_ms, row.end_ms) == (0, 100)


async def test_a_superseding_turn_deletes_the_earlier_row_and_emits_one_event() -> None:
    context = call_context(uuid.uuid4())
    rec, turns, events = recorder(context)
    rec.on_user_turn(user_turn(3, "book a table for two", 100.0, 900.0, supersedes=1))
    await rec.drain()
    assert turns.ops[0] == TurnDelete(context.org_id, context.call_id, 1, "user")
    assert isinstance(turns.ops[1], CallTurnRow)
    assert (turns.ops[1].turn_index, turns.ops[1].text) == (3, "book a table for two")
    assert [e.data["turn_index"] for e in events.events] == [3]


async def test_a_keypad_turn_is_stored_with_its_text() -> None:
    rec, turns, events = recorder(call_context(uuid.uuid4()))
    rec.on_user_turn(user_turn(2, "[DTMF: 12#]", 0.0, 0.0, dtmf="12#"))
    await rec.drain()
    (row,) = turns.ops
    assert isinstance(row, CallTurnRow)
    assert row.text == "[DTMF: 12#]"
    assert events.events[0].message == "[DTMF: 12#]"


async def test_an_interrupted_agent_turn_keeps_the_spoken_part() -> None:
    context = call_context(uuid.uuid4())
    rec, turns, events = recorder(context)
    turn = AgentTurn(
        index=2,
        text="Sure, for how many people will that be?",
        text_spoken="Sure, for how",
        interrupted=True,
        started_ms=ORIGIN_MS + 3000,
        ended_ms=ORIGIN_MS + 3900,
    )
    rec.on_agent_turn(turn)
    await rec.drain()
    (row,) = turns.ops
    assert isinstance(row, CallTurnRow)
    assert (row.role, row.text, row.text_spoken, row.interrupted, row.start_ms, row.end_ms) == (
        "assistant",
        "Sure, for how many people will that be?",
        "Sure, for how",
        True,
        3000,
        3900,
    )
    (event,) = events.events
    assert event.data["role"] == "assistant"
    assert event.data["interrupted"] is True
    assert event.data["text"] == turn.text


async def test_long_turn_text_is_cut_in_the_message_only() -> None:
    rec, _, events = recorder(call_context(uuid.uuid4()))
    text = "word " * 100
    rec.on_user_turn(user_turn(1, text, 0.0, 1.0))
    await rec.drain()
    (event,) = events.events
    assert len(event.message) == EVENT_MESSAGE_CHARS
    assert event.data["text"] == text


async def test_latency_merges_onto_the_user_row_and_emits_every_event() -> None:
    context = call_context(uuid.uuid4())
    rec, turns, events = recorder(context)
    latency = TurnLatency(eot_ms=300, stt_final_ms=63, llm_ttft_ms=266, tts_ttfa_ms=218, e2e_ms=742)
    rec.on_latency(TurnReport(latency, latency_events(latency, 3), 3))
    await rec.drain()
    assert turns.ops == [LatencyMerge(context.org_id, context.call_id, 3, "user", latency.as_dict())]
    assert [(e.type, e.message) for e in events.events] == [
        (EventType.CALL_LATENCY, "E2E: 742ms"),
        (EventType.CALL_LATENCY, "STT: 63ms"),
        (EventType.CALL_LATENCY, "LLM: 266ms"),
        (EventType.CALL_LATENCY, "TTS: 218ms"),
    ]
    assert events.events[0].data == {**latency.as_dict(), "turn_index": 3}


async def test_latency_without_e2e_emits_only_stage_events() -> None:
    rec, turns, events = recorder(call_context(uuid.uuid4()))
    latency = TurnLatency(stt_final_ms=80)
    rec.on_latency(TurnReport(latency, latency_events(latency, 1), 1))
    await rec.drain()
    assert [e.message for e in events.events] == ["STT: 80ms"]
    assert len(turns.ops) == 1


async def test_an_unmeasured_turn_writes_no_latency() -> None:
    rec, turns, events = recorder(call_context(uuid.uuid4()))
    rec.on_latency(TurnReport(TurnLatency(), [], 1))
    await rec.drain()
    assert (turns.ops, events.events) == ([], [])


async def test_interrupt_event_carries_the_played_share() -> None:
    rec, turns, events = recorder(call_context(uuid.uuid4()))
    rec.on_interrupt(interrupt())
    await rec.drain()
    assert turns.ops == []
    (event,) = events.events
    assert event.type == EventType.CALL_INTERRUPT
    assert event.data == {
        "turn_index": 2,
        "played_chars": 18,
        "total_chars": 42,
        "played_ms": 1211,
        "reaction_ms": 540,
    }
    assert event.message == "Interrupted after 18 of 42 characters"


async def test_events_are_published_in_callback_order_with_gap_free_seqs() -> None:
    publisher = publisher_rig(router=False)
    context = call_context(uuid.uuid4())
    rec, _, _ = recorder(context, events=publisher.publisher)
    for index in range(1, 6):
        rec.on_user_turn(user_turn(index, f"turn {index}", 0.0, 1.0))
    rec.on_interrupt(interrupt(5))
    await rec.drain()
    rows = [row for batch in publisher.writer.batches for row in batch]
    assert [row.seq for row in rows] == [1, 2, 3, 4, 5, 6]
    assert [row.data.get("text") for row in rows[:5]] == [f"turn {i}" for i in range(1, 6)]
    assert rows[5].category == "interrupt"


async def test_a_redis_outage_is_logged_without_text_and_never_raised() -> None:
    publisher = publisher_rig(router=False)
    publisher.server.connected = False
    stream = json_log_stream()
    rec, turns, _ = recorder(call_context(uuid.uuid4()), events=publisher.publisher)
    rec.on_user_turn(user_turn(1, "my secret account number", 0.0, 1.0))
    rec.on_latency(TurnReport(TurnLatency(e2e_ms=700), [LatencyEvent("E2E: 700ms", {"e2e_ms": 700})], 1))
    await rec.drain()
    assert len(turns.ops) == 2
    failures = [e for e in log_events(stream) if e["event"] == "media.event_publish_failed"]
    assert [(e["event_type"], e["error"]) for e in failures] == [
        ("call.turn", "EventPublishError"),
        ("call.latency", "EventPublishError"),
    ]
    assert "secret" not in stream.getvalue()


async def test_drain_without_events_returns_at_once() -> None:
    rec, _, _ = recorder(call_context(uuid.uuid4()))
    await rec.drain()


async def test_persistence_recorder_measures_from_the_answer_time() -> None:
    clock = FrozenClock()
    turns = RecordingTurns()
    persistence = CallPersistence(turns=turns, events=RecordingEvents())
    context = call_context(uuid.uuid4())
    rec = persistence.recorder(context, clock=clock, answered_at=clock.now() - timedelta(milliseconds=1200))
    now_ms = clock.monotonic() * 1000.0
    rec.on_user_turn(UserTurn(index=1, text="hi", start_ms=now_ms + 300, end_ms=now_ms + 800))
    unknown = persistence.recorder(context, clock=clock)
    unknown.on_user_turn(UserTurn(index=2, text="hi", start_ms=now_ms + 300, end_ms=now_ms + 800))
    rows = [op for op in turns.ops if isinstance(op, CallTurnRow)]
    assert [(r.start_ms, r.end_ms) for r in rows] == [(1500, 2000), (300, 800)]
    await rec.drain()
    await unknown.drain()


async def test_finish_call_drains_then_flushes_turns_and_event_rows() -> None:
    turns = RecordingTurns()
    rows = RecordingTurns()
    events = RecordingEvents()
    persistence = CallPersistence(turns=turns, events=events, event_rows=rows)
    context = call_context(uuid.uuid4())
    rec = persistence.recorder(context, clock=FrozenClock())
    rec.on_user_turn(UserTurn(index=1, text="hi", start_ms=0.0, end_ms=1.0))
    await persistence.finish_call(rec, context)
    assert len(events.events) == 1
    assert turns.flushed == [context.call_id]
    assert rows.flushed == [context.call_id]


@pytest.mark.parametrize("failing", ["turns", "call_events"])
async def test_finish_call_logs_a_failed_flush(failing: str) -> None:
    turns = RecordingTurns(fail_flush=failing == "turns")
    rows = RecordingTurns(fail_flush=failing == "call_events")
    persistence = CallPersistence(turns=turns, events=RecordingEvents(), event_rows=rows)
    context = call_context(uuid.uuid4())
    stream = json_log_stream()
    await persistence.finish_call(persistence.recorder(context, clock=FrozenClock()), context)
    failures = [e for e in log_events(stream) if e["event"] == "media.call_flush_failed"]
    assert [(e["target"], e["error"]) for e in failures] == [(failing, "ConnectionError")]
    assert len(turns.flushed) + len(rows.flushed) == 1


async def test_finish_call_without_an_event_buffer_flushes_turns_only() -> None:
    turns = RecordingTurns()
    persistence = CallPersistence(turns=turns, events=RecordingEvents(fail=True))
    context = call_context(uuid.uuid4())
    rec = persistence.recorder(context, clock=FrozenClock())
    rec.on_agent_turn(AgentTurn(0, "Hi", "Hi", interrupted=False, started_ms=0.0, ended_ms=1.0))
    await persistence.finish_call(rec, context)
    assert turns.flushed == [context.call_id]
