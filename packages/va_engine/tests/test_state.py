"""`EngineState` versioning, pending actions, history cap (specs/11 §9 P05-T09a, P07-T03; §4 P07-T05)."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import orjson
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import JsonValue, ValidationError

from va_engine.models import (
    CURRENT_SCHEMA_VERSION,
    HISTORY_CAP,
    HISTORY_SUMMARY_MAX_CHARS,
    TERMINAL_ACTION_KINDS,
    EngineState,
    EngineStateError,
    NodeFrame,
    PendingAction,
    PendingDtmf,
    PendingEndCall,
    PendingHumanTakeover,
    PendingSay,
    PendingSetVariables,
    PendingSms,
    PendingSwitchGraph,
    PendingTransfer,
    TurnRecord,
    dump_engine_state,
    load_engine_state,
)

FIXTURES = Path(__file__).parent / "fixtures" / "engine_state"
PATHWAY = uuid.UUID("0190f5a2-7c3e-7b1a-9d2e-4f6a8b0c1d2e")


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
        return_stack=[NodeFrame(kind="global", pathway_id=pathway, version=3, node_id="n1")],
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

    assert load_engine_state(dump_engine_state(state)) == state
    assert load_engine_state(dump_engine_state(state).encode()) == state


def test_defaults() -> None:
    state = _state()

    assert state.schema_version == CURRENT_SCHEMA_VERSION == 3
    assert (state.pending_actions, state.wait_armed_at_ms) == ([], None)
    assert (state.return_stack, state.visit_counts, state.variables, state.history) == ([], {}, {}, [])
    assert (state.history_summary, state.wait_reprompts, state.turn_index, state.ended, state.end_reason) == (
        "",
        0,
        0,
        False,
        None,
    )


@pytest.mark.parametrize("model", ["state", "turn", "pending"])
def test_unknown_fields_are_rejected(model: str) -> None:
    state = _state().model_copy(update={"history": [_turn(0)], "pending_actions": [PendingHumanTakeover()]})
    raw = orjson.loads(dump_engine_state(state))
    if model == "state":
        raw["surprise"] = 1
    elif model == "turn":
        raw["history"][0]["surprise"] = 1
    else:
        raw["pending_actions"][0]["surprise"] = 1

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


def _fixture_text(version: int) -> str:
    return (FIXTURES / f"v{version}.json").read_text()


def test_v1_fixture_upgrades() -> None:
    raw = orjson.loads(_fixture_text(1))

    state = load_engine_state(_fixture_text(1))

    assert state.schema_version == CURRENT_SCHEMA_VERSION
    assert (state.pending_actions, state.wait_armed_at_ms) == ([], None)
    dumped = orjson.loads(dump_engine_state(state))
    unchanged = {"schema_version", "return_stack"}
    assert {key: value for key, value in dumped.items() if key in raw and key not in unchanged} == {
        key: value for key, value in raw.items() if key not in unchanged
    }
    assert state.return_stack == [
        NodeFrame(kind="global", pathway_id=PATHWAY, version=4, node_id="greeting"),
        NodeFrame(kind="transfer_pathway", pathway_id=None, version=None, node_id="faq"),
    ]
    assert state.history[1].interrupted is True
    assert load_engine_state(dump_engine_state(state)) == state


def test_v3_fixture_is_canonical() -> None:
    text = _fixture_text(3)

    state = load_engine_state(text)

    assert dump_engine_state(state) == orjson.dumps(orjson.loads(text)).decode()
    assert [action.kind for action in state.pending_actions] == [
        "say",
        "send_dtmf",
        "set_variables",
        "send_sms",
        "switch_graph",
        "human_takeover",
        "transfer",
        "end_call",
    ]
    assert state.wait_armed_at_ms == 3100
    assert [frame.kind for frame in state.return_stack] == ["global", "transfer_pathway"]


def test_v2_fixture_upgrades_frames_with_derived_kinds() -> None:
    upgraded = load_engine_state(_fixture_text(2))

    assert upgraded == load_engine_state(_fixture_text(3))


def test_v1_fixture_matches_v3_fixture_except_v2_fields() -> None:
    upgraded = load_engine_state(_fixture_text(1))
    current = load_engine_state(_fixture_text(3))

    assert upgraded == current.model_copy(update={"pending_actions": [], "wait_armed_at_ms": None})


@pytest.mark.parametrize(
    ("state_pathway", "frame_pathway", "kind"),
    [
        (str(PATHWAY), str(PATHWAY), "global"),
        (str(PATHWAY), str(PATHWAY).upper(), "global"),
        (None, None, "global"),
        (str(PATHWAY), None, "transfer_pathway"),
        (None, str(PATHWAY), "transfer_pathway"),
        (str(PATHWAY), "0190f5a2-7c3e-7b1a-9d2e-4f6a8b0c1d2f", "transfer_pathway"),
    ],
)
def test_v2_frame_kind_comes_from_the_frame_pathway(
    state_pathway: str | None, frame_pathway: str | None, kind: str
) -> None:
    raw = orjson.loads(_fixture_text(2))
    raw["pathway_id"] = state_pathway
    raw["return_stack"] = [{"pathway_id": frame_pathway, "version": None, "node_id": "a"}]

    state = load_engine_state(raw)

    assert [frame.kind for frame in state.return_stack] == [kind]


def test_v2_without_return_stack_upgrades() -> None:
    raw = orjson.loads(_fixture_text(2))
    del raw["return_stack"]

    assert load_engine_state(raw).return_stack == []


def test_v2_with_frame_kind_is_rejected() -> None:
    raw = orjson.loads(_fixture_text(2))
    raw["return_stack"][0]["kind"] = "global"

    with pytest.raises(EngineStateError, match=r"return_stack\.kind") as caught:
        load_engine_state(raw)

    assert caught.value.version == 2


def test_v2_malformed_stack_is_left_to_validation() -> None:
    raw = orjson.loads(_fixture_text(2))
    raw["return_stack"] = ["greeting"]

    with pytest.raises(ValidationError, match="return_stack"):
        load_engine_state(raw)


def test_frame_kind_is_required() -> None:
    raw = orjson.loads(_fixture_text(3))
    del raw["return_stack"][0]["kind"]

    with pytest.raises(ValidationError, match="kind"):
        load_engine_state(raw)


PENDING: list[PendingAction] = [
    PendingSay(text="Hold on.", mark="m1", interruptible=True),
    PendingDtmf(digits="123", after_mark="m1"),
    PendingTransfer(mode="warm", target="+14155550111", caller_id_mode="original_caller"),
    PendingEndCall(reason="user_hangup", ended_by="user", after_mark="m2"),
    PendingSms(to="+14155550100", from_="+16285550199", body="hi"),
    PendingSetVariables(variables={"a": [1, {"b": None}]}),
    PendingSwitchGraph(pathway_id=PATHWAY),
    PendingHumanTakeover(),
]


@pytest.mark.parametrize("action", PENDING, ids=lambda action: action.kind)
@pytest.mark.parametrize("form", ["str", "bytes", "dict"])
def test_round_trip_every_pending_kind(action: PendingAction, form: str) -> None:
    state = _state().model_copy(update={"pending_actions": [action], "wait_armed_at_ms": 12})
    text = dump_engine_state(state)
    data: str | bytes | dict[str, Any] = {"str": text, "bytes": text.encode(), "dict": orjson.loads(text)}[
        form
    ]

    assert load_engine_state(data) == state


def test_pending_kinds_cover_terminal_kinds() -> None:
    assert {action.kind for action in PENDING} >= TERMINAL_ACTION_KINDS


def test_sms_from_alias() -> None:
    sms = PendingSms(to="+14155550100", from_="+16285550199", body="hi")
    state = _state().model_copy(update={"pending_actions": [sms]})
    raw = orjson.loads(dump_engine_state(state))

    assert raw["pending_actions"][0]["from"] == "+16285550199"
    assert "from_" not in raw["pending_actions"][0]
    raw["pending_actions"][0]["from_"] = raw["pending_actions"][0].pop("from")
    assert load_engine_state(raw) == state


@pytest.mark.parametrize("version", [1, 2])
def test_load_does_not_mutate_input(version: int) -> None:
    raw = orjson.loads(_fixture_text(version))
    before = orjson.loads(_fixture_text(version))

    load_engine_state(raw)

    assert raw == before


@pytest.mark.parametrize("version", [0, -1, 4, "1", 2.0, True, None])
def test_rejects_bad_versions(version: object) -> None:
    raw = orjson.loads(dump_engine_state(_state()))
    raw["schema_version"] = version

    with pytest.raises(EngineStateError, match="unsupported engine state schema_version") as caught:
        load_engine_state(raw)

    assert caught.value.version == version


def test_rejects_missing_version() -> None:
    raw = orjson.loads(dump_engine_state(_state()))
    del raw["schema_version"]

    with pytest.raises(EngineStateError, match="no schema_version") as caught:
        load_engine_state(raw)

    assert caught.value.version is None


@pytest.mark.parametrize(
    ("data", "message"), [("[]", "object"), ("1", "object"), (b"null", "object"), ("{", "JSON")]
)
def test_rejects_non_objects(data: str | bytes, message: str) -> None:
    with pytest.raises(EngineStateError, match=message):
        load_engine_state(data)


@pytest.mark.parametrize("field", ["pending_actions", "wait_armed_at_ms"])
def test_v1_with_v2_fields_is_rejected(field: str) -> None:
    raw = orjson.loads(_fixture_text(1))
    raw[field] = None

    with pytest.raises(EngineStateError, match=field) as caught:
        load_engine_state(raw)

    assert caught.value.version == 1


def test_invalid_pending_action_is_rejected() -> None:
    raw = orjson.loads(dump_engine_state(_state()))
    raw["pending_actions"] = [{"kind": "send_dtmf", "digits": ""}]

    with pytest.raises(ValidationError, match="digits"):
        load_engine_state(raw)


def test_take_pending() -> None:
    say = PendingSay(text="a", mark="m0")
    dtmf = PendingDtmf(digits="1", after_mark="m1")
    end = PendingEndCall(reason="done", after_mark="m2")
    takeover = PendingHumanTakeover()
    state = _state().model_copy(update={"pending_actions": [say, dtmf, end, takeover]})

    assert state.take_pending() == [say, takeover]
    assert state.pending_actions == [dtmf, end]
    assert state.take_pending("m2") == [end]
    assert state.take_pending("m3") == []
    assert state.take_pending("m1") == [dtmf]
    assert state.pending_actions == []


_ids = st.text(alphabet="abcdefgh_0123456789", min_size=1, max_size=8)
_json: st.SearchStrategy[JsonValue] = st.recursive(
    st.none()
    | st.booleans()
    | st.integers(-(2**53), 2**53)
    | st.floats(allow_nan=False, allow_infinity=False)
    | st.text(max_size=12),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=6), children, max_size=3)
    ),
    max_leaves=8,
)
_turns = st.builds(
    TurnRecord,
    role=st.sampled_from(["user", "assistant", "tool", "system"]),
    content=st.text(max_size=20),
    node_id=st.none() | _ids,
    interrupted=st.booleans(),
    tool_call_id=st.none() | _ids,
    tool_name=st.none() | _ids,
    at_ms=st.integers(0, 10**9),
)
_marks = st.none() | _ids
_actions: st.SearchStrategy[PendingAction] = st.one_of(
    st.builds(
        PendingSay,
        text=st.text(max_size=20),
        mark=_ids,
        after_mark=_marks,
        interruptible=st.none() | st.booleans(),
    ),
    st.builds(
        PendingDtmf, digits=st.text(alphabet="0123456789*#w", min_size=1, max_size=6), after_mark=_marks
    ),
    st.builds(
        PendingTransfer,
        target=st.text(max_size=16),
        mode=st.sampled_from(["cold", "warm"]),
        after_mark=_marks,
    ),
    st.builds(PendingEndCall, reason=_ids, after_mark=_marks),
    st.builds(PendingSms, to=_ids, from_=_ids, body=st.text(max_size=20), after_mark=_marks),
    st.builds(PendingSetVariables, variables=st.dictionaries(_ids, _json, max_size=3), after_mark=_marks),
    st.builds(
        PendingSwitchGraph, pathway_id=st.uuids(), version=st.none() | st.integers(1, 99), after_mark=_marks
    ),
    st.builds(PendingHumanTakeover, after_mark=_marks),
)
_states = st.builds(
    EngineState,
    runner=st.sampled_from(["task", "pathway"]),
    pathway_id=st.none() | st.uuids(),
    pathway_version=st.none() | st.integers(1, 99),
    current_node_id=_ids,
    return_stack=st.lists(
        st.builds(
            NodeFrame,
            kind=st.sampled_from(["global", "transfer_pathway"]),
            pathway_id=st.none() | st.uuids(),
            version=st.none() | st.integers(1, 9),
            node_id=_ids,
        ),
        max_size=3,
    ),
    visit_counts=st.dictionaries(_ids, st.integers(0, 50), max_size=3),
    variables=st.dictionaries(_ids, _json, max_size=4),
    history=st.lists(_turns, max_size=4),
    history_summary=st.text(max_size=30),
    wait_reprompts=st.integers(0, 5),
    turn_index=st.integers(0, 500),
    ended=st.booleans(),
    end_reason=st.none() | _ids,
    graph_stack_versions=st.dictionaries(_ids, st.integers(1, 9), max_size=2),
    pending_actions=st.lists(_actions, max_size=4),
    wait_armed_at_ms=st.none() | st.integers(0, 10**9),
)


@settings(max_examples=100, deadline=None)
@given(_states)
def test_round_trip_property(state: EngineState) -> None:
    assert load_engine_state(dump_engine_state(state)) == state
