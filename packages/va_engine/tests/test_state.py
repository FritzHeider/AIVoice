"""`EngineState` serialization and history cap (specs/11 §9 Decision P05-T09a)."""

from __future__ import annotations

import uuid

import orjson
import pytest
from pydantic import ValidationError

from va_engine.models import (
    HISTORY_CAP,
    HISTORY_SUMMARY_MAX_CHARS,
    EngineState,
    NodeFrame,
    TurnRecord,
    load_engine_state,
)


def _turn(index: int, content: str | None = None) -> TurnRecord:
    return TurnRecord(
        role="user" if index % 2 == 0 else "assistant",
        content=content or f"turn {index}",
        node_id=None,
        at_ms=index * 1000,
    )


def _state() -> EngineState:
    return EngineState(runner="task", pathway_id=None, pathway_version=None, current_node_id="task")


def test_round_trip() -> None:
    pathway = uuid.uuid4()
    state = EngineState(
        runner="pathway",
        pathway_id=pathway,
        pathway_version=3,
        current_node_id="n2",
        return_stack=[NodeFrame(pathway_id=pathway, version=3, node_id="n1")],
        visit_counts={"n1": 2},
        variables={"name": "Al", "nested": {"a": [1, None, True]}},
        history=[
            _turn(0),
            TurnRecord(
                role="tool",
                content="{}",
                node_id="n2",
                tool_call_id="c1",
                tool_name="lookup",
                at_ms=5,
                interrupted=True,
            ),
        ],
        history_summary="user: earlier",
        wait_reprompts=1,
        turn_index=4,
        ended=True,
        end_reason="agent",
        graph_stack_versions={str(pathway): 3},
    )

    assert load_engine_state(state.model_dump_json()) == state
    assert load_engine_state(state.model_dump_json().encode()) == state


def test_defaults() -> None:
    state = _state()

    assert state.schema_version == 1
    assert (state.return_stack, state.visit_counts, state.variables, state.history) == ([], {}, {}, [])
    assert (state.history_summary, state.wait_reprompts, state.turn_index, state.ended, state.end_reason) == (
        "",
        0,
        0,
        False,
        None,
    )


def test_unknown_schema_version_is_rejected() -> None:
    raw = orjson.loads(_state().model_dump_json())
    raw["schema_version"] = 2

    with pytest.raises(ValidationError, match="schema_version"):
        load_engine_state(orjson.dumps(raw))


@pytest.mark.parametrize("model", ["state", "turn"])
def test_unknown_fields_are_rejected(model: str) -> None:
    raw = orjson.loads(_state().model_copy(update={"history": [_turn(0)]}).model_dump_json())
    if model == "state":
        raw["surprise"] = 1
    else:
        raw["history"][0]["surprise"] = 1

    with pytest.raises(ValidationError, match="surprise"):
        load_engine_state(orjson.dumps(raw))


def test_history_keeps_the_cap() -> None:
    state = _state()
    for index in range(HISTORY_CAP):
        state.add_turn(_turn(index))

    assert len(state.history) == HISTORY_CAP
    assert state.history_summary == ""

    state.add_turn(_turn(HISTORY_CAP))

    assert len(state.history) == HISTORY_CAP
    assert state.history[0].content == "turn 1"
    assert state.history[-1].content == f"turn {HISTORY_CAP}"
    assert state.history_summary == "user: turn 0"

    state.add_turn(_turn(HISTORY_CAP + 1))

    assert state.history_summary == "user: turn 0\nassistant: turn 1"


def test_history_summary_keeps_its_tail() -> None:
    state = _state()
    for index in range(HISTORY_CAP + 3):
        state.add_turn(_turn(index, content=f"{index:03d}" + "x" * 2000))

    assert len(state.history_summary) == HISTORY_SUMMARY_MAX_CHARS
    assert state.history_summary.endswith("user: 002" + "x" * 2000)
    assert "000" not in state.history_summary
