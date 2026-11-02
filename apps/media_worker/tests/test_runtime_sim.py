"""The call runtime end to end on the simulators (wall clock): FakeTelnyx streams a scripted call to a served
media worker whose body is :class:`CallRuntime` over FakeElevenLabs (STT/TTS), FakeLLM, and FakeTelnyx for
outbound DTMF (specs/07 §16 Decision P05-T10a, §11 Decision P05-T10b)."""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

import fakeredis
import httpx
from llm_helpers import add_rules
from media_helpers import FakeContextLoader
from service_helpers import settings_of
from sim_media import StreamedCall, realtime_telnyx, streamed_call
from starlette.applications import Starlette
from telnyx_helpers import CALLEE, register
from tts_helpers import VOICE_ID

from va_common.http import http_client
from va_common.metrics import Service, build_metrics, new_registry
from va_common.time import SystemClock
from va_domain.org import OrgSettings
from va_llm import LlmGateway, LlmMetrics, ProviderRegistry
from va_llm.providers import platform_provider
from va_llm.tokens import default_counter
from va_media.runtime import LLM_HTTP_TIMEOUT, CallRuntime
from va_media.runtime_config import ERROR_END
from va_media.session import STREAM_CLOSED, CallSession, SessionEnd
from va_media.settings import MediaSettings
from va_sim.server import running_sims, simulator_routes
from va_sim.state import SimState
from va_sim.telnyx.store import DEFAULT_API_KEY
from va_telnyx.settings import telnyx_factory
from va_voice.vad import VadExecutor
from va_voice.vad_model import SileroModel

MODELS_DIR: Final = Path(__file__).resolve().parents[3] / "models"
WAIT_S: Final = 20.0
OPENER: Final = "Hi, this is Maya."


class StaticOrgSettings:
    """An org settings source answering ``settings`` (``None``: the org is gone)."""

    def __init__(self, settings: OrgSettings | None = None) -> None:
        self.settings = settings
        self.reads: list[uuid.UUID] = []

    async def get(self, org_id: uuid.UUID) -> OrgSettings | None:
        self.reads.append(org_id)
        return self.settings


class RecordingRuntime:
    """The session body: :class:`CallRuntime`, with every session end recorded."""

    def __init__(self, runtime: CallRuntime) -> None:
        self.runtime = runtime
        self.ends: asyncio.Queue[SessionEnd] = asyncio.Queue()

    async def __call__(self, session: CallSession) -> SessionEnd:
        end = await self.runtime(session)
        self.ends.put_nowait(end)
        return end

    async def end(self) -> SessionEnd:
        async with asyncio.timeout(WAIT_S):
            return await self.ends.get()


@dataclass
class Rig:
    state: SimState
    llm: httpx.AsyncClient
    body: RecordingRuntime
    org_settings: StaticOrgSettings

    @asynccontextmanager
    async def call(self, *, answered: bool = True, **config: Any) -> AsyncIterator[StreamedCall]:
        """A streamed call; the carrier state says ``answered`` unless ``answered=False`` (no 2 s wait)."""
        values: dict[str, Any] = {"provider_voice_id": VOICE_ID, "first_sentence": OPENER, **config}
        loader = FakeContextLoader(**values)
        async with streamed_call(
            MODELS_DIR, self.body, state=self.state, loader=loader, answered=answered
        ) as streamed:
            yield streamed

    def scenario(self, *steps: str) -> None:
        text = f'id: rt-{secrets.token_hex(4)}\nmatch: {{to: "{CALLEE}"}}\nanswer: {{after_ms: 200}}\n'
        text += "steps:\n" + "".join(f"  - {step}\n" for step in steps)
        register(self.state, text)

    def user_messages(self) -> list[str]:
        """The content of the last user message of every FakeLLM chat request."""
        found: list[str] = []
        for request in self.state.llm.requests:
            users = [m for m in request.messages if isinstance(m, dict) and m.get("role") == "user"]
            if users:
                found.append(str(users[-1].get("content")))
        return found


async def no_byo_key(_account_id: uuid.UUID) -> str | None:
    return None


@asynccontextmanager
async def rig(org: OrgSettings | None = None) -> AsyncIterator[Rig]:
    state = realtime_telnyx()
    telnyx_app = Starlette(routes=simulator_routes("telnyx", state))
    async with running_sims(state, ports={"elevenlabs": 0, "llm": 0}) as urls:
        settings = settings_of(
            MediaSettings,
            elevenlabs_ws_base=urls["elevenlabs"].replace("http://", "ws://"),
            llm_default_base_url=f"{urls['llm']}/v1",
            telnyx_api_base="http://sim/v2",
            telnyx_api_key=DEFAULT_API_KEY,
        )
        metrics = build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False))
        clock = SystemClock()
        org_settings = StaticOrgSettings(org if org is not None else OrgSettings())
        async with (
            http_client(settings, timeout=LLM_HTTP_TIMEOUT) as http,
            VadExecutor(1) as executor,
            httpx.AsyncClient(base_url=urls["llm"]) as llm,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=telnyx_app)) as telnyx_http,
        ):
            gateway = LlmGateway(
                ProviderRegistry(platform_provider(settings)),
                http,
                clock=clock,
                metrics=LlmMetrics(metrics.registry, metrics),
            )
            runtime = CallRuntime(
                settings=settings,
                gateway=gateway,
                counter=default_counter(),
                org_settings=org_settings,
                vad_model=SileroModel.load(MODELS_DIR),
                vad_executor=executor,
                clock=clock,
                metrics=metrics,
                telnyx=telnyx_factory(
                    settings,
                    http=telnyx_http,
                    redis=fakeredis.FakeAsyncRedis(),
                    resolve_key=no_byo_key,
                    clock=clock,
                ),
            )
            yield Rig(state, llm, RecordingRuntime(runtime), org_settings)


