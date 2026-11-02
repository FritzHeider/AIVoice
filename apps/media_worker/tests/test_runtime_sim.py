"""The call runtime end to end on the simulators (wall clock): FakeTelnyx streams a scripted call to a served
media worker whose body is :class:`CallRuntime` over FakeElevenLabs (STT/TTS), FakeLLM, and FakeTelnyx for
outbound DTMF (specs/07 §16 Decision P05-T10a, §11 Decisions P05-T10b and P05-T10d)."""

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
import pytest
from call_rows import SeededCall, load_call, seed_call
from llm_helpers import add_rules
from media_helpers import FakeContextLoader, FakeMediaContainer, RecordingSignaller, call_context
from resolved_configs import resolved_config
from service_helpers import settings_of
from sim_media import StreamedCall, realtime_telnyx, streamed_call
from sqlalchemy import select
from starlette.applications import Starlette
from telnyx_helpers import CALLEE, register
from tenant_rows import open_database
from tts_helpers import VOICE_ID

from va_billing.keys import concurrency_key
from va_common.http import http_client
from va_common.metrics import Service, build_metrics, new_registry
from va_common.time import SystemClock, utcnow
from va_db.models import Call, CallEvent
from va_db.repositories import CallTurnRepo
from va_db.session import Database
from va_domain.carrier import carrier_state_key
from va_domain.control import StopControl, control_channel
from va_domain.org import OrgSettings
from va_elevenlabs import ElevenLabsRest
from va_elevenlabs.tts_models import DEFAULT_TTS_MODEL
from va_events import ENDED_MESSAGE
from va_llm import LlmGateway, LlmMetrics, ProviderRegistry
from va_llm.providers import platform_provider
from va_llm.tokens import default_counter
from va_media.call_end import CallEnd
from va_media.runtime import LLM_HTTP_TIMEOUT, CallRuntime, open_persistence
from va_media.runtime_config import ERROR_END
from va_media.session import STREAM_CLOSED, CallSession, SessionEnd
from va_media.settings import MediaSettings
from va_media.turns import CallPersistence
from va_sim.server import running_sims, simulator_routes
from va_sim.state import SimState
from va_sim.telnyx.store import DEFAULT_API_KEY
from va_telnyx.client_state import command_id
from va_telnyx.settings import telnyx_factory
from va_voice.phrase_cache import PhraseCache
from va_voice.vad import VadExecutor
from va_voice.vad_model import SileroModel
from va_workflows.activities.prerender import OpenerPrerender

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
    """The session body: :class:`CallRuntime`, with every session end recorded (its end routine too)."""

    def __init__(self, runtime: CallRuntime) -> None:
        self.runtime = runtime
        self.ends: asyncio.Queue[SessionEnd] = asyncio.Queue()
        self.started = asyncio.Event()

    @property
    def end_routine(self) -> CallEnd | None:
        return self.runtime.end_routine

    async def __call__(self, session: CallSession) -> SessionEnd:
        self.started.set()
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
    redis: fakeredis.FakeAsyncRedis
    elevenlabs_url: str
    container: FakeMediaContainer
    signaller: RecordingSignaller

    @asynccontextmanager
    async def call(
        self,
        *,
        answered: bool = True,
        opener_text: str | None = None,
        seeded: SeededCall | None = None,
        **config: Any,
    ) -> AsyncIterator[StreamedCall]:
        """A streamed call; the carrier state says ``answered`` unless ``answered=False`` (no 2 s wait);
        ``seeded``: use that call's id and org (a call row on a real database)."""
        values: dict[str, Any] = {"provider_voice_id": VOICE_ID, "first_sentence": OPENER, **config}
        loader = FakeContextLoader(**values)
        loader.opener_text = opener_text
        call_id = None
        if seeded is not None:
            call_id = seeded.call_id
            loader.contexts[call_id] = call_context(call_id, org_id=seeded.org_id, **values)
        async with streamed_call(
            MODELS_DIR,
            self.body,
            state=self.state,
            loader=loader,
            answered=answered,
            call_id=call_id,
            container=self.container,
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
async def rig(
    org: OrgSettings | None = None,
    *,
    redis: fakeredis.FakeAsyncRedis | None = None,
    persistence: CallPersistence | None = None,
    database: Database | None = None,
) -> AsyncIterator[Rig]:
    """The runtime over the simulators; with ``database`` its end-of-call routine runs too (over the media
    container's Redis, FakeTelnyx, ``persistence``'s events and a :class:`RecordingSignaller`)."""
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
        phrase_redis = redis if redis is not None else fakeredis.FakeAsyncRedis()
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
            telnyx = telnyx_factory(
                settings,
                http=telnyx_http,
                redis=fakeredis.FakeAsyncRedis(),
                resolve_key=no_byo_key,
                clock=clock,
            )
            container, signaller = FakeMediaContainer(), RecordingSignaller()
            end_routine = None
            if database is not None:
                end_routine = CallEnd(
                    database=database,
                    redis=container.redis,
                    signaller=signaller,
                    clock=clock,
                    telnyx=telnyx,
                    events=None if persistence is None else persistence.events,
                    event_rows=None if persistence is None else persistence.event_rows,
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
                telnyx=telnyx,
                redis=phrase_redis,
                persistence=persistence,
                end_routine=end_routine,
            )
            yield Rig(
                state,
                llm,
                RecordingRuntime(runtime),
                org_settings,
                phrase_redis,
                urls["elevenlabs"],
                container,
                signaller,
            )


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


PRERENDERED: Final = "Hello Ana, this is Maya calling from the clinic about your visit."


def socket_texts(state: SimState) -> list[str]:
    """Text generated over the TTS WebSocket (REST renders excluded)."""
    texts = [r.generated_text.strip() for r in state.elevenlabs.tts_requests if r.transport != "rest"]
    return [text for text in texts if text]


async def prerender(r: Rig, **config: Any) -> str | None:
    """Run the worker's pre-render over FakeElevenLabs REST into the rig's Redis; the context's opener."""
    values: dict[str, Any] = {"provider_voice_id": VOICE_ID, "first_sentence": PRERENDERED, **config}
    context = call_context(uuid.uuid4(), **values)
    async with httpx.AsyncClient() as http:
        rest = ElevenLabsRest("sim-eleven-key", r.elevenlabs_url, http, clock=SystemClock())
        cache = PhraseCache(rest, r.redis)
        job = OpenerPrerender(
            cache=cache, org_settings=StaticOrgSettings(OrgSettings()), model_id=DEFAULT_TTS_MODEL
        )
        context = context.model_copy(update={"opener_text": job.opener_for(context, utcnow())})
        result = await job.render(context)
        await cache.aclose()
    assert result.opener
    return context.opener_text


async def test_a_prerendered_opener_plays_from_the_phrase_cache() -> None:
    async with rig() as r:
        opener = await prerender(r, wait_for_greeting=False)
        assert opener == PRERENDERED
        r.scenario("wait_agent_speech: {}", "hangup: {}")
        async with r.call(
            opener_text=opener, first_sentence=PRERENDERED, wait_for_greeting=False
        ) as streamed:
            end = await r.body.end()
            agent_audio = bytes(streamed.sim_call().audio.agent)
        assert end == STREAM_CLOSED
        # The caller heard the cached opener (over a second of PCM16 8 kHz agent audio, not silence).
        assert len(agent_audio) > 16_000
        assert any(agent_audio)
        assert socket_texts(r.state) == []
        assert r.state.llm.requests == []


def broken_redis() -> fakeredis.FakeAsyncRedis:
    server = fakeredis.FakeServer()
    server.connected = False
    return fakeredis.FakeAsyncRedis(server=server)


@pytest.mark.parametrize("broken", [False, True], ids=["no_entry", "redis_down"])
async def test_without_a_cached_opener_it_goes_through_the_socket(*, broken: bool) -> None:
    async with rig(redis=broken_redis() if broken else None) as r:
        r.scenario("wait_agent_speech: {}", "hangup: {}")
        async with r.call(opener_text=PRERENDERED, first_sentence=PRERENDERED, wait_for_greeting=False):
            end = await r.body.end()
        assert end == STREAM_CLOSED
        assert socket_texts(r.state) == [PRERENDERED]


@pytest.mark.integration
async def test_a_conversation_leaves_ordered_turns_and_call_events(media_db_url: str) -> None:
    async with open_database(media_db_url) as database:
        call = await seed_call(database, status="in_progress", config=resolved_config())
        async with (
            open_persistence(database, fakeredis.FakeAsyncRedis(), clock=SystemClock()) as persistence,
            rig(persistence=persistence) as r,
        ):
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
            async with r.call(seeded=call):
                assert await r.body.end() == STREAM_CLOSED
            # The call's turns and event rows were flushed when its pipeline ended, before the writers close.
            async with database.tenant_session(call.org_id) as session:
                turns = await CallTurnRepo(session, call.org_id).list_for_call(call.call_id)
                events = list(
                    await session.scalars(
                        select(CallEvent).where(CallEvent.call_id == call.call_id).order_by(CallEvent.seq)
                    )
                )
    assert [(t.turn_index, t.role) for t in turns] == [(0, "assistant"), (1, "user"), (2, "assistant")]
    assert turns[0].text_ == OPENER
    assert "book a table" in turns[1].text_.lower()
    assert turns[2].text_ == "Sure, for how many people?"
    assert turns[1].latency.get("stt_final_ms") is not None
    assert all(t.org_id == call.org_id for t in turns)
    assert turns[0].start_ms <= turns[1].start_ms <= turns[2].start_ms
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert all(e.org_id == call.org_id for e in events)
    turn_events = [(e.data["turn_index"], e.data["role"]) for e in events if e.category == "call"]
    assert turn_events == [(0, "assistant"), (1, "user"), (2, "assistant")]
    latency = [e for e in events if e.category == "latency"]
    assert latency
    assert {e.data["turn_index"] for e in latency} == {1}


@asynccontextmanager
async def ending_rig(
    media_db_url: str, *, status: str = "in_progress"
) -> AsyncIterator[tuple[Rig, SeededCall]]:
    """A rig with the end-of-call routine on the media database and a seeded answered call holding a slot."""
    async with open_database(media_db_url) as database:
        call = await seed_call(database, status="in_progress", answered_at=utcnow(), config=resolved_config())
        async with (
            open_persistence(database, fakeredis.FakeAsyncRedis(), clock=SystemClock()) as persistence,
            rig(persistence=persistence, database=database) as r,
        ):
            await r.container.redis.zadd(concurrency_key(call.org_id), {str(call.call_id): 1})
            yield r, call


async def signalled(r: Rig) -> list[tuple[str, str]]:
    async with asyncio.timeout(WAIT_S):
        await r.signaller.signalled.wait()
    return [(f.end_reason, f.ended_by) for _, f in r.signaller.signals]


def hangups(streamed: StreamedCall) -> list[str | None]:
    return [c.command_id for c in streamed.sim_call().commands if c.name == "hangup"]


async def ended_row(media_db_url: str, call: SeededCall) -> tuple[Call, list[dict[str, Any]]]:
    async with open_database(media_db_url) as database:
        row = await load_call(database, call)
        async with database.tenant_session(call.org_id) as session:
            ends = [
                dict(e.data)
                for e in await session.scalars(select(CallEvent).where(CallEvent.call_id == call.call_id))
                if e.message == ENDED_MESSAGE
            ]
    return row, ends


@pytest.mark.integration
async def test_an_agent_end_call_is_recorded_hung_up_and_signalled(media_db_url: str) -> None:
    async with ending_rig(media_db_url) as (r, call):
        await add_rules(
            r.llm,
            "rules:\n  - id: bye\n    match: {last_user_regex: 'bye'}\n"
            "    respond: {tool_calls: [{name: end_call,\n"
            "      arguments: {reason: done, final_message: 'Goodbye now.'}}]}\n",
        )
        r.scenario("wait_agent_speech: {}", "say: {text: that is all thanks bye}", "expect_hangup: {}")
        async with r.call(seeded=call) as streamed:
            assert await r.body.end() == SessionEnd("ended", "agent_end_call", "agent")
            assert await signalled(r) == [("agent_end_call", "agent")]
            assert hangups(streamed) == [command_id("hangup", call.call_id, "primary")]
        assert await r.container.redis.zscore(concurrency_key(call.org_id), str(call.call_id)) is None
    row, ends = await ended_row(media_db_url, call)
    assert (row.status, row.end_reason, row.ended_by) == ("completed", "agent_end_call", "agent")
    assert row.connected_seconds is not None
    assert row.connected_seconds > 0
    assert ends == [{"end_reason": "agent_end_call", "ended_by": "agent"}]


@pytest.mark.integration
async def test_a_caller_hangup_is_recorded_without_a_media_hangup(media_db_url: str) -> None:
    async with ending_rig(media_db_url) as (r, call):
        # What the edge stores for the carrier's call.hangup (before it publishes it).
        await r.container.redis.hset(carrier_state_key(call.call_id), mapping={"primary_hangup": "1"})
        r.scenario("wait_agent_speech: {}", "hangup: {}")
        async with r.call(seeded=call) as streamed:
            assert await r.body.end() == STREAM_CLOSED
            assert await signalled(r) == [("user_hangup", "user")]
            assert hangups(streamed) == []
    row, ends = await ended_row(media_db_url, call)
    assert (row.status, row.end_reason, row.ended_by) == ("completed", "user_hangup", "user")
    assert ends == [{"end_reason": "user_hangup", "ended_by": "user"}]


@pytest.mark.integration
async def test_a_stop_control_message_ends_the_call_as_api_stop(media_db_url: str) -> None:
    async with ending_rig(media_db_url) as (r, call):
        r.scenario(
            "wait_agent_speech: {}", "say: {text: tell me more about the menu please}", "expect_hangup: {}"
        )
        async with r.call(seeded=call) as streamed:
            async with asyncio.timeout(WAIT_S):
                await r.body.started.wait()
            stop = StopControl(id=uuid.uuid4())
            await r.container.redis.publish(control_channel(call.call_id), stop.model_dump_json())
            assert await signalled(r) == [("api_stop", "api")]
            assert hangups(streamed) == [command_id("hangup", call.call_id, "primary")]
    row, _ = await ended_row(media_db_url, call)
    assert (row.status, row.end_reason, row.ended_by) == ("completed", "api_stop", "api")
