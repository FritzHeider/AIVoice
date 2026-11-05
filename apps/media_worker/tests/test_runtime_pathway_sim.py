"""Pathway calls through the media runtime on the simulators (wall clock): FakeTelnyx streams a scripted
caller to a served media worker whose :class:`CallRuntime` runs a :class:`PathwayBrain` on a stored pathway
version over FakeElevenLabs and FakeLLM, with a real webhook receiver, the media database and the end-of-call
routine (specs/11 §9, §10, §12; specs/19 §2; specs/11 §10 Decision P07-T12b). A reply node's stored tool
runs through the call's :class:`~va_media.call_tools.PathwayCallTools` (specs/13 §4 Decision P08-T02c2)."""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

import httpx
import pytest
from call_rows import SeededCall, load_call, seed_call
from graph_helpers import edge, graph, node, start
from llm_client_helpers import served_requests
from llm_helpers import add_rules
from media_helpers import FakeContextLoader, FakeMediaContainer, RecordingSignaller, call_context
from pathways_helpers import NO_SPECULATION, store_pathway
from runtime_rig import (
    MODELS_DIR,
    WAIT_S,
    RecordingRuntime,
    StaticOrgSettings,
    local_tool_source,
    sim_backends,
    sim_runtime,
)
from service_helpers import json_log_stream, log_events
from sim_media import EdgeRelay, realtime_telnyx, streamed_call
from sqlalchemy import select
from telnyx_helpers import CALLEE, register
from tenant_rows import OrgFactory
from tts_helpers import VOICE_ID
from webhook_receiver import Receiver, Reply

from va_common.crypto import Envelope
from va_common.time import SystemClock, utcnow
from va_db.call_object import load_call_objects
from va_db.models import Call, CallEvent
from va_db.provisioning import OrgProvisioner
from va_db.repositories import CallTurnRepo, ToolRepo
from va_db.repositories.identity import NewUser
from va_db.session import Database
from va_domain.org import OrgSettings
from va_elevenlabs import ElevenLabsRest
from va_elevenlabs.tts_models import DEFAULT_TTS_MODEL
from va_engine.models import PathwayGraph, load_engine_state
from va_engine.pathway_call import engine_state_key
from va_events.webhook_signing import signature_headers
from va_media.pathway_runtime import PathwayTools
from va_media.runtime_config import ERROR_END
from va_media.session import STREAM_CLOSED, SessionEnd
from va_pathways.call_graphs import CallGraphCache
from va_sim.state import SimState
from va_tools.secrets import encrypt_secret_headers
from va_voice.phrase_cache import PhraseCache
from va_workflows.activities.prerender import OpenerPrerender

pytestmark = pytest.mark.integration

WELCOME: Final = "Welcome to Bella Pasta."
CHECKING: Final = "Let me check that for you."
GOODBYE: Final = "Goodbye!"
TOOL_SPEECH: Final = "Checking your order."
TOOL_REPLY: Final = "Your order has shipped."

RULES: Final = """rules:
  - id: route_greeting
    match: {tool_name: select_path, system_contains: ['Current step: "Greeting"']}
    respond:
      tool_calls:
        - name: select_path
          arguments: {path_id: e_hook, confidence: 9, alternatives: [], reason: s}
  - id: route_status
    match:
      tool_name: select_path
      system_contains: ['Current step: "Delivery Status"']
      last_user_regex: 'bye'
    respond:
      tool_calls:
        - name: select_path
          arguments: {path_id: e_bye, confidence: 9, alternatives: [], reason: s}
  - id: size
    match: {tool_name: extract_variables}
    respond:
      tool_calls: [{name: extract_variables, arguments: {pizza_size: large}}]
"""


TOOL_RULES: Final = """rules:
  - id: route_greeting
    match: {tool_name: select_path, system_contains: ['Current step: "Greeting"']}
    respond:
      tool_calls:
        - name: select_path
          arguments: {path_id: e_lookup, confidence: 9, alternatives: [], reason: s}
  - id: route_lookup
    match:
      tool_name: select_path
      system_contains: ['Current step: "Order Lookup"']
      last_user_regex: 'bye'
    respond:
      tool_calls:
        - name: select_path
          arguments: {path_id: e_bye, confidence: 9, alternatives: [], reason: s}
  - id: ask_tool
    match: {tool_name: lookup_order, system_contains: ['## Current step: Order Lookup'], call_index: 0}
    respond:
      tool_calls: [{name: lookup_order, arguments: {order: A7}}]
  - id: use_answer
    match: {tool_name: lookup_order, system_contains: ['## Current step: Order Lookup'], call_index: 1}
    respond: {content: 'Your order has shipped.'}
"""


