"""Pathway calls through the media runtime on the simulators (wall clock): FakeTelnyx streams a scripted
caller to a served media worker whose :class:`CallRuntime` runs a :class:`PathwayBrain` on a stored pathway
version over FakeElevenLabs and FakeLLM, with a real webhook receiver, the media database and the end-of-call
routine (specs/11 §9, §10, §12; specs/19 §2; specs/11 §10 Decision P07-T12b)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

import httpx
import pytest
from call_rows import SeededCall, load_call, seed_call
from graph_helpers import edge, graph, node, start
from llm_helpers import add_rules
from media_helpers import FakeContextLoader, FakeMediaContainer, RecordingSignaller, call_context
from pathways_helpers import NO_SPECULATION, store_pathway
from runtime_rig import MODELS_DIR, WAIT_S, RecordingRuntime, StaticOrgSettings, sim_runtime
from service_helpers import json_log_stream, log_events
from sim_media import EdgeRelay, realtime_telnyx, streamed_call
from sqlalchemy import select
from telnyx_helpers import CALLEE, register
from tenant_rows import OrgFactory, open_database
from tts_helpers import VOICE_ID
from webhook_receiver import Receiver, Reply, served_receiver

from va_common.time import SystemClock, utcnow
from va_db.call_object import load_call_objects
from va_db.models import Call, CallEvent
from va_db.repositories import CallTurnRepo
from va_db.session import Database
from va_domain.org import OrgSettings
from va_elevenlabs import ElevenLabsRest
from va_elevenlabs.tts_models import DEFAULT_TTS_MODEL
from va_engine.models import PathwayGraph, load_engine_state
from va_engine.pathway_call import engine_state_key
from va_media.pathway_runtime import PathwayTools
from va_media.runtime import open_persistence
from va_media.runtime_config import ERROR_END
from va_media.session import STREAM_CLOSED, SessionEnd
from va_pathways.call_graphs import CallGraphCache
from va_sim.server import running_sims
from va_sim.state import SimState
from va_voice.phrase_cache import PhraseCache
from va_workflows.activities.prerender import OpenerPrerender

pytestmark = pytest.mark.integration

WELCOME: Final = "Welcome to Bella Pasta."
CHECKING: Final = "Let me check that for you."
GOODBYE: Final = "Goodbye!"

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
async def pathway_rig(media_db_url: str) -> AsyncIterator[PathwayRig]:
    """The runtime with pathway calls on the media database: one Redis (the container's) for the engine
    snapshot, the end routine and the events."""
    relay = EdgeRelay()
    state = realtime_telnyx(relay)
    container = FakeMediaContainer()
    receiver = Receiver(Reply(200, {"content-type": "application/json"}, body=b'{"eta": 25}'))
    async with (
        open_database(media_db_url) as database,
        running_sims(state, ports={"elevenlabs": 0, "llm": 0}) as urls,
        served_receiver(receiver) as hook_port,
        httpx.AsyncClient(base_url=urls["llm"]) as llm,
        httpx.AsyncClient() as tools_http,
        open_persistence(database, container.redis, clock=SystemClock()) as persistence,
        sim_runtime(
            state,
            urls,
            org_settings=StaticOrgSettings(NO_SPECULATION),
            phrase_redis=container.redis,
            persistence=persistence,
            database=database,
            end_redis=container.redis,
            pathways=PathwayTools(graphs=CallGraphCache(database), tools_http=tools_http, allow_http=True),
        ) as parts,
    ):
        await add_rules(llm, RULES)
        yield PathwayRig(
            state,
            llm,
            parts.body,
            container,
            parts.signaller,
            relay,
            database,
            receiver,
            hook_port,
            urls["elevenlabs"],
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


async def test_a_pathway_call_routes_calls_the_webhook_and_ends(media_db_url: str) -> None:
    async with pathway_rig(media_db_url) as r:
        call = await _stored(r)
        r.scenario(
            "wait_agent_speech: {}",
            "say: {text: where is my delivery}",
            "wait_agent_speech: {then_silence_ms: 1500}",
            "say: {text: thanks bye}",
            "expect_hangup: {}",
        )
        async with r.call(call, **await _pathway_of(r, call)):
            end = await r.body.end()
            signals = await r.signalled()
        assert end == SessionEnd("ended", "agent_end_call", "agent")
        assert signals == [("agent_end_call", "agent")]
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
