"""Pathway call pieces of the media runtime: the engine snapshot saver, engine events through the recorder,
the SMS node port, and the brain builder's refusals (specs/11 §9, §10 Decision P07-T12b)."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from clock_helpers import ManualClock
from dialogue_helpers import FakeSource
from fakeredis import FakeAsyncRedis
from media_helpers import call_context
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from service_helpers import json_log_stream, log_events
from sms_fakes import FakeTelnyx
from sms_rows import add_sms_number, random_e164
from sqlalchemy import select
from tenant_rows import OrgFactory, open_database
from tools_helpers import GatedSystem
from turn_data_fakes import FakeTurnData

from va_common.ids import new_id
from va_db.models import Message
from va_db.session import Database
from va_domain.call_context import CallContext
from va_domain.org import OrgSettings
from va_engine.engine import new_pathway_state
from va_engine.models import EngineState, load_engine_state, load_graph
from va_engine.navigation import ActiveGraph, PathwayUnavailableError
from va_engine.pathway_brain import PathwayBrain
from va_engine.pathway_call import ENGINE_STATE_TTL_S, engine_state_key
from va_engine.ports import SmsOutcome, SmsRequest
from va_engine.system_tools import SystemToolFacts, facts_for_call
from va_engine.turn_data import TurnData
from va_llm.tokens import default_counter
from va_media.call_sms import CallSmsPort, OutboundSms, agent_number, counterpart_number
from va_media.call_tools import CallToolSource, PathwayCallTools
from va_media.pathway_runtime import (
    EngineStateSaver,
    PathwayToolEvents,
    PathwayTools,
    RecorderEvents,
    build_pathway_brain,
    load_call_graph,
)
from va_pathways.call_graphs import CallGraphCache
from va_tools.executor import SystemOutcome
from va_voice.pipeline.models import BrainOutput, SpeakText, UserTurn

EDGE = "edge.test:8100"


def _state(**values: Any) -> EngineState:
    nodes = [{"id": "start", "type": "Default", "data": {"name": "Start", "isStart": True, "text": "Hi."}}]
    state = new_pathway_state(ActiveGraph(None, None, load_graph(nodes, [], None)))
    return state.model_copy(update=values)


async def test_the_saver_writes_the_latest_state_with_its_ttl() -> None:
    redis = FakeAsyncRedis()
    call_id = new_id()
    saver = EngineStateSaver(cast("Redis", redis), call_id)
    saver.on_state(_state(variables={"party": 2}))
    saver.on_state(_state(variables={"party": 4}, ended=True))
    await saver.aclose()
    stored = await redis.get(engine_state_key(call_id))
    assert stored is not None
    saved = load_engine_state(stored)
    assert (saved.variables, saved.ended) == ({"party": 4}, True)
    assert 0 < await redis.ttl(engine_state_key(call_id)) <= ENGINE_STATE_TTL_S
    saver.on_state(_state(variables={"party": 5}))
    await saver.aclose()
    assert load_engine_state(await redis.get(engine_state_key(call_id)) or "").variables == {"party": 5}
    await redis.aclose()


async def test_a_redis_outage_is_logged_and_never_raised() -> None:
    stream = json_log_stream()
    saver = EngineStateSaver(cast("Redis", _Broken()), new_id())
    saver.on_state(_state())
    await saver.aclose()
    await EngineStateSaver(cast("Redis", _Broken()), new_id()).aclose()
    failures = [e["error_type"] for e in log_events(stream) if e["event"] == "media.engine_state_save_failed"]
    assert failures == ["ConnectionError"]


class _Broken:
    async def set(self, key: str, value: str, *, ex: int) -> None:
        del key, value, ex
        raise RedisConnectionError


class _Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def engine_event(self, event_type: str, data: dict[str, Any]) -> None:
        self.events.append((event_type, data))


def test_recorder_events_forward_to_the_recorder_or_drop() -> None:
    recorder = _Recorder()
    RecorderEvents(cast("Any", recorder)).emit("call.route", {"turn_index": 1})
    RecorderEvents(None).emit("call.route", {"turn_index": 2})
    assert recorder.events == [("call.route", {"turn_index": 1})]


def test_agent_and_counterpart_numbers_follow_the_direction() -> None:
    outbound = call_context(new_id())
    inbound = call_context(new_id(), direction="inbound")
    assert (agent_number(outbound), counterpart_number(outbound)) == (
        outbound.from_number,
        outbound.to_number,
    )
    assert (agent_number(inbound), counterpart_number(inbound)) == (inbound.to_number, inbound.from_number)


@pytest.fixture
async def database(media_db_url: str) -> AsyncIterator[Database]:
    async with open_database(media_db_url) as db:
        yield db


async def _sms_call(database: Database, **numbers: str) -> CallContext:
    org_id = await OrgFactory(database).create()
    agent = await add_sms_number(database, org_id)
    context = call_context(new_id(), org_id=org_id)
    return context.model_copy(update={"from_number": agent, "to_number": random_e164(), **numbers})


def _port(database: Database, telnyx: FakeTelnyx | None, context: CallContext) -> CallSmsPort:
    sender = OutboundSms(
        database=database,
        telnyx=telnyx,
        platform_profile_id="mp-platform",
        edge_public_host=EDGE,
        clock=ManualClock(),
        log_prefix="media.pathway_sms",
    )
    return CallSmsPort(sender, context)


@pytest.mark.integration
async def test_the_sms_port_sends_from_the_agent_number(database: Database) -> None:
    context = await _sms_call(database)
    telnyx = FakeTelnyx()
    to = random_e164()
    outcome = await _port(database, telnyx, context).send(
        SmsRequest(to=to, from_=None, body="Your table is booked.", node_id="sms")
    )
    assert outcome.sent
    assert outcome.message_id is not None
    [sent] = telnyx.sent
    assert (sent.from_, sent.to, sent.text, sent.messaging_profile_id) == (
        context.from_number,
        to,
        "Your table is booked.",
        "mp-platform",
    )
    async with database.tenant_session(context.org_id) as session:
        [stored] = (await session.scalars(select(Message).where(Message.org_id == context.org_id))).all()
    assert (str(stored.id), stored.status) == (outcome.message_id, "queued")


@pytest.mark.integration
async def test_sms_port_failures_are_outcomes(database: Database) -> None:
    stream = json_log_stream()
    context = await _sms_call(database)
    request = SmsRequest(to=random_e164(), from_=random_e164(), body="Hi.", node_id="sms")
    not_enabled = await _port(database, FakeTelnyx(), context).send(request)
    no_client = await _port(database, None, context).send(
        SmsRequest(to=random_e164(), from_=None, body="Hi.", node_id="sms")
    )
    assert (not_enabled, no_client) == (
        SmsOutcome(sent=False, error="sms_not_enabled"),
        SmsOutcome(sent=False, error="telnyx_unavailable"),
    )
    reasons = [e["reason"] for e in log_events(stream) if e["event"] == "media.pathway_sms_failed"]
    assert reasons == ["sms_not_enabled", "telnyx_unavailable"]


def _tools(database: Database) -> PathwayTools:
    return PathwayTools(
        graphs=CallGraphCache(database), tools_http=cast("httpx.AsyncClient", None), allow_http=True
    )


@pytest.mark.integration
async def test_a_pathway_call_without_a_loadable_version_is_refused(database: Database) -> None:
    org_id = await OrgFactory(database).create()
    contexts = [
        call_context(new_id(), org_id=org_id, runner_type="pathway"),
        call_context(
            new_id(), org_id=org_id, runner_type="pathway", pathway_id=str(uuid.uuid4()), pathway_version=1
        ),
    ]
    reasons = []
    for context in contexts:
        with pytest.raises(PathwayUnavailableError) as refused:
            await load_call_graph(context, _tools(database))
        reasons.append(refused.value.reason)
    assert reasons == ["not_found", "not_found"]


class _ToolRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def tool(self, data: dict[str, Any]) -> None:
        self.calls.append(("tool", data))

    def tool_failed(self, detail: str) -> None:
        self.calls.append(("tool_failed", detail))

    def sms_sent(self, data: dict[str, Any]) -> None:
        self.calls.append(("sms_sent", data))


def test_tool_events_carry_the_engine_turn_and_node_of_the_report() -> None:
    recorder = _ToolRecorder()
    state = _state()
    events = PathwayToolEvents(cast("Any", recorder), state)
    run = {"tool_name": "lookup", "outcome": "ok"}
    events.tool(dict(run))
    state.turn_index, state.current_node_id = 3, "orders"
    events.tool(dict(run))
    events.tool_failed("lookup: http_error (500)")
    events.sms_sent({"to": "+14155550100"})
    assert recorder.calls == [
        ("tool", {**run, "turn_index": 0, "node_id": "start"}),
        ("tool", {**run, "turn_index": 3, "node_id": "orders"}),
        ("tool_failed", "lookup: http_error (500)"),
        ("sms_sent", {"to": "+14155550100"}),
    ]


def _reply_graph() -> ActiveGraph:
    """One reply node whose line streams from the dialogue model."""
    nodes = [
        {"id": "start", "type": "Default", "data": {"name": "Start", "isStart": True, "prompt": "Greet."}}
    ]
    return ActiveGraph(None, None, load_graph(nodes, [], None))


def _brain(  # noqa: PLR0913 - what one built brain varies in
    context: CallContext,
    org: OrgSettings,
    active: ActiveGraph,
    state: EngineState,
    *,
    gateway: FakeSource | None = None,
    call_tools: PathwayCallTools | None = None,
    system_tools: SystemToolFacts | None = None,
    turn_data: TurnData | None = None,
) -> PathwayBrain:
    return build_pathway_brain(
        context,
        org,
        active=active,
        state=state,
        tools=_tools(cast("Database", None)),
        gateway=cast("Any", gateway),
        counter=default_counter(),
        clock=ManualClock(),
        sms=None,
        recorder=None,
        saver=None,
        call_tools=call_tools,
        system_tools=system_tools,
        turn_data=turn_data,
    )


async def test_the_brain_refreshes_the_call_s_per_turn_data_at_a_caller_turn() -> None:
    """specs/13 §7 Decision P08-T03b: the engine refreshes the port, the reply prompt reads its lines."""
    active = _reply_graph()
    state = new_pathway_state(active)
    context = call_context(new_id(), runner_type="pathway").model_copy(
        update={"context_sections": ["The customer is Ana."], "turn_sections": {0: ["stale"]}}
    )
    org = OrgSettings.model_validate({"engine": {"speculative": False}})
    source = FakeSource().reply("Hello.").reply("You are on platinum.")
    port = FakeTurnData(lines=("Their plan is gold.",)).then({"plan": "platinum"}, "Their plan is platinum.")
    brain = _brain(context, org, active, state, gateway=source, turn_data=port)

    await _drained(brain.start())
    opener = source.requests[0].messages[0].content or ""
    turn = UserTurn(index=1, text="which plan am I on", start_ms=1000.0, end_ms=1600.0)
    await _drained(brain.on_user_turn(turn))
    await brain.aclose()

    assert opener.endswith("\n\n## Context\nThe customer is Ana.\nTheir plan is gold.")
    assert port.scopes == [{}]
    assert state.variables == {"plan": "platinum"}
    system = source.requests[-1].messages[0].content or ""
    assert "- plan: platinum" in system
    assert system.endswith("\n\n## Context\nThe customer is Ana.\nTheir plan is platinum.")


async def _drained(outputs: AsyncIterator[BrainOutput]) -> list[str]:
    """Consume a brain's outputs like the pipeline (streams read before resuming); the streamed texts."""
    spoken: list[str] = []
    async for output in outputs:
        if isinstance(output, SpeakText):
            spoken.append("".join([token async for token in output.tokens]))
    return spoken