def lookup_pathway(tool_id: uuid.UUID) -> PathwayGraph:
    """Greeting → a reply node holding the stored tool ``tool_id`` → goodbye."""
    nodes = [
        start("greet", name="Greeting", text=WELCOME),
        node(
            "lookup",
            name="Order Lookup",
            prompt="Tell the caller where the order is.",
            tools=[f"TL-{tool_id}"],
        ),
        node("bye", name="Goodbye", node_type="End Call", endCall={"speech": GOODBYE}),
    ]
    edges = [
        edge("e_lookup", "greet", "lookup", "Caller asks about an order"),
        edge("e_bye", "lookup", "bye", "Caller says goodbye"),
    ]
    return graph(nodes, edges)


def delivery_pathway(hook_port: int) -> PathwayGraph:
    """Greeting (extracts ``pizza_size``) → the delivery-status webhook (``eta``) → goodbye."""
    hook = {
        "url": f"http://127.0.0.1:{hook_port}/status",
        "method": "POST",
        "body": {"caller": "{{from}}", "size": "{{pizza_size}}"},
        "speech": CHECKING,
        "responseData": [{"name": "eta", "data": "$.eta"}],
    }
    nodes = [
        start(
            "greet",
            name="Greeting",
            text=WELCOME,
            extractVars=[{"name": "pizza_size", "type": "string", "description": "Pizza size"}],
        ),
        node(
            "hook",
            name="Delivery Status",
            node_type="Webhook",
            webhook=hook,
            text="It arrives in {{eta}} minutes.",
        ),
        node("bye", name="Goodbye", node_type="End Call", endCall={"speech": GOODBYE}),
    ]
    edges = [
        edge("e_hook", "greet", "hook", "Caller asks about delivery status"),
        edge("e_bye", "hook", "bye", "Caller says goodbye"),
    ]
    return graph(nodes, edges)


@dataclass
class PathwayRig:
    state: SimState
    llm: httpx.AsyncClient
    body: RecordingRuntime
    container: FakeMediaContainer
    signaller: RecordingSignaller
    relay: EdgeRelay
    database: Database
    receiver: Receiver
    hook_port: int
    elevenlabs_url: str
    envelope: Envelope

    def scenario(self, *steps: str) -> None:
        text = f'id: pw-{uuid.uuid4().hex[:8]}\nmatch: {{to: "{CALLEE}"}}\nanswer: {{after_ms: 200}}\n'
        text += "steps:\n" + "".join(f"  - {step}\n" for step in steps)
        register(self.state, text)

    @asynccontextmanager
    async def call(self, seeded: SeededCall, **config: Any) -> AsyncIterator[None]:
        values: dict[str, Any] = {"provider_voice_id": VOICE_ID, "wait_for_greeting": False, **config}
        loader = FakeContextLoader()
        loader.contexts[seeded.call_id] = call_context(seeded.call_id, org_id=seeded.org_id, **values)
        async with streamed_call(
            MODELS_DIR,
            self.body,
            state=self.state,
            loader=loader,
            answered=True,
            call_id=seeded.call_id,
            container=self.container,
            carrier={"answered_by": "human"},
            relay=self.relay,
        ):
            yield

    async def signalled(self) -> list[tuple[str, str]]:
        async with asyncio.timeout(WAIT_S):
            await self.signaller.signalled.wait()
        return [(f.end_reason, f.ended_by) for _, f in self.signaller.signals]


