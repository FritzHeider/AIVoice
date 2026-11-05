"""Task calls with custom tools through the media runtime on the simulators (wall clock): FakeTelnyx streams
a scripted caller, FakeLLM asks for an inline tool, the tool reaches a real receiver, and the runtime speaks
the tool's speech and the follow-up reply, publishes ``call.tool``/``call.error`` and keeps the extracted
variables (specs/13 §5; Decision P08-T02b1). The executor-backed system tools ``send_sms`` and
``schedule_callback`` run through the same runtime (specs/13 §6; Decision P08-T02b2)."""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Final

import httpx
import pytest
from call_rows import SeededCall, load_call, seed_call
from llm_client_helpers import served_requests
from llm_helpers import add_rules
from media_helpers import FakeContextLoader, FakeMediaContainer, RecordingSignaller, call_context
from runtime_rig import MODELS_DIR, WAIT_S, RecordingRuntime, StaticOrgSettings, sim_runtime
from service_helpers import json_log_stream, log_events
from sim_media import EdgeRelay, realtime_telnyx, streamed_call
from sms_rows import add_sms_number
from sqlalchemy import select, update
from telnyx_helpers import CALLEE, register
from tenant_rows import open_database
from tts_helpers import VOICE_ID
from webhook_receiver import Receiver, Reply, served_receiver

from va_common.crypto import Envelope, LocalKmsProvider
from va_common.time import SystemClock, utcnow
from va_db.models import Call, CallEvent, PhoneNumber
from va_db.provisioning import OrgProvisioner
from va_db.repositories import OrgDataKeyStore
from va_db.repositories.identity import NewUser
from va_db.session import Database
from va_domain.call_workflow import CallWorkflowInput
from va_domain.org import OrgSettings
from va_engine.models import load_engine_state
from va_engine.pathway_call import engine_state_key
from va_media.call_tools import CallToolSource
from va_media.runtime import open_persistence
from va_sim.server import running_sims
from va_sim.state import SimState
from va_sim.telnyx.store import DEFAULT_API_KEY

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
    sms_number: str | None = None
    starter: RecordingStarter | None = None

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

    async def call(
        self,
        seeded: SeededCall,
        *,
        say: str = "where is my order",
        tools: bool = True,
        from_number: str | None = None,
        to_number: str | None = None,
    ) -> None:
        """Run one call to its end (the caller says ``say``, then hangs up); ``tools``: the call carries the
        inline tool; ``from_number`` / ``to_number`` replace the context's agent / other party number."""
        text = f'id: tools-{uuid.uuid4().hex[:8]}\nmatch: {{to: "{CALLEE}"}}\nanswer: {{after_ms: 200}}\n'
        steps = (
            "wait_agent_speech: {}",
            f"say: {{text: {say}}}",
            "wait_agent_speech: {then_silence_ms: 2000}",
            "hangup: {}",
        )
        register(self.state, text + "steps:\n" + "".join(f"  - {step}\n" for step in steps))
        loader = FakeContextLoader()
        context = call_context(
            seeded.call_id,
            org_id=seeded.org_id,
            provider_voice_id=VOICE_ID,
            wait_for_greeting=False,
            first_sentence=OPENER,
            inline_tools=[self.tool()] if tools else [],
        )
        if from_number is not None:
            context = context.model_copy(update={"from_number": from_number})
        if to_number is not None:
            context = context.model_copy(update={"to_number": to_number})
        loader.contexts[seeded.call_id] = context
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
async def tools_rig(  # noqa: PLR0913 - the rig's options, keyword-only
    media_db_url: str,
    reply: Reply,
    *,
    rules: str = RULES,
    org: OrgSettings | None = None,
    messaging: bool = False,
    starter: RecordingStarter | None = None,
) -> AsyncIterator[ToolsRig]:
    """The runtime with task-mode tools on the media database (one Redis: snapshots, end routine, events);
    ``messaging`` gives FakeTelnyx an SMS number, ``starter`` records ``schedule_callback`` starts."""
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
            org_settings=StaticOrgSettings(org if org is not None else OrgSettings()),
            phrase_redis=container.redis,
            persistence=persistence,
            database=database,
            end_redis=container.redis,
            call_tools=source,
            messaging=messaging,
            callback_starter=starter,
        ) as parts:
            await add_rules(llm, rules)
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
                parts.sms_number,
                starter,
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


@dataclass
class RecordingStarter:
    """Records the ``CallWorkflow`` starts of callbacks."""

    started: list[CallWorkflowInput] = field(default_factory=list)

    async def start(self, call: CallWorkflowInput) -> None:
        self.started.append(call)

    async def abandon(self, call_id: uuid.UUID) -> None:
        raise AssertionError(call_id)


def _offered(request: dict[str, Any]) -> list[str]:
    return [tool["function"]["name"] for tool in request["tools"] or []]


SMS_RULES: Final = """rules:
  - id: sms
    match: {last_user_regex: 'text', call_index: 0}
    respond:
      tool_calls: [{name: send_sms, arguments: {message: 'Your code is 42.'}}]
  - id: sent
    match: {last_user_regex: 'text', call_index: 1}
    respond: {content: 'I just sent you a text.'}
"""


