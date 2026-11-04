"""``va_db.call_object`` on a migrated database: every Call Object field from its source rows, defaults of a
bare call, list items, batched reads, and RLS (specs/19 §4; Decision P05-T16a)."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final

import pytest
from call_rows import SeededCall, seed_call, seed_leg
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from tenant_rows import OrgFactory, RowFactory, open_database

from va_common.ids import new_id
from va_common.storage import SignedUrl
from va_db.call_object import load_call_list_items, load_call_objects
from va_db.models import (
    AgentVersion,
    Call,
    CallAnalysis,
    CallEvent,
    CallTurn,
    CallTurnRole,
    Citation,
    CitationSchema,
    EvalRun,
    EvalSetupVersion,
    ExperimentArm,
    GuardRailEvent,
)
from va_db.session import Database
from va_domain.call_object import CallListItem, CallObject

pytestmark = pytest.mark.integration

ANSWERED: Final = datetime(2026, 10, 24, 18, 20, 9, tzinfo=UTC)
ENDED: Final = ANSWERED + timedelta(seconds=141)
RECORDING: Final = SignedUrl("https://s3.test/rec.wav?sig=1", ENDED + timedelta(days=7))


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


@dataclass(frozen=True, slots=True)
class Runner:
    agent_id: uuid.UUID
    agent_version_id: uuid.UUID
    experiment_id: uuid.UUID
    experiment_arm_id: uuid.UUID


async def _insert(session: AsyncSession, model: Any, org_id: uuid.UUID, **values: Any) -> dict[str, Any]:
    factory = RowFactory()
    table = model.__table__
    row = {**await factory.values(session, table, org_id), **values}
    await factory.insert_values(session, table, row)
    return row


async def _runner(database: Database, org_id: uuid.UUID) -> Runner:
    async with database.tenant_session(org_id) as session:
        version = await _insert(session, AgentVersion, org_id, id=new_id(), semver="1.2.0")
        arm = await _insert(
            session, ExperimentArm, org_id, id=new_id(), agent_version_id=version["id"], label="B"
        )
    return Runner(version["agent_id"], version["id"], arm["experiment_id"], arm["id"])


def _turn(
    call: SeededCall, index: int, role: CallTurnRole, text: str, *, at: int | None = None, **values: Any
) -> CallTurn:
    second = index if at is None else at
    return CallTurn(
        id=new_id(),
        org_id=call.org_id,
        call_id=call.call_id,
        turn_index=index,
        role=role,
        text_=text,
        start_ms=second * 1000,
        end_ms=second * 1000 + 800,
        **values,
    )


def _event(
    call: SeededCall, seq: int, category: Any, data: dict[str, Any], event_type: str | None = None
) -> CallEvent:
    return CallEvent(
        id=new_id(),
        org_id=call.org_id,
        call_id=call.call_id,
        seq=seq,
        category=category,
        event_type=event_type,
        message="m",
        data=data,
    )


def _log_events(call: SeededCall) -> list[CallEvent]:
    """Turn 1's decision events, plus rows the projection must skip (pre-0007, non-integer turn index)."""
    route = {
        "turn_index": 1,
        "from_node": "greet",
        "to_node": "pitch",
        "label": "Interested",
        "alternatives": [{"label": "Busy", "score": 2}],
        "global": False,
        "router_ms": 120,
        "tag_color": "#16A34A",
    }
    return [
        _event(call, 1, "route", {**route, "tag_color": "#000000"}),
        _event(call, 2, "route", {"turn_index": 1, "score": 7, "met": True}, "call.loop_condition"),
        _event(
            call, 3, "extraction", {"turn_index": 1, "variables": {"first_name": "Mike"}}, "call.extraction"
        ),
        _event(call, 4, "route", route, "call.route"),
        _event(
            call,
            5,
            "webhook",
            {"turn_index": 1, "node_id": "hook", "node_name": "CRM", "status_code": 200, "duration_ms": 184},
            "call.webhook_node",
        ),
        _event(call, 6, "route", {**route, "turn_index": "1", "tag_color": "#FFFFFF"}, "call.route"),
    ]