@asynccontextmanager
async def pathway_rig(
    media_db_url: str, *, rules: str = RULES, reply: bytes = b'{"eta": 25}'
) -> AsyncIterator[PathwayRig]:
    """The runtime with pathway calls and their tools on the media database: one Redis (the container's)
    for the engine snapshot, the end routine and the events; the receiver answers ``reply``."""
    relay = EdgeRelay()
    state = realtime_telnyx(relay)
    container = FakeMediaContainer()
    receiver = Receiver(Reply(200, {"content-type": "application/json"}, body=reply))
    async with sim_backends(media_db_url, state, receiver, container.redis) as backends:
        database, tools_http = backends.database, backends.tools_http
        source = local_tool_source(database, tools_http)
        async with sim_runtime(
            state,
            backends.urls,
            org_settings=StaticOrgSettings(NO_SPECULATION),
            phrase_redis=container.redis,
            persistence=backends.persistence,
            database=database,
            end_redis=container.redis,
            pathways=PathwayTools(graphs=CallGraphCache(database), tools_http=tools_http, allow_http=True),
            call_tools=source,
        ) as parts:
            await add_rules(backends.llm, rules)
            yield PathwayRig(
                state,
                backends.llm,
                parts.body,
                container,
                parts.signaller,
                relay,
                database,
                receiver,
                backends.receiver_port,
                backends.urls["elevenlabs"],
                source.envelope,
            )


async def _stored(r: PathwayRig, version: int = 1) -> SeededCall:
    """An org with the delivery pathway (version 1) and an answered call on ``version`` of it."""
    org_id = await OrgFactory(r.database).create()
    pathway_id = await store_pathway(r.database, org_id, delivery_pathway(r.hook_port), name="Delivery")
    return await seed_call(
        r.database,
        org_id=org_id,
        status="in_progress",
        answered_at=utcnow(),
        runner_type="pathway",
        pathway_id=pathway_id,
        pathway_version=version,
        variables={"source": "api"},
    )


async def _pathway_of(r: PathwayRig, call: SeededCall) -> dict[str, Any]:
    row = await load_call(r.database, call)
    return {
        "runner_type": "pathway",
        "pathway_id": str(row.pathway_id),
        "pathway_version": row.pathway_version,
    }


def spoken(state: SimState, *, socket_only: bool = False) -> list[str]:
    """Text FakeElevenLabs generated, in order (``socket_only``: REST renders excluded)."""
    requests = state.elevenlabs.tts_requests
    texts = [r.generated_text.strip() for r in requests if not socket_only or r.transport != "rest"]
    return [text for text in texts if text]


async def _events(database: Database, call: SeededCall) -> list[CallEvent]:
    async with database.tenant_session(call.org_id) as session:
        statement = select(CallEvent).where(CallEvent.call_id == call.call_id).order_by(CallEvent.seq)
        return list(await session.scalars(statement))


async def _ask_then_say_bye(r: PathwayRig, call: SeededCall, question: str, *, silence_ms: int) -> None:
    """Run ``call``: the caller asks ``question`` after the opener, waits for the answer (``silence_ms`` of
    agent silence), says goodbye, and the agent's End Call node ends the call."""
    r.scenario(
        "wait_agent_speech: {}",
        f"say: {{text: {question}}}",
        f"wait_agent_speech: {{then_silence_ms: {silence_ms}}}",
        "say: {text: thanks bye}",
        "expect_hangup: {}",
    )
    async with r.call(call, **await _pathway_of(r, call)):
        end = await r.body.end()
        signals = await r.signalled()
    assert end == SessionEnd("ended", "agent_end_call", "agent")
    assert signals == [("agent_end_call", "agent")]