def spoken(state: SimState) -> list[str]:
    """The text FakeElevenLabs generated per TTS context, in order (the socket's own record has none)."""
    texts = [request.generated_text.strip() for request in state.elevenlabs.tts_requests]
    return [text for text in texts if text]


async def test_caller_turn_is_answered_by_the_task_brain() -> None:
    async with rig() as r:
        await add_rules(
            r.llm,
            "rules:\n  - id: table\n    match: {last_user_regex: 'table'}\n"
            "    respond: {content: 'Sure, for how many people?'}\n",
        )
        r.scenario(
            "wait_agent_speech: {}",
            "say: {text: I would like to book a table}",
            "wait_agent_speech: {}",
            "hangup: {}",
        )
        async with r.call():
            end = await r.body.end()
        assert end == STREAM_CLOSED
        assert any("book a table" in text for text in r.user_messages())
        assert [request.rule_id for request in r.state.llm.requests] == ["table"]
        assert spoken(r.state) == [OPENER, "Sure, for how many people?"]


async def test_end_call_tool_ends_the_session_as_agent_end_call() -> None:
    async with rig() as r:
        await add_rules(
            r.llm,
            "rules:\n  - id: bye\n    match: {last_user_regex: 'bye'}\n"
            "    respond: {tool_calls: [{name: end_call,\n"
            "      arguments: {reason: done, final_message: 'Goodbye now.'}}]}\n",
        )
        r.scenario("wait_agent_speech: {}", "say: {text: that is all thanks bye}", "expect_hangup: {}")
        async with r.call():
            end = await r.body.end()
        assert end == SessionEnd("ended", "agent_end_call", "agent")
        assert spoken(r.state) == [OPENER, "Goodbye now."]


async def test_carrier_hangup_ends_the_session_as_stream_closed() -> None:
    async with rig() as r:
        r.scenario("wait_agent_speech: {}", "hangup: {}")
        async with r.call():
            end = await r.body.end()
        assert end == STREAM_CLOSED
        assert spoken(r.state) == [OPENER]
        assert r.state.llm.requests == []


async def test_rejected_vendor_key_ends_with_error_before_any_audio() -> None:
    async with rig() as r:
        r.state.elevenlabs.api_keys = ("only-this-key",)
        r.scenario("silence: {ms: 60000}")
        async with r.call():
            end = await r.body.end()
        assert end == ERROR_END
        assert spoken(r.state) == []
        assert not r.state.elevenlabs.sockets
        assert not r.state.elevenlabs.stt.sockets


async def test_non_task_runner_is_refused_without_vendor_sockets() -> None:
    async with rig() as r:
        r.scenario("silence: {ms: 60000}")
        async with r.call(runner_type="pathway", task=None, pathway_id=str(uuid.uuid4()), pathway_version=1):
            end = await r.body.end()
        assert end == ERROR_END
        assert r.org_settings.reads == []
        assert r.state.elevenlabs.stt.sessions == []


async def test_missing_org_ends_with_error() -> None:
    async with rig() as r:
        r.org_settings.settings = None
        r.scenario("silence: {ms: 60000}")
        async with r.call():
            end = await r.body.end()
        assert end == ERROR_END
        assert len(r.org_settings.reads) == 1


def dtmf_groups(streamed: StreamedCall) -> list[str]:
    return [entry.digits for entry in streamed.sim_call().dtmf_sent]


async def test_the_precall_sequence_reaches_telnyx_before_the_agent_speaks() -> None:
    async with rig() as r:
        r.scenario("wait_agent_speech: {}", "hangup: {}")
        async with r.call(precall_dtmf_sequence="12w3", first_sentence=None) as streamed:
            end = await r.body.end()
            sent = streamed.sim_call().dtmf_sent
        assert end == STREAM_CLOSED
        assert [entry.digits for entry in sent] == ["12", "3"]
        # The opener is generated (FakeLLM) only after the last group and the 500 ms settle.
        [opener_request] = r.state.llm.requests
        last_group = datetime.fromisoformat(sent[-1].at)
        first_group = datetime.fromisoformat(sent[0].at)
        assert (last_group - first_group).total_seconds() >= 0.5
        assert (datetime.fromisoformat(opener_request.received_at) - last_group).total_seconds() >= 0.5


async def test_a_press_button_tool_call_reaches_telnyx() -> None:
    async with rig() as r:
        await add_rules(
            r.llm,
            "rules:\n  - id: menu\n    match: {last_user_regex: 'press', call_index: 0}\n"
            "    respond: {tool_calls: [{name: press_button, arguments: {digits: '5w6'}}]}\n",
        )
        r.scenario(
            "wait_agent_speech: {}",
            "say: {text: please press five then six}",
            "wait_agent_speech: {}",
            "hangup: {}",
        )
        async with r.call() as streamed:
            end = await r.body.end()
            groups = dtmf_groups(streamed)
        assert end == STREAM_CLOSED
        assert groups == ["5", "6"]
        assert r.state.llm.requests[0].rule_id == "menu"


async def test_wait_for_greeting_holds_the_opener_until_the_callee_speaks() -> None:
    async with rig() as r:
        r.scenario("say: {text: Hello who is this}", "wait_agent_speech: {}", "hangup: {}")
        async with r.call(wait_for_greeting=True, first_sentence=None):
            end = await r.body.end()
        assert end == STREAM_CLOSED
        # The first model request is the opener, and it already answers the callee's greeting.
        assert len(r.state.llm.requests) == 1
        assert "Hello who is this" in r.user_messages()[0]
        assert len(spoken(r.state)) == 1