@asynccontextmanager
async def _org_number(database: Database, seeded: SeededCall, e164: str) -> AsyncIterator[None]:
    """FakeTelnyx's SMS number as the only active number with that E.164 (the sim hands out the same number in
    every test, other tests leave it active in their orgs, and active numbers are unique), released after."""
    await _release(database, e164)
    await add_sms_number(database, seeded.org_id, e164)
    try:
        yield
    finally:
        await _release(database, e164)


async def _release(database: Database, e164: str) -> None:
    async with database.system_session() as session:
        await session.execute(
            update(PhoneNumber)
            .where(PhoneNumber.e164 == e164, PhoneNumber.status == "active")
            .values(status="released")
        )


async def test_send_sms_texts_the_callee_from_the_agent_number(media_db_url: str) -> None:
    async with tools_rig(media_db_url, Reply(200), rules=SMS_RULES, messaging=True) as r:
        assert r.sms_number is not None
        seeded = await r.seeded()
        async with _org_number(r.database, seeded, r.sms_number):
            await r.call(seeded, say="please text me the code", tools=False, from_number=r.sms_number)
        first, followup = (await served_requests(r.llm))[:2]
        assert "send_sms" in _offered(first)
        assert "schedule_callback" not in _offered(first)
        [answer] = [m for m in followup["messages"] if m["role"] == "tool"]
        assert json.loads(answer["content"]) == {"status": "sent", "segments": 1}
        [record] = r.state.telnyx.accounts[DEFAULT_API_KEY].messages.values()
        assert record["text"] == "Your code is 42."
        assert record["from"]["phone_number"] == r.sms_number
        assert record["to"][0]["phone_number"] == call_context(seeded.call_id).to_number
        [sms] = [e for e in await r.events(seeded) if e.event_type == "call.sms_sent"]
        assert (sms.message, sms.data["source"]) == ("SMS sent", "tool")
        assert "I just sent you a text." in _spoken(r.state)


def _callback_rules(when: str) -> str:
    return f"""rules:
  - id: callback
    match: {{last_user_regex: 'call me back', call_index: 0}}
    respond:
      tool_calls: [{{name: schedule_callback, arguments: {{datetime_iso: '{when}', note: busy now}}}}]
  - id: booked
    match: {{last_user_regex: 'call me back', call_index: 1}}
    respond: {{content: 'I will call you back tomorrow.'}}
"""


async def test_schedule_callback_creates_a_scheduled_child_and_starts_it(media_db_url: str) -> None:
    when = (utcnow() + timedelta(days=1)).replace(microsecond=0)
    org = OrgSettings.model_validate({"engine": {"allow_callbacks": True}})
    starter = RecordingStarter()
    rules = _callback_rules(when.isoformat())
    async with tools_rig(media_db_url, Reply(200), rules=rules, org=org, starter=starter) as r:
        seeded = await r.seeded()
        await r.call(seeded, say="please call me back tomorrow", tools=False)
        first, followup = (await served_requests(r.llm))[:2]
        # The context's agent number is no SMS number of the org: send_sms is not offered.
        assert "send_sms" not in _offered(first)
        assert "schedule_callback" in _offered(first)
        [answer] = [m for m in followup["messages"] if m["role"] == "tool"]
        content = json.loads(answer["content"])
        assert content["status"] == "scheduled"
        child_id = uuid.UUID(content["scheduled_call_id"])
        async with r.database.tenant_session(seeded.org_id) as session:
            child = await session.get_one(Call, child_id)
        assert (child.status, child.parent_call_id, child.start_time) == ("scheduled", seeded.call_id, when)
        assert child.config["callback_note"] == "busy now"
        assert starter.started == [
            CallWorkflowInput(call_id=child_id, org_id=seeded.org_id, scheduled_for=when)
        ]
        assert "I will call you back tomorrow." in _spoken(r.state)


async def test_callbacks_are_not_offered_when_the_org_does_not_allow_them(media_db_url: str) -> None:
    starter = RecordingStarter()
    rules = _callback_rules((utcnow() + timedelta(days=1)).isoformat())
    async with tools_rig(media_db_url, Reply(200), rules=rules, starter=starter) as r:
        seeded = await r.seeded()
        await r.call(seeded, say="please call me back tomorrow", tools=False)
        first = (await served_requests(r.llm))[0]
        assert _offered(first) == ["end_call", "press_button"]
        assert starter.started == []


async def test_no_system_tool_is_offered_without_a_callable_number(media_db_url: str) -> None:
    starter = RecordingStarter()
    org = OrgSettings.model_validate({"engine": {"allow_callbacks": True}})
    rules = _callback_rules((utcnow() + timedelta(days=1)).isoformat())
    async with tools_rig(media_db_url, Reply(200), rules=rules, org=org, starter=starter) as r:
        seeded = await r.seeded()
        stream = json_log_stream()
        await r.call(seeded, say="please call me back tomorrow", tools=False, to_number="anonymous")
        first = (await served_requests(r.llm))[0]
        assert _offered(first) == ["end_call", "press_button"]
        assert starter.started == []
        assert any(
            e["event"] == "media.system_tools_skipped" and e["reason"] == "no_callable_number"
            for e in log_events(stream)
        )