async def test_a_pathway_call_routes_calls_the_webhook_and_ends(media_db_url: str) -> None:
    async with pathway_rig(media_db_url) as r:
        call = await _stored(r)
        await _ask_then_say_bye(r, call, "where is my delivery", silence_ms=1500)
        assert spoken(r.state) == [WELCOME, CHECKING, "It arrives in 25 minutes.", GOODBYE]
        [hook] = r.receiver.requests
        assert hook.path == "/status"
        assert json.loads(hook.body) == {"caller": "+14155550199", "size": "large"}

        stored = await r.container.redis.get(engine_state_key(call.call_id))
        assert stored is not None
        snapshot = load_engine_state(stored)
        assert (snapshot.ended, snapshot.current_node_id) == (True, "bye")
        row = await load_call(r.database, call)
        assert (row.status, row.end_reason) == ("completed", "agent_end_call")
        assert row.variables == {"source": "api", "pizza_size": "large", "eta": 25}

        events = await _events(r.database, call)
        routes = [
            (e.data["from_node"], e.data["to_node"], e.data["turn_index"])
            for e in events
            if e.event_type == "call.route"
        ]
        assert routes == [("greet", "hook", 1), ("hook", "bye", 2)]
        [webhook] = [e for e in events if e.event_type == "call.webhook_node"]
        assert (webhook.data["node_id"], webhook.data["status_code"], webhook.data["turn_index"]) == (
            "hook",
            200,
            1,
        )
        assert webhook.message == "Webhook node hook: status 200"
        assert any(
            e.event_type == "call.extraction" and e.data["variables"] == {"pizza_size": "large"}
            for e in events
        )

        async with r.database.tenant_session(call.org_id) as session:
            turns = await CallTurnRepo(session, call.org_id).list_for_call(call.call_id)
            [stored_call] = (await session.scalars(select(Call).where(Call.id == call.call_id))).all()
            [body] = await load_call_objects(session, [stored_call], recording_urls={})
        assert [(t.role, t.pathway_turn, t.node_id) for t in turns] == [
            ("assistant", 0, "greet"),
            ("user", 1, "greet"),
            ("assistant", 1, "hook"),
            ("assistant", 1, "hook"),
            ("user", 2, "hook"),
            ("assistant", 2, "bye"),
        ]
        logs = [(log.node_id, log.text, log.user_text, log.tag.name) for log in body.pathway_logs]
        assert logs == [
            ("greet", WELCOME, None, "Greeting"),
            ("hook", f"{CHECKING} It arrives in 25 minutes.", "where is my delivery", "Delivery Status"),
            ("bye", GOODBYE, "thanks bye", "Goodbye"),
        ]
        routes_of = [(log.decision or {}).get("route") for log in body.pathway_logs]
        assert [None if route is None else route["to"] for route in routes_of] == [None, "hook", "bye"]
        tools = (body.pathway_logs[1].decision or {})["tools"]
        assert [(tool["name"], tool["status"]) for tool in tools] == [("Delivery Status", 200)]
        assert (body.pathway_logs[1].decision or {})["extracted"] == {"pizza_size": "large"}


async def test_a_pathway_version_that_cannot_load_ends_the_call_with_error(media_db_url: str) -> None:
    stream = json_log_stream()
    async with pathway_rig(media_db_url) as r:
        call = await _stored(r, version=7)
        r.scenario("silence: {ms: 60000}")
        async with r.call(call, **await _pathway_of(r, call)):
            end = await r.body.end()
        assert end == ERROR_END
        assert spoken(r.state) == []
        assert not r.state.elevenlabs.sockets
    reasons = [e["reason"] for e in log_events(stream) if e["event"] == "media.pathway_unavailable"]
    assert reasons == ["version_not_found"]


async def test_a_start_line_prerendered_at_prefetch_plays_from_the_phrase_cache(media_db_url: str) -> None:
    async with pathway_rig(media_db_url) as r:
        call = await _stored(r)
        pathway = await _pathway_of(r, call)
        context = call_context(
            call.call_id, org_id=call.org_id, provider_voice_id=VOICE_ID, wait_for_greeting=False, **pathway
        )
        async with httpx.AsyncClient() as http:
            cache = PhraseCache(
                ElevenLabsRest("sim-eleven-key", r.elevenlabs_url, http, clock=SystemClock()),
                r.container.redis,
            )
            job = OpenerPrerender(
                cache=cache,
                org_settings=StaticOrgSettings(OrgSettings()),
                model_id=DEFAULT_TTS_MODEL,
                graphs=CallGraphCache(r.database),
            )
            opener = await job.opener_for_call(context, utcnow())
            rendered = await job.render(context.model_copy(update={"opener_text": opener}))
            await cache.aclose()
        assert (opener, rendered.opener) == (WELCOME, True)
        r.scenario("wait_agent_speech: {min_ms: 500, then_silence_ms: 700, timeout_ms: 20000}", "hangup: {}")
        async with r.call(call, **pathway):
            end = await r.body.end()
        assert end == STREAM_CLOSED
        assert spoken(r.state, socket_only=True) == []
        assert WELCOME in spoken(r.state)