async def _populated(database: Database) -> tuple[SeededCall, Runner]:
    org_id = await OrgFactory(database).create()
    runner = await _runner(database, org_id)
    call = await seed_call(
        database,
        org_id=org_id,
        status="completed",
        queue_status="complete",
        answered_by="human",
        ended_by="agent",
        end_reason="agent_end_call",
        external_id="crm-42",
        runner_type="agent",
        persona_version=3,
        agent_id=runner.agent_id,
        agent_version_id=runner.agent_version_id,
        experiment_id=runner.experiment_id,
        experiment_arm_id=runner.experiment_arm_id,
        test_call=True,
        config={
            "max_duration_min": 12,
            "record": True,
            "analysis_schema": {"demo_booked": "boolean"},
            "dialing_strategy": {"type": "local", "pool_id": None},
        },
        request_data={"first_name": "Mike"},
        metadata_={"crm_id": "42"},
        variables={"first_name": "Mike", "now_utc": "2026-10-24T18:20:09Z"},
        answered_at=ANSWERED,
        started_at=ANSWERED - timedelta(seconds=5),
        ended_at=ENDED,
        connected_seconds=Decimal("141.400"),
        transfer_seconds=Decimal("30.000"),
        transferred_to="+14155550777",
        summary="Booked a demo.",
        disposition_tag="INTERESTED",
        price_micros=282_000,
        recording_s3_key="org/2026/10/call.wav",
    )
    await seed_leg(database, call, f"v3:{new_id()}")
    await seed_leg(
        database, call, f"v3:{new_id()}", role="warm_proxy", state="hangup", hangup_cause="normal_clearing"
    )
    earlier = datetime.now(UTC) - timedelta(minutes=5)
    async with database.tenant_session(org_id) as session:
        session.add_all(
            [
                _turn(call, 0, "system", "context update"),
                _turn(call, 0, "assistant", "Hi Mike.", at=1, node_id="greet", node_name="Greeting"),
                _turn(
                    call,
                    1,
                    "user",
                    "Hello.",
                    at=2,
                    latency={"e2e_ms": 700, "llm_ttft_ms": 300, "tts_ttfa_ms": 150},
                ),
                _turn(
                    call,
                    1,
                    "assistant",
                    "Want a demo?",
                    at=3,
                    node_id="pitch",
                    node_name="Pitch",
                    interrupted=True,
                ),
                _turn(call, 2, "user", "Sure.", at=4, latency={"e2e_ms": 900, "llm_ttft_ms": 301}),
                _turn(call, 2, "agent-action", "Booked.", at=5),
                *_log_events(call),
                GuardRailEvent(
                    org_id=org_id,
                    call_id=call.call_id,
                    guard_rail="tcpa:opt_out",
                    triggered=False,
                    turn_index=2,
                ),
                GuardRailEvent(
                    org_id=org_id,
                    call_id=call.call_id,
                    guard_rail="custom:pricing",
                    triggered=True,
                    action="warn",
                    turn_index=4,
                ),
                CallAnalysis(
                    org_id=org_id,
                    call_id=call.call_id,
                    kind="analysis_schema",
                    result={"old": 1},
                    model="m",
                    created_at=earlier,
                ),
                CallAnalysis(
                    org_id=org_id,
                    call_id=call.call_id,
                    kind="analysis_schema",
                    result={"demo_booked": True},
                    model="m",
                ),
                CallAnalysis(org_id=org_id, call_id=call.call_id, kind="summary", result={"s": 1}, model="m"),
            ]
        )
        schema = await _insert(session, CitationSchema, org_id, id=new_id())
        session.add(
            Citation(
                org_id=org_id,
                call_id=call.call_id,
                citation_schema_id=schema["id"],
                variable_name="budget",
                variable_type="number",
                value=5000,
                conditions_met=True,
                cited_utterances=[{"turn_index": 4, "text": "Sure."}],
            )
        )
        setup = await _insert(session, EvalSetupVersion, org_id, id=new_id())
        session.add_all(
            [
                EvalRun(
                    org_id=org_id,
                    call_id=call.call_id,
                    eval_setup_version_id=setup["id"],
                    status="completed",
                    overall_score=Decimal("50.00"),
                    passed=False,
                    created_at=earlier,
                ),
                EvalRun(
                    org_id=org_id,
                    call_id=call.call_id,
                    eval_setup_version_id=setup["id"],
                    status="completed",
                    overall_score=Decimal("87.50"),
                    passed=True,
                    results=[{"criterion_id": "c1", "score": 9}],
                ),
            ]
        )
    return call, runner


async def _objects(
    database: Database, org_id: uuid.UUID, *call_ids: uuid.UUID, **urls: Any
) -> list[CallObject]:
    async with database.tenant_session(org_id) as session:
        calls = await _calls(session, call_ids)
        return await load_call_objects(session, calls, recording_urls=urls.get("urls", {}))


