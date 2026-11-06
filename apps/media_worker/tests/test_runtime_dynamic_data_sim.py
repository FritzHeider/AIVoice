"""Per-turn dynamic data through the media runtime on the simulators (wall clock): FakeTelnyx streams a caller
who speaks twice, the call's ``every_turn`` entry is fetched from a real receiver at each turn, and the
prompt FakeLLM receives carries the refreshed context (specs/13 §7 Decision P08-T03b). The call has no tools:
its refreshed variables still reach ``calls.variables`` through the engine snapshot."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Final

import httpx
import pytest
from call_rows import SeededCall, load_call, seed_call
from gc_cycles import cycles_through, unreachable_after
from llm_client_helpers import served_requests
from media_helpers import FakeContextLoader, FakeMediaContainer, RecordingSignaller, call_context
from runtime_rig import RecordingRuntime, StaticOrgSettings, run_human_call, sim_backends, sim_runtime
from sim_media import EdgeRelay, realtime_telnyx
from telephony_rig import call_event_rows
from telnyx_helpers import CALLEE, register
from tts_helpers import VOICE_ID
from webhook_receiver import Receiver, Reply

from va_common.time import SystemClock, utcnow
from va_db.models import CallEvent
from va_db.session import Database
from va_domain.org import OrgSettings
from va_engine.models import load_engine_state
from va_engine.pathway_call import engine_state_key
from va_media.call_dynamic_data import TurnDataSource
from va_sim.state import SimState

pytestmark = pytest.mark.integration

OPENER: Final = "Hi, this is Maya from Acme."
JSON: Final = {"content-type": "application/json"}
CALL_PARTS: Final = (
    "TaskBrain",
    "TurnRecorder",
    "TurnDynamicData",
    "DynamicDataFetcher",
    "HttpExecutor",
    "EngineStateSaver",
)


def _plan(name: str) -> Reply:
    return Reply(200, JSON, body=f'{{"plan": "{name}"}}'.encode())


@dataclass
class DataRig:
    state: SimState
    llm: httpx.AsyncClient
    body: RecordingRuntime
    container: FakeMediaContainer
    signaller: RecordingSignaller
    relay: EdgeRelay
    database: Database
    receiver: Receiver
    port: int

    def fetch(self) -> dict[str, Any]:
        return {
            "url": f"http://127.0.0.1:{self.port}/plan?phone={{{{to}}}}",
            "timing": "every_turn",
            "response_data": [{"name": "plan", "data": "$.plan", "context": "Their plan is {{plan}}."}],
        }

    async def call(self, seeded: SeededCall) -> None:
        """Run one call to its end: the caller speaks twice, then hangs up. The context carries what the
        pre-call fetch left (``plan`` = bronze and its line)."""
        text = f'id: data-{uuid.uuid4().hex[:8]}\nmatch: {{to: "{CALLEE}"}}\nanswer: {{after_ms: 200}}\n'
        steps = (
            "wait_agent_speech: {}",
            "say: {text: which plan am I on}",
            "wait_agent_speech: {then_silence_ms: 1500}",
            "say: {text: and what about now}",
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
            dynamic_data=[self.fetch()],
        ).model_copy(
            update={"variables": {"plan": "bronze"}, "turn_sections": {0: ["Their plan is bronze."]}}
        )
        await run_human_call(
            self.body,
            self.signaller,
            loader,
            state=self.state,
            container=self.container,
            relay=self.relay,
            call_id=seeded.call_id,
        )

    async def data_events(self, seeded: SeededCall) -> list[CallEvent]:
        """The call's ``call.dynamic_data`` rows in order."""
        rows = await call_event_rows(self.database, seeded)
        return [row for row in rows if row.event_type == "call.dynamic_data"]


@asynccontextmanager
async def data_rig(media_db_url: str, *replies: Reply) -> AsyncIterator[DataRig]:
    """The runtime with a per-turn data source and NO call tool source on the media database (one Redis:
    the ``dyn:`` cache, snapshots, end routine, events); the receiver answers ``/plan`` with ``replies``."""
    relay = EdgeRelay()
    state = realtime_telnyx(relay)
    container = FakeMediaContainer()
    receiver = Receiver(Reply(500))
    receiver.script("/plan", *replies)
    async with sim_backends(media_db_url, state, receiver, container.redis) as backends:
        source = TurnDataSource(
            tools_http=backends.tools_http, allow_http=True, redis=container.redis, clock=SystemClock()
        )
        async with sim_runtime(
            state,
            backends.urls,
            org_settings=StaticOrgSettings(OrgSettings()),
            phrase_redis=container.redis,
            persistence=backends.persistence,
            database=backends.database,
            end_redis=container.redis,
            turn_data=source,
        ) as parts:
            yield DataRig(
                state,
                backends.llm,
                parts.body,
                container,
                parts.signaller,
                relay,
                backends.database,
                receiver,
                backends.receiver_port,
            )


def _system(request: dict[str, Any]) -> str:
    content: str = request["messages"][0]["content"]
    return content


async def test_each_turn_s_prompt_carries_the_refreshed_context(media_db_url: str) -> None:
    async with data_rig(media_db_url, _plan("gold"), _plan("platinum")) as r:
        seeded = await seed_call(
            r.database, status="in_progress", answered_at=utcnow(), variables={"source": "api"}
        )
        with unreachable_after() as garbage:
            await r.call(seeded)
        assert cycles_through(garbage, CALL_PARTS) == {}

        assert [request.path for request in r.receiver.requests] == ["/plan", "/plan"]
        fetched = r.receiver.requests[0]
        assert fetched.headers["x-va-call-id"] == str(seeded.call_id)
        assert "x-va-signature" not in fetched.headers
        first, second = [_system(request) for request in await served_requests(r.llm)][:2]
        assert first.endswith("\n\n## Context\nTheir plan is gold.")
        assert "- plan: gold" in first
        assert second.endswith("\n\n## Context\nTheir plan is platinum.")
        assert "- plan: platinum" in second
        assert "gold" not in second
        assert "bronze" not in first + second

        events = await r.data_events(seeded)
        assert [(event.level, event.message) for event in events] == [
            ("info", "Dynamic data: 1 variable set")
        ] * 2
        assert [
            (event.data["timing"], event.data["index"], event.data["url"], event.data["variables_set"])
            for event in events
        ] == [("every_turn", 0, f"http://127.0.0.1:{r.port}/plan", ["plan"])] * 2
        stored = await r.container.redis.get(engine_state_key(seeded.call_id))
        assert stored is not None
        assert load_engine_state(stored).variables == {"plan": "platinum"}
        assert (await load_call(r.database, seeded)).variables == {"source": "api", "plan": "platinum"}


async def test_a_failed_refresh_keeps_the_earlier_context_and_the_call_goes_on(media_db_url: str) -> None:
    async with data_rig(media_db_url, _plan("gold"), Reply(503, JSON, body=b"{}")) as r:
        seeded = await seed_call(r.database, status="in_progress", answered_at=utcnow())
        await r.call(seeded)

        first, second = [_system(request) for request in await served_requests(r.llm)][:2]
        assert first.endswith("\n\n## Context\nTheir plan is gold.")
        assert second.endswith("\n\n## Context\nTheir plan is gold.")
        events = await r.data_events(seeded)
        assert [(event.level, event.message) for event in events] == [
            ("info", "Dynamic data: 1 variable set"),
            ("warning", "Dynamic data failed: http_error (503)"),
        ]
        assert (await load_call(r.database, seeded)).variables == {"plan": "gold"}