def test_the_brain_runs_on_the_given_graph_and_state() -> None:
    active = _reply_graph()
    state = new_pathway_state(active)
    context = call_context(new_id(), runner_type="pathway")
    org = OrgSettings.model_validate({"engine": {"tool_fillers": False}})
    brain = _brain(context, org, active, state, system_tools=SystemToolFacts(runner="pathway"))
    assert brain.state is state
    assert brain.engine.navigator.graph is active.graph


async def _offered(context: CallContext, org: OrgSettings, call_tools: PathwayCallTools | None) -> list[str]:
    """The tools the start node's reply offers the model on a call every system tool is allowed on (the
    agent number can send SMS)."""
    active = _reply_graph()
    source = FakeSource().reply("Hello.")
    brain = _brain(
        context,
        org,
        active,
        new_pathway_state(active),
        gateway=source,
        call_tools=call_tools,
        system_tools=facts_for_call(context, org, sms_capable=True),
    )
    async with contextlib.aclosing(brain.start()) as outputs:
        spoken = ["".join([token async for token in output.tokens]) async for output in outputs]  # type: ignore[union-attr]
    await brain.aclose()
    assert spoken == ["Hello."]
    [request] = source.requests
    return [tool.name for tool in request.tools]


async def test_reply_nodes_offer_only_the_system_tools_the_call_can_run_and_never_a_transfer() -> None:
    """specs/13 §4 Decision P08-T02c2 item 3: a call that allows every system tool (and has a transfer
    number) offers its reply nodes the task-mode set without ``transfer_call``; a media worker without a
    call tool source runs no executor-backed system tool, so it offers ``end_call`` and ``press_button``."""
    context = call_context(new_id(), runner_type="pathway", transfer_phone_number="+14155550123")
    org = OrgSettings.model_validate({"tools": {"allow_schedule_callback": True}})
    facts = facts_for_call(context, org, sms_capable=True)
    assert (facts.runner, facts.transfer_phone_number) == ("pathway", "+14155550123")
    assert (facts.sms_capable, facts.allow_schedule_callback) == (True, True)

    assert await _offered(context, org, None) == ["end_call", "press_button"]

    source = CallToolSource(
        database=cast("Database", None),
        envelope=cast("Any", None),
        tools_http=cast("httpx.AsyncClient", None),
        allow_http=True,
        clock=ManualClock(),
    )
    answer = SystemOutcome('{"status":"ok"}')
    sms_only = await source.pathway_tools(
        context, _reply_graph().graph, None, system=[GatedSystem("send_sms", answer)]
    )
    assert await _offered(context, org, sms_only) == ["end_call", "press_button", "send_sms"]
    both = await source.pathway_tools(
        context,
        _reply_graph().graph,
        None,
        system=[GatedSystem("send_sms", answer), GatedSystem("schedule_callback", answer)],
    )
    assert await _offered(context, org, both) == ["end_call", "press_button", "send_sms", "schedule_callback"]
