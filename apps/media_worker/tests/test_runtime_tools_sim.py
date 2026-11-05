"""Task calls with custom tools through the media runtime on the simulators (wall clock): FakeTelnyx streams
a scripted caller, FakeLLM asks for an inline tool, the tool reaches a real receiver, and the runtime speaks
the tool's speech and the follow-up reply, publishes ``call.tool``/``call.error`` and keeps the extracted
variables (specs/13 §5; Decision P08-T02b1)."""

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
from llm_client_helpers import served_requests
from llm_helpers import add_rules
from media_helpers import FakeContextLoader, FakeMediaContainer, RecordingSignaller, call_context
from runtime_rig import MODELS_DIR, WAIT_S, RecordingRuntime, StaticOrgSettings, sim_runtime
from sim_media import EdgeRelay, realtime_telnyx, streamed_call
from sqlalchemy import select
from telnyx_helpers import CALLEE, register
from tenant_rows import open_database
from tts_helpers import VOICE_ID
from webhook_receiver import Receiver, Reply, served_receiver

from va_common.crypto import Envelope, LocalKmsProvider
from va_common.time import SystemClock, utcnow
from va_db.models import CallEvent
from va_db.provisioning import OrgProvisioner
from va_db.repositories import OrgDataKeyStore
from va_db.repositories.identity import NewUser
from va_db.session import Database
from va_domain.org import OrgSettings
from va_engine.models import load_engine_state
from va_engine.pathway_call import engine_state_key
from va_media.call_tools import CallToolSource
from va_media.runtime import open_persistence
from va_sim.server import running_sims
from va_sim.state import SimState

pytestmark = pytest.mark.integration

OPENER: Final = "Hi, this is Maya from Acme."
SPEECH: Final = "Let me look up order A7."
REPLY: Final = "Your order is on its way."

RULES: Final = """rules:
  - id: lookup
    match: {last_user_regex: 'order', call_index: 0}
    respond:
      tool_calls: [{name: lookup_order, arguments: {order: A7}}]
  - id: answer
    match: {last_user_regex: 'order', call_index: 1}
    respond: {content: 'Your order is on its way.'}
"""


@dataclass
class ToolsRig:
    state: SimState
    llm: httpx.AsyncClient
    body: RecordingRuntime
    container: FakeMediaContainer
    signaller: RecordingSignaller
    relay: EdgeRelay
    database: Database
    provisioner: OrgProvisioner
    receiver: Receiver
    port: int

    async def seeded(self) -> SeededCall:
        provisioned = await self.provisioner.provision(
            name="Acme", owner=NewUser(email=f"owner-{secrets.token_hex(4)}@acme.test")
        )
        return await seed_call(
            self.database,
            org_id=provisioned.org.id,
            status="in_progress",
            answered_at=utcnow(),
            variables={"source": "api"},
        )

    def tool(self) -> dict[str, Any]:
        return {
            "name": "lookup_order",
            "description": "Looks up an order.",
            "url": f"http://127.0.0.1:{self.port}/orders",
            "body": {"order": "{{input.order}}"},
            "speech": "Let me look up order {{input.order}}.",
            "input_schema": {"type": "object", "properties": {"order": {"type": "string"}}},
            "response": {"status": "$.status"},
        }

    async def call(self, seeded: SeededCall) -> None:
        """Run one call to its end (the caller asks about an order, then hangs up)."""
        text = f'id: tools-{uuid.uuid4().hex[:8]}\nmatch: {{to: "{CALLEE}"}}\nanswer: {{after_ms: 200}}\n'
        steps = (
            "wait_agent_speech: {}",
            "say: {text: where is my order}",
            "wait_agent_speech: {then_silence_ms: 2000}",
            "hangup: {}",
        )
        register(self.state, text + "steps:\n" + "".join(f"  - {step}\n" for step in steps))
        loader = FakeContextLoader()
        loader.contexts[seeded.call_id] = call_context(
            seeded.call_id,
            org_id=seeded.org_id,
            provider_voice_id=VOICE_ID,
            wait_for_greeting=False,
            first_sentence=OPENER,
            inline_tools=[self.tool()],
        )
        options: dict[str, Any] = {"carrier": {"answered_by": "human"}, "relay": self.relay}
        options.update(state=self.state, container=self.container, call_id=seeded.call_id, answered=True)
        async with streamed_call(MODELS_DIR, self.body, loader=loader, **options):
            await self.body.end()
            async with asyncio.timeout(WAIT_S):
                await self.signaller.signalled.wait()

    async def events(self, seeded: SeededCall) -> list[CallEvent]:
        async with self.database.tenant_session(seeded.org_id) as session:
            statement = select(CallEvent).where(CallEvent.call_id == seeded.call_id).order_by(CallEvent.seq)
            return list(await session.scalars(statement))


