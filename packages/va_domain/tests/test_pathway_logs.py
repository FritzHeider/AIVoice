"""Deterministic pathway_logs builder (specs/11 §12; Decision P07-T08c)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, Final

from va_domain.call_object import LogEvent, LogTurn, build_pathway_logs, pathway_decision
from va_domain.call_object.pathway_logs import event_turn_index

PATHWAY: Final = uuid.UUID("0191f2a4-7c55-7a1e-9a10-5b2e4c8f1a99")
AT: Final = datetime(2026, 10, 24, 18, 22, 31, 412_987, tzinfo=UTC)
EMPTY: Final[dict[str, object]] = {
    "route": None,
    "global": False,
    "loop_condition": None,
    "extracted": None,
    "tools": [],
}


def _turn(index: int, role: str, text: str, node: str | None = None, *, at: datetime = AT) -> LogTurn:
    return LogTurn(
        turn_index=index,
        role=role,
        text=text,
        node_id=node,
        node_name=None if node is None else node.title(),
        created_at=at,
    )


def _route(index: Any, to: str, *, color: str | None = None, global_: bool = False) -> LogEvent:
    return LogEvent(
        event_type="call.route",
        data={
            "turn_index": index,
            "from_node": "start",
            "to_node": to,
            "label": f"to {to}",
            "alternatives": [{"label": "other", "score": 3}],
            "global": global_,
            "router_ms": 80,
            "tag_color": color,
        },
    )


def _logs(turns: list[LogTurn], events: list[LogEvent]) -> list[dict[str, Any]]:
    return [
        entry.model_dump(mode="json")
        for entry in build_pathway_logs(turns, events, pathway_id=PATHWAY, pathway_version=3)
    ]


def test_every_decision_key_is_present_without_events() -> None:
    assert pathway_decision([]) == EMPTY


def test_the_last_route_and_condition_win_and_extractions_merge() -> None:
    events = [
        _route(1, "a", global_=True),
        LogEvent(event_type="call.loop_condition", data={"score": 3, "met": False, "message": "x"}),
        LogEvent(event_type="call.extraction", data={"variables": {"name": "Ann", "size": "S"}}),
        LogEvent(event_type="call.extraction", data={"variables": {"size": "L"}}),
        LogEvent(event_type="call.extraction", data={"variables": "broken"}),
        LogEvent(event_type="call.loop_condition", data={"score": 8, "met": True}),
        _route(1, "b"),
    ]
    assert pathway_decision(events) == {
        "route": {
            "from": "start",
            "to": "b",
            "label": "to b",
            "alternatives": [{"label": "other", "score": 3}],
        },
        "global": False,
        "loop_condition": {"score": 8, "met": True},
        "extracted": {"name": "Ann", "size": "L"},
        "tools": [],
    }


def test_a_global_route_and_missing_alternatives() -> None:
    event = LogEvent(event_type="call.route", data={"to_node": "g", "global": True, "alternatives": None})
    decision = pathway_decision([event])
    assert decision["route"] == {"from": None, "to": "g", "label": None, "alternatives": []}
    assert decision["global"] is True


def test_tool_names_fall_back_from_tool_to_node_name_to_node_id() -> None:
    events = [
        LogEvent(
            event_type="call.tool", data={"tool_name": "lookup", "status_code": 200, "duration_ms": 184}
        ),
        LogEvent(
            event_type="call.webhook_node", data={"node_name": "CRM", "node_id": "n1", "status_code": 500}
        ),
        LogEvent(event_type="call.webhook_node", data={"node_id": "n2", "duration_ms": 9}),
        LogEvent(event_type="call.sms_sent", data={"node_id": "n3"}),
    ]
    assert pathway_decision(events)["tools"] == [
        {"name": "lookup", "status": 200, "ms": 184},
        {"name": "CRM", "status": 500, "ms": None},
        {"name": "n2", "status": None, "ms": 9},
    ]


def test_turn_index_must_be_a_real_integer() -> None:
    assert event_turn_index({"turn_index": 2}) == 2
    assert event_turn_index({"turn_index": True}) is None
    assert event_turn_index({"turn_index": "2"}) is None
    assert event_turn_index({}) is None


def test_entries_pair_user_text_and_carry_the_tag_colour_over() -> None:
    turns = [
        _turn(0, "assistant", "Hi.", "start"),
        _turn(1, "user", "Order please."),
        _turn(1, "assistant", "Sure. ", "order", at=AT + timedelta(seconds=1)),
        _turn(1, "assistant", "  ", "order"),
        _turn(1, "assistant", "What size?", "size", at=AT + timedelta(seconds=2)),
        _turn(1, "tool", "ok", "size"),
        _turn(2, "user", "Large."),
        _turn(2, "assistant", "Got it.", "size"),
        _turn(3, "user", "Bye."),
        _turn(4, "assistant", "Said without a node."),
    ]
    events = [
        _route(1, "order", color="#16A34A"),
        _route(1, "size", color="#2563EB"),
        _route(True, "ignored", color="#000000"),
        _route(3, "end", color=None),
        LogEvent(event_type="call.sms_sent", data={"turn_index": 2}),
    ]
    logs = _logs(turns, events)
    assert [(log["node_id"], log["tag"], log["text"], log["user_text"]) for log in logs] == [
        ("start", {"name": "Start", "color": None}, "Hi.", None),
        ("size", {"name": "Size", "color": "#2563EB"}, "Sure. What size?", "Order please."),
        ("size", {"name": "Size", "color": "#2563EB"}, "Got it.", "Large."),
    ]
    assert [log["decision"]["route"] and log["decision"]["route"]["to"] for log in logs] == [
        None,
        "size",
        None,
    ]
    assert logs[1]["created_at"] == "2026-10-24T18:22:32.412Z"
    assert logs[0] == {
        "tag": {"name": "Start", "color": None},
        "node_id": "start",
        "pathway_id": str(PATHWAY),
        "pathway_version": 3,
        "role": "assistant",
        "text": "Hi.",
        "user_text": None,
        "decision": EMPTY,
        "created_at": "2026-10-24T18:22:31.412Z",
    }


def test_a_route_turn_without_speech_still_sets_the_colour() -> None:
    turns = [_turn(1, "user", "x"), _turn(2, "assistant", "Later.", "b")]
    logs = _logs(turns, [_route(1, "b", color="#111111")])
    assert [(log["tag"]["color"], log["user_text"]) for log in logs] == [("#111111", None)]


def test_created_at_is_utc_milliseconds() -> None:
    eastern = datetime(2026, 10, 24, 14, 0, 0, 5_000, tzinfo=timezone(timedelta(hours=-4)))
    naive = datetime(2026, 10, 24, 18, 0, 0)  # noqa: DTZ001 - naive values are read as UTC
    logs = _logs([_turn(0, "assistant", "a", "n", at=eastern), _turn(1, "assistant", "b", "n", at=naive)], [])
    assert [log["created_at"] for log in logs] == ["2026-10-24T18:00:00.005Z", "2026-10-24T18:00:00.000Z"]
    python_mode = build_pathway_logs(
        [_turn(0, "assistant", "a", "n")], [], pathway_id=None, pathway_version=None
    )
    assert python_mode[0].model_dump()["created_at"] == AT