async def _stored_lookup(r: PathwayRig) -> tuple[SeededCall, uuid.UUID, str]:
    """A provisioned org with the stored tool ``lookup_order`` (a secret header, the receiver's ``/order``),
    the lookup pathway holding it and an answered call on it; also the tool id and the org's signing
    secret."""
    provisioned = await OrgProvisioner(r.database, r.envelope).provision(
        name="Orders Inc", owner=NewUser(email=f"owner-{secrets.token_hex(4)}@orders.test")
    )
    org_id = provisioned.org.id
    tool_id = uuid.uuid4()
    blob = await encrypt_secret_headers(r.envelope, org_id, tool_id, {"X-Key": "order-key"})
    async with r.database.tenant_session(org_id) as session:
        await ToolRepo(session, org_id).create(
            {
                "name": "lookup_order",
                "description": "Looks up an order.",
                "speech": TOOL_SPEECH,
                "url": f"http://127.0.0.1:{r.hook_port}/order",
                "body": {"order": "{{input.order}}"},
                "input_schema": {"type": "object", "properties": {"order": {"type": "string"}}},
                "response_map": {"order_status": "$.status"},
                "secret_headers_enc": blob,
            },
            tool_id=tool_id,
        )
    pathway_id = await store_pathway(r.database, org_id, lookup_pathway(tool_id), name="Orders")
    call = await seed_call(
        r.database,
        org_id=org_id,
        status="in_progress",
        answered_at=utcnow(),
        runner_type="pathway",
        pathway_id=pathway_id,
        pathway_version=1,
        variables={"source": "api"},
    )
    return call, tool_id, provisioned.webhook_signing_secret


async def test_a_node_tool_runs_and_its_answer_is_used_in_the_reply(media_db_url: str) -> None:
    async with pathway_rig(media_db_url, rules=TOOL_RULES, reply=b'{"status": "shipped"}') as r:
        call, tool_id, signing_secret = await _stored_lookup(r)
        await _ask_then_say_bye(r, call, "where is my order", silence_ms=2000)
        assert spoken(r.state) == [WELCOME, TOOL_SPEECH, TOOL_REPLY, GOODBYE]

        [sent] = r.receiver.requests
        assert (sent.path, json.loads(sent.body)) == ("/order", {"order": "A7"})
        assert sent.headers["x-key"] == "order-key"
        assert sent.headers["x-va-call-id"] == str(call.call_id)
        signature = sent.headers["x-va-signature"]
        timestamp = int(signature.split(",", 1)[0].removeprefix("t="))
        assert signature == signature_headers([signing_secret], sent.body, timestamp)["X-VA-Signature"]

        node_requests = [
            request
            for request in await served_requests(r.llm)
            if any(tool["function"]["name"] == "lookup_order" for tool in request.get("tools") or [])
        ]
        offers = [[tool["function"]["name"] for tool in request["tools"]] for request in node_requests]
        assert offers == [["end_call", "press_button", "lookup_order"]] * 2
        followup = node_requests[1]
        answers = [
            message["content"]
            for message in followup["messages"]
            if "Tool lookup_order returned:" in (message.get("content") or "")
        ]
        assert len(answers) == 1
        assert '"order_status":"shipped"' in answers[0]

        row = await load_call(r.database, call)
        assert (row.status, row.end_reason) == ("completed", "agent_end_call")
        assert row.variables == {"source": "api", "order_status": "shipped"}

        events = await _events(r.database, call)
        [tool] = [e for e in events if e.event_type == "call.tool"]
        assert (tool.message, tool.data["tool_id"], tool.data["status_code"]) == (
            "Tool lookup_order: ok",
            f"TL-{tool_id}",
            200,
        )
        assert (tool.data["turn_index"], tool.data["node_id"]) == (1, "lookup")
        assert "order-key" not in json.dumps(tool.data)
        assert not [e for e in events if e.event_type == "call.error"]

        async with r.database.tenant_session(call.org_id) as session:
            [stored_call] = (await session.scalars(select(Call).where(Call.id == call.call_id))).all()
            [body] = await load_call_objects(session, [stored_call], recording_urls={})
        logs = [(log.node_id, log.text, log.user_text) for log in body.pathway_logs]
        assert logs == [
            ("greet", WELCOME, None),
            ("lookup", f"{TOOL_SPEECH} {TOOL_REPLY}", "where is my order"),
            ("bye", GOODBYE, "thanks bye"),
        ]
        tools = (body.pathway_logs[1].decision or {})["tools"]
        assert [(entry["name"], entry["status"]) for entry in tools] == [("lookup_order", 200)]