@asynccontextmanager
async def tools_rig(media_db_url: str, reply: Reply) -> AsyncIterator[ToolsRig]:
    """The runtime with task-mode tools on the media database (one Redis: snapshots, end routine, events)."""
    relay = EdgeRelay()
    state = realtime_telnyx(relay)
    container = FakeMediaContainer()
    receiver = Receiver(reply)
    async with (
        open_database(media_db_url) as database,
        running_sims(state, ports={"elevenlabs": 0, "llm": 0}) as urls,
        served_receiver(receiver) as port,
        httpx.AsyncClient(base_url=urls["llm"]) as llm,
        httpx.AsyncClient() as tools_http,
        open_persistence(database, container.redis, clock=SystemClock()) as persistence,
    ):
        envelope = Envelope(LocalKmsProvider.from_hex(secrets.token_hex(32)), OrgDataKeyStore(database))
        source = CallToolSource(
            database=database, envelope=envelope, tools_http=tools_http, allow_http=True, clock=SystemClock()
        )
        async with sim_runtime(
            state,
            urls,
            org_settings=StaticOrgSettings(OrgSettings()),
            phrase_redis=container.redis,
            persistence=persistence,
            database=database,
            end_redis=container.redis,
            call_tools=source,
        ) as parts:
            await add_rules(llm, RULES)
            yield ToolsRig(
                state,
                llm,
                parts.body,
                container,
                parts.signaller,
                relay,
                database,
                OrgProvisioner(database, envelope),
                receiver,
                port,
            )


def _spoken(state: SimState) -> list[str]:
    texts = [request.generated_text.strip() for request in state.elevenlabs.tts_requests]
    return [text for text in texts if text]


async def test_a_task_call_speaks_during_the_tool_and_keeps_its_answer(media_db_url: str) -> None:
    reply = Reply(200, {"content-type": "application/json"}, body=b'{"status": "shipped"}')
    async with tools_rig(media_db_url, reply) as r:
        seeded = await r.seeded()
        await r.call(seeded)
        assert _spoken(r.state)[:3] == [OPENER, SPEECH, REPLY]
        [request] = r.receiver.requests
        assert json.loads(request.body) == {"order": "A7"}
        assert request.headers["x-va-signature"].startswith("t=")
        followup = (await served_requests(r.llm))[1]
        [answer] = [m for m in followup["messages"] if m["role"] == "tool"]
        assert json.loads(answer["content"]) == {
            "status": 200,
            "data": {"status": "shipped"},
            "extracted": {"status": "shipped"},
        }
        stored = await r.container.redis.get(engine_state_key(seeded.call_id))
        assert stored is not None
        assert load_engine_state(stored).variables["status"] == "shipped"
        row = await load_call(r.database, seeded)
        assert row.variables == {"source": "api", "status": "shipped"}
        [tool] = [e for e in await r.events(seeded) if e.event_type == "call.tool"]
        assert (tool.message, tool.data["outcome"], tool.data["status_code"]) == (
            "Tool lookup_order: ok",
            "ok",
            200,
        )


async def test_a_failing_tool_reports_tool_failed(media_db_url: str) -> None:
    async with tools_rig(media_db_url, Reply(503)) as r:
        seeded = await r.seeded()
        await r.call(seeded)
        events = await r.events(seeded)
        [tool] = [e for e in events if e.event_type == "call.tool"]
        assert tool.data["outcome"] == "http_error"
        [error] = [e for e in events if e.event_type == "call.error"]
        assert (error.level, error.message, error.data) == (
            "warning",
            "Tool call failed",
            {"code": "TOOL_FAILED", "detail": "lookup_order: http_error (503)"},
        )
        assert (await load_call(r.database, seeded)).variables == {"source": "api"}