async def _items(database: Database, org_id: uuid.UUID, *call_ids: uuid.UUID) -> list[CallListItem]:
    async with database.tenant_session(org_id) as session:
        calls = await _calls(session, call_ids)
        return await load_call_list_items(session, calls, recording_urls={})


async def _calls(session: AsyncSession, call_ids: tuple[uuid.UUID, ...]) -> list[Call]:
    rows = {call.id: call for call in await session.scalars(select(Call).where(Call.id.in_(call_ids)))}
    return [rows[call_id] for call_id in call_ids]


async def test_a_populated_call_fills_every_field(database: Database) -> None:
    call, runner = await _populated(database)
    (found,) = await _objects(database, call.org_id, call.call_id, urls={call.call_id: RECORDING})
    body = found.model_dump(mode="json", by_alias=True)
    created = body.pop("created_at")
    transcripts = body.pop("transcripts")
    logs = body.pop("pathway_logs")
    citations = body.pop("citations")
    evals = body.pop("evals")
    rails = body.pop("guard_rails")
    legs = body.pop("warm_transfer_calls")
    assert created.endswith("Z")
    assert body == {
        "call_id": str(call.call_id),
        "c_id": str(call.call_id),
        "org_id": str(call.org_id),
        "external_id": "crm-42",
        "batch_id": None,
        "to": "+14155550100",
        "from": "+14155550199",
        "inbound": False,
        "direction": "outbound",
        "test_call": True,
        "started_at": "2026-10-24T18:20:09Z",
        "end_at": "2026-10-24T18:22:30Z",
        "call_length": 2.356667,
        "corrected_duration": "141",
        "completed": True,
        "status": "completed",
        "queue_status": "complete",
        "answered_by": "human",
        "call_ended_by": "ASSISTANT",
        "end_reason": "agent_end_call",
        "max_duration": 12.0,
        "record": True,
        "recording_url": RECORDING.url,
        "recording_expiration": "2026-10-31T18:22:30Z",
        "request_data": {"first_name": "Mike"},
        "metadata": {"crm_id": "42"},
        "variables": {"first_name": "Mike", "now_utc": "2026-10-24T18:20:09Z"},
        "pathway_id": None,
        "pathway_version": None,
        "persona_id": None,
        "persona_version": "3",
        "agent_id": str(runner.agent_id),
        "agent_version": "1.2.0",
        "experiment_id": str(runner.experiment_id),
        "experiment_arm": "B",
        "concatenated_transcript": "assistant: Hi Mike.\n user: Hello.\n assistant: Want a demo?\n"
        " user: Sure.\n agent-action: Booked.\n",
        "summary": "Booked a demo.",
        "disposition_tag": "INTERESTED",
        "analysis_schema": {"demo_booked": "boolean"},
        "analysis": {"demo_booked": True},
        "price": 0.282,
        "transferred_to": "+14155550777",
        "transfer_duration": 0.5,
        "local_dialing": True,
        "error_message": None,
        "latency": {
            "p50_e2e_ms": 700,
            "p95_e2e_ms": 900,
            "avg_llm_ttft_ms": 300,
            "avg_tts_ttfa_ms": 150,
            "turns": 2,
        },
        "voicemail": None,
    }
    assert [(t["id"], t["user"], t["text"], t["start_ms"], t["interrupted"]) for t in transcripts] == [
        (1, "assistant", "Hi Mike.", 1000, False),
        (2, "user", "Hello.", 2000, False),
        (3, "assistant", "Want a demo?", 3000, True),
        (4, "user", "Sure.", 4000, False),
        (5, "agent-action", "Booked.", 5000, False),
    ]
    assert all(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", log["created_at"]) for log in logs)
    assert [{k: v for k, v in log.items() if k != "created_at"} for log in logs] == [
        {
            "tag": {"name": "Greeting", "color": None},
            "node_id": "greet",
            "pathway_id": None,
            "pathway_version": None,
            "role": "assistant",
            "text": "Hi Mike.",
            "user_text": None,
            "decision": {
                "route": None,
                "global": False,
                "loop_condition": None,
                "extracted": None,
                "tools": [],
            },
        },
        {
            "tag": {"name": "Pitch", "color": "#16A34A"},
            "node_id": "pitch",
            "pathway_id": None,
            "pathway_version": None,
            "role": "assistant",
            "text": "Want a demo?",
            "user_text": "Hello.",
            "decision": {
                "route": {
                    "from": "greet",
                    "to": "pitch",
                    "label": "Interested",
                    "alternatives": [{"label": "Busy", "score": 2}],
                },
                "global": False,
                "loop_condition": {"score": 7, "met": True},
                "extracted": {"first_name": "Mike"},
                "tools": [{"name": "CRM", "status": 200, "ms": 184}],
            },
        },
    ]
    assert [(c["variable_name"], c["value"], c["conditions_met"]) for c in citations] == [
        ("budget", 5000, True)
    ]
    assert evals is not None
    assert (evals["overall_score"], evals["passed"], evals["results"]) == (
        87.5,
        True,
        [{"criterion_id": "c1", "score": 9}],
    )
    assert rails == [
        {"guard_rail": "tcpa:opt_out", "triggered": False, "action": None, "turn_index": 2},
        {"guard_rail": "custom:pricing", "triggered": True, "action": "warn", "turn_index": 4},
    ]
    assert [(leg["role"], leg["state"], leg["hangup_cause"]) for leg in legs] == [
        ("warm_proxy", "hangup", "normal_clearing")
    ]


async def test_a_bare_queued_call_has_defaults_and_empties(database: Database) -> None:
    call = await seed_call(database, status="queued", to_number=None, from_number=None)
    (found,) = await _objects(database, call.org_id, call.call_id)
    body = found.model_dump(mode="json", by_alias=True)
    assert {key: body[key] for key in DEFAULTS} == DEFAULTS


DEFAULTS: Final[dict[str, Any]] = {
    "to": "",
    "from": "",
    "started_at": None,
    "end_at": None,
    "call_length": 0.0,
    "corrected_duration": "0",
    "completed": False,
    "status": "queued",
    "queue_status": "new",
    "answered_by": None,
    "call_ended_by": None,
    "end_reason": None,
    "max_duration": 30.0,
    "record": False,
    "recording_url": None,
    "recording_expiration": None,
    "agent_version": None,
    "experiment_arm": None,
    "analysis_schema": None,
    "analysis": None,
    "price": 0.0,
    "transfer_duration": 0.0,
    "local_dialing": False,
    "latency": {
        "p50_e2e_ms": None,
        "p95_e2e_ms": None,
        "avg_llm_ttft_ms": None,
        "avg_tts_ttfa_ms": None,
        "turns": 0,
    },
    "voicemail": None,
    "warm_transfer_calls": [],
    "guard_rails": [],
    "transcripts": [],
    "pathway_logs": [],
    "citations": [],
    "evals": None,
    "concatenated_transcript": "",
}


@pytest.mark.parametrize(
    ("values", "events", "expected"),
    [
        ({"answered_by": "voicemail"}, [], {"detected": True, "action_taken": None, "message_left": False}),
        (
            {"end_reason": "voicemail_hangup"},
            [],
            {"detected": True, "action_taken": "hangup", "message_left": False},
        ),
        (
            {"end_reason": "voicemail_left", "answered_by": "voicemail"},
            [{"action": "leave_message_and_sms"}],
            {"detected": True, "action_taken": "leave_message_and_sms", "message_left": True},
        ),
        (
            {},
            [{"action": "ignore", "trigger": "classifier"}],
            {"detected": True, "action_taken": "ignore", "message_left": False},
        ),
        ({}, [{"trigger": "carrier"}], {"detected": True, "action_taken": None, "message_left": False}),
        ({"answered_by": "no_answer", "end_reason": "no_answer"}, [], None),
    ],
)
async def test_voicemail_summary(
    database: Database, values: dict[str, Any], events: list[dict[str, Any]], expected: dict[str, Any] | None
) -> None:
    call = await seed_call(database, status="completed", **values)
    async with database.tenant_session(call.org_id) as session:
        session.add_all([_event(call, seq, "voicemail", data) for seq, data in enumerate(events, start=1)])
    (found,) = await _objects(database, call.org_id, call.call_id)
    assert found.model_dump(mode="json")["voicemail"] == expected
    (item,) = await _items(database, call.org_id, call.call_id)
    assert item.model_dump(mode="json")["voicemail"] == expected


async def test_list_items_omit_heavy_fields_but_keep_the_summary_fields(database: Database) -> None:
    call, _ = await _populated(database)
    (item,) = await _items(database, call.org_id, call.call_id)
    (full,) = await _objects(database, call.org_id, call.call_id)
    assert type(item) is CallListItem
    listed = item.model_dump(mode="json", by_alias=True)
    expected = full.model_dump(
        mode="json", by_alias=True, exclude=set(CallObject.model_fields) - set(CallListItem.model_fields)
    )
    assert listed == expected
    assert listed["latency"]["turns"] == 2
    assert listed["agent_version"] == "1.2.0"


async def test_the_inbound_flag_and_answered_by_mapping(database: Database) -> None:
    call = await seed_call(
        database, status="no_answer", direction="inbound", answered_by="no_answer", ended_by="system"
    )
    (item,) = await _items(database, call.org_id, call.call_id)
    assert (item.inbound, item.direction, item.status, item.answered_by, item.call_ended_by) == (
        True,
        "inbound",
        "no-answer",
        "no-answer",
        "SYSTEM",
    )


@contextmanager
def _counted(database: Database) -> Iterator[list[str]]:
    statements: list[str] = []

    def record(*args: Any) -> None:
        statements.append(args[2])

    engine = database.app_engine.sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def _seed_many(database: Database, count: int) -> tuple[uuid.UUID, list[uuid.UUID]]:
    org_id = await OrgFactory(database).create()
    runner = await _runner(database, org_id)
    ids: list[uuid.UUID] = []
    for index in range(count):
        call = await seed_call(
            database,
            org_id=org_id,
            status="completed",
            agent_id=runner.agent_id,
            agent_version_id=runner.agent_version_id,
            experiment_id=runner.experiment_id,
            experiment_arm_id=runner.experiment_arm_id,
        )
        async with database.tenant_session(org_id) as session:
            session.add(_turn(call, 1, "user", f"turn {index}", latency={"e2e_ms": 500 + index}))
        ids.append(call.call_id)
    return org_id, ids


@pytest.mark.parametrize("load", ["objects", "items"])
async def test_reads_are_batched_regardless_of_the_number_of_calls(database: Database, load: str) -> None:
    org_id, ids = await _seed_many(database, 20)
    counts: list[int] = []
    for selected in (ids[:1], ids):
        with _counted(database) as statements:
            found = (
                await _objects(database, org_id, *selected)
                if load == "objects"
                else await _items(database, org_id, *selected)
            )
        assert [item.call_id for item in found] == selected
        counts.append(len(statements))
    assert counts[0] == counts[1]


async def test_no_calls_read_nothing(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    async with database.tenant_session(org_id) as session:
        assert await load_call_objects(session, [], recording_urls={}) == []
        assert await load_call_list_items(session, [], recording_urls={}) == []


async def test_another_orgs_child_rows_never_leak(database: Database) -> None:
    call, _ = await _populated(database)
    other = await seed_call(database, status="completed")
    async with database.tenant_session(call.org_id) as session:
        mine = await session.get_one(Call, call.call_id)
    # Pretend the other org's call belongs to this page: RLS hides its children from this tenant session.
    async with database.tenant_session(other.org_id) as session:
        session.add(_turn(other, 1, "user", "secret", latency={"e2e_ms": 1}))
    async with database.tenant_session(call.org_id) as session:
        foreign = Call(**{**_columns(mine), "id": other.call_id, "org_id": other.org_id})
        (found,) = await load_call_objects(session, [foreign], recording_urls={})
    assert found.transcripts == []
    assert found.latency.turns == 0


def _columns(call: Call) -> dict[str, Any]:
    return {column.key: getattr(call, column.key) for column in Call.__mapper__.column_attrs}


async def test_pathway_turns_group_by_their_engine_turn(database: Database) -> None:
    """Pipeline turns 2 and 3 (two chained nodes of engine turn 1) make one log entry (P07-T12b)."""
    call = await seed_call(database, status="completed", runner_type="pathway")
    rows = [
        _turn(call, 0, "assistant", "Welcome to Bella.", node_id="start", node_name="Start", pathway_turn=0),
        _turn(call, 1, "user", "A table please.", node_id="start", node_name="Start", pathway_turn=1),
        _turn(call, 2, "assistant", "Sure.", node_id="book", node_name="Book", pathway_turn=1),
        _turn(call, 3, "assistant", "How many guests?", node_id="size", node_name="Size", pathway_turn=1),
    ]
    route = {"turn_index": 1, "from_node": "start", "to_node": "book", "label": "Book", "tag_color": None}
    async with database.tenant_session(call.org_id) as session:
        session.add_all(rows)
        session.add(_event(call, 1, "route", route, "call.route"))
    [body] = await _objects(database, call.org_id, call.call_id)
    assert [(log.node_id, log.text, log.user_text) for log in body.pathway_logs] == [
        ("start", "Welcome to Bella.", None),
        ("size", "Sure. How many guests?", "A table please."),
    ]
    assert body.pathway_logs[1].tag.name == "Size"
