"""The call runtime end to end on the simulators (wall clock): FakeTelnyx streams a scripted call to a served
media worker whose body is :class:`CallRuntime` over FakeElevenLabs (STT/TTS), FakeLLM, and FakeTelnyx for
outbound DTMF (specs/07 §16 Decision P05-T10a, §11 Decisions P05-T10b and P05-T10d)."""

from __future__ import annotations

import asyncio
import io
import math
import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

import fakeredis
import httpx
import numpy as np
import pytest
from call_rows import SeededCall, load_call, seed_call
from llm_helpers import add_rules
from media_helpers import FakeContextLoader, FakeMediaContainer, RecordingSignaller, call_context
from resolved_configs import resolved_config
from runtime_rig import MODELS_DIR, WAIT_S, RecordingRuntime, StaticOrgSettings, sim_runtime
from service_helpers import json_log_stream, log_events
from sim_media import EdgeRelay, StreamedCall, realtime_telnyx, streamed_call
from sms_rows import add_sms_number
from sqlalchemy import select
from telnyx_helpers import CALLEE, register
from tenant_rows import open_database
from tts_helpers import VOICE_ID

import va_media.runtime
from va_billing.keys import concurrency_key
from va_common.metrics import ServiceMetrics
from va_common.time import SystemClock, utcnow
from va_db.models import Call, CallEvent, Message, WebhookDelivery
from va_db.repositories import CallTurnRepo
from va_db.session import Database
from va_domain.carrier import carrier_state_key
from va_domain.control import StopControl, control_channel
from va_domain.org import OrgSettings
from va_elevenlabs import ElevenLabsRest
from va_elevenlabs.tts_models import DEFAULT_TTS_MODEL
from va_engine.opener import cache_text, phrase_format, phrase_voice
from va_events import ENDED_MESSAGE
from va_media.runtime import open_persistence
from va_media.runtime_config import ERROR_END
from va_media.session import STREAM_CLOSED, SessionEnd
from va_media.turns import CallPersistence
from va_media.voicemail_sms import SMS_SENT_MESSAGE
from va_sim.server import running_sims
from va_sim.state import SimState
from va_sim.telnyx.store import DEFAULT_API_KEY
from va_telnyx.client_state import command_id
from va_voice.audio import rms_dbfs
from va_voice.phrase_cache import PhraseCache
from va_voice.pipeline import CallPipeline
from va_workflows.activities.prerender import OpenerPrerender

OPENER: Final = "Hi, this is Maya."


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
    relay: EdgeRelay
    sms_number: str | None = None
    metrics: ServiceMetrics | None = None

    @asynccontextmanager
    async def call(
        self,
        *,
        answered: bool = True,
        opener_text: str | None = None,
        seeded: SeededCall | None = None,
        amd: str | None = None,
        maintenance_message: str | None = None,
        **config: Any,
    ) -> AsyncIterator[StreamedCall]:
        """A streamed call; the carrier state says ``answered`` unless ``answered=False`` (no 2 s wait);
        ``seeded``: use that call's id and org (a call row on a real database). Without ``amd`` the carrier
        state already holds the premium verdict ``answered_by=human`` (no AMD wait); with ``amd`` (e.g.
        ``premium``) the dial requests it and FakeTelnyx's verdict and beep reach the worker through the
        :class:`EdgeRelay`. ``maintenance_message``: an inbound call to a number in maintenance (the context
        carries the message the edge stored)."""
        values: dict[str, Any] = {"provider_voice_id": VOICE_ID, "first_sentence": OPENER, **config}
        loader = FakeContextLoader(**values)
        loader.opener_text = opener_text
        if maintenance_message is not None:
            loader.maintenance_message = maintenance_message
            loader.direction = "inbound"
        call_id = None
        if seeded is not None:
            call_id = seeded.call_id
            loader.contexts[call_id] = call_context(
                call_id,
                org_id=seeded.org_id,
                maintenance_message=loader.maintenance_message,
                direction=loader.direction,
                **values,
            )
        async with streamed_call(
            MODELS_DIR,
            self.body,
            state=self.state,
            loader=loader,
            answered=answered,
            call_id=call_id,
            container=self.container,
            carrier=None if amd is not None else {"answered_by": "human"},
            relay=self.relay,
            **({} if amd is None else {"answering_machine_detection": amd}),
        ) as streamed:
            yield streamed

    def scenario(self, *steps: str, answer: str = "{after_ms: 200}") -> None:
        text = f'id: rt-{secrets.token_hex(4)}\nmatch: {{to: "{CALLEE}"}}\nanswer: {answer}\n'
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


@asynccontextmanager
async def rig(
    org: OrgSettings | None = None,
    *,
    redis: fakeredis.FakeAsyncRedis | None = None,
    persistence: CallPersistence | None = None,
    database: Database | None = None,
    messaging: bool = False,
) -> AsyncIterator[Rig]:
    """The runtime over the simulators (:func:`runtime_rig.sim_runtime`); with ``database`` its end-of-call
    routine runs too, over the media container's Redis. ``messaging``: see :func:`sim_runtime`."""
    relay = EdgeRelay()
    state = realtime_telnyx(relay)
    async with running_sims(state, ports={"elevenlabs": 0, "llm": 0}) as urls:
        org_settings = StaticOrgSettings(org if org is not None else OrgSettings())
        phrase_redis = redis if redis is not None else fakeredis.FakeAsyncRedis()
        container = FakeMediaContainer()
        async with (
            httpx.AsyncClient(base_url=urls["llm"]) as llm,
            sim_runtime(
                state,
                urls,
                org_settings=org_settings,
                phrase_redis=phrase_redis,
                persistence=persistence,
                database=database,
                end_redis=container.redis,
                messaging=messaging,
            ) as parts,
        ):
            yield Rig(
                state,
                llm,
                parts.body,
                org_settings,
                phrase_redis,
                urls["elevenlabs"],
                container,
                parts.signaller,
                relay,
                parts.sms_number,
                parts.metrics,
            )


def spoken(state: SimState) -> list[str]:
    """The text FakeElevenLabs generated per TTS context, in order (the socket's own record has none)."""
    texts = [request.generated_text.strip() for request in state.elevenlabs.tts_requests]
    return [text for text in texts if text]


async def test_caller_turn_is_answered_by_the_task_brain(monkeypatch: pytest.MonkeyPatch) -> None:
    hooks: list[Callable[[], None]] = []

    def recording_pipeline(**kwargs: Any) -> CallPipeline:
        hooks.append(kwargs["on_underrun"])
        return CallPipeline(**kwargs)

    monkeypatch.setattr(va_media.runtime, "CallPipeline", recording_pipeline)
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
        assert r.metrics is not None
        registry = r.metrics.registry
        # The wall-clock sim may underrun under load; on-time playback reporting none is covered on the
        # accelerated clock (va_voice test_pipeline_underruns). Here: the hook drives the catalog counter.
        before = registry.get_sample_value("va_audio_underruns_total") or 0.0
        assert len(hooks) == 1
        hooks[0]()
        assert registry.get_sample_value("va_audio_underruns_total") == before + 1


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


async def test_unsupported_runner_is_refused_without_vendor_sockets() -> None:
    async with rig() as r:
        r.scenario("silence: {ms: 60000}")
        async with r.call(runner_type="persona", task=None, persona_id=str(uuid.uuid4()), persona_version=1):
            end = await r.body.end()
        assert end == ERROR_END
        assert r.org_settings.reads == []
        assert r.state.elevenlabs.stt.sessions == []


async def test_a_pathway_call_without_pathway_support_is_refused_without_vendor_sockets() -> None:
    stream = json_log_stream()
    async with rig() as r:
        r.scenario("silence: {ms: 60000}")
        async with r.call(runner_type="pathway", task=None, pathway_id=str(uuid.uuid4()), pathway_version=1):
            end = await r.body.end()
        assert end == ERROR_END
        assert r.state.elevenlabs.stt.sessions == []
    reasons = [e["reason"] for e in log_events(stream) if e["event"] == "media.pathway_unavailable"]
    assert reasons == ["not_configured"]


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
        # The caller hangs up only after one continuous agent turn of 1.5 s (the opener is ~3.5 s of speech):
        # an outbound stall under CPU load restarts the turn instead of ending the call mid-opener.
        r.scenario("wait_agent_speech: {min_ms: 1500, then_silence_ms: 700, timeout_ms: 20000}", "hangup: {}")
        async with r.call(
            opener_text=opener, first_sentence=PRERENDERED, wait_for_greeting=False
        ) as streamed:
            end = await r.body.end()
            agent_audio = bytes(streamed.sim_call().audio.agent)
        assert end == STREAM_CLOSED
        # The caller heard the cached opener (at least 1.5 s of PCM16 8 kHz agent speech, not silence).
        assert len(agent_audio) > 24_000
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
    webhook = "https://calls.hooks.test/live"
    config = {**resolved_config(), "webhook": webhook, "webhook_events": ["call", "latency"]}
    async with open_database(media_db_url) as database:
        call = await seed_call(database, status="in_progress", config=config)
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
                deliveries = list(
                    await session.scalars(
                        select(WebhookDelivery).where(WebhookDelivery.call_id == call.call_id)
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
    assert (events[0].event_type, events[0].message) == ("call.started", "Call started")
    assert events[0].data == {"direction": "outbound", "media_format": "PCMU/8000"}
    turn_events = [(e.data["turn_index"], e.data["role"]) for e in events if e.event_type == "call.turn"]
    assert turn_events == [(0, "assistant"), (1, "user"), (2, "assistant")]
    latency = [e for e in events if e.category == "latency"]
    assert latency
    assert {e.data["turn_index"] for e in latency} == {1}
    # Every flushed row was routed in its flush transaction (specs/19 §1 step 5, Decision P06-T07).
    routed = {e.event_type for e in events} - {"call.interrupt"}
    assert {d.event_type for d in deliveries} == routed
    assert {"call.started", "call.turn", "call.latency"} <= routed
    assert {(d.url, d.format) for d in deliveries} == {(webhook, "bland")}
    assert sorted(d.event_id for d in deliveries) == sorted(
        f"evt_{e.id.hex}" for e in events if e.event_type in routed
    )


@asynccontextmanager
async def ending_rig(
    media_db_url: str, *, status: str = "in_progress", messaging: bool = False
) -> AsyncIterator[tuple[Rig, SeededCall]]:
    """A rig with the end-of-call routine on the media database and a seeded answered call holding a slot."""
    async with open_database(media_db_url) as database:
        call = await seed_call(database, status="in_progress", answered_at=utcnow(), config=resolved_config())
        async with (
            open_persistence(database, fakeredis.FakeAsyncRedis(), clock=SystemClock()) as persistence,
            rig(persistence=persistence, database=database, messaging=messaging) as r,
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
        # No goodbye line (specs/05 §7.2): at most (part of) the opener was ever synthesized.
        assert all(OPENER.startswith(text) for text in spoken(r.state))
    row, ends = await ended_row(media_db_url, call)
    assert (row.status, row.end_reason, row.ended_by) == ("completed", "api_stop", "api")
    assert ends == [{"end_reason": "api_stop", "ended_by": "api"}]


# AMD and voicemail (specs/07 §14 Decision P05-T10c)

GREETING: Final = (
    "Hi, you have reached Dana at the front desk. I cannot take your call right now, so please leave "
    "your name, your number and a short message and I will get back to you as soon as I possibly can"
)
VOICEMAIL_TEMPLATE: Final = "Hi {{name}}, please call us back."
BEEP_MS: Final = 4000
_PCM_BYTES_PER_MS: Final = 16


def first_audio_ms(agent: bytes) -> float:
    """Offset (ms after the answer) of the first non-silent agent sample in the aligned 8 kHz PCM16 track."""
    for index in range(0, len(agent) - 1, 2):
        if agent[index : index + 2] != b"\x00\x00":
            return index / _PCM_BYTES_PER_MS
    message = "the agent never spoke"
    raise AssertionError(message)


def voicemail_events(rows: list[CallEvent]) -> list[dict[str, Any]]:
    return [dict(row.data) for row in rows if row.message == "Voicemail detected"]


async def call_event_rows(media_db_url: str, call: SeededCall) -> list[CallEvent]:
    async with open_database(media_db_url) as database, database.tenant_session(call.org_id) as session:
        return list(await session.scalars(select(CallEvent).where(CallEvent.call_id == call.call_id)))


@pytest.mark.integration
async def test_a_machine_gets_the_message_after_the_beep(media_db_url: str) -> None:
    async with ending_rig(media_db_url) as (r, call):
        voicemail = {"action": "leave_message", "message": VOICEMAIL_TEMPLATE, "detect_timeout_ms": 10_000}
        async with machine_call(r, call, voicemail) as streamed:
            agent_audio = bytes(streamed.sim_call().audio.agent)
            assert hangups(streamed) == [command_id("hangup", call.call_id, "primary")]
        assert r.relay.relayed[:2] == [
            "call.machine.premium.detection.ended",
            "call.machine.premium.greeting.ended",
        ]
        # Nothing was said before the beep; then only the rendered message, and no LLM was asked.
        assert BEEP_MS <= first_audio_ms(agent_audio) <= BEEP_MS + 2000
        assert spoken(r.state) == ["Hi Dana, please call us back."]
        assert r.state.llm.requests == []
        row, _ = await ended_row(media_db_url, call)
        assert (row.end_reason, row.ended_by, row.disposition_tag) == ("voicemail_left", "agent", "VOICEMAIL")
        assert voicemail_events(await call_event_rows(media_db_url, call)) == [
            {"action": "leave_message", "trigger": "amd"}
        ]


@asynccontextmanager
async def machine_call(r: Rig, call: SeededCall, voicemail: dict[str, Any]) -> AsyncIterator[StreamedCall]:
    """An AMD call answered by a machine that beeps at :data:`BEEP_MS`; yields once the call ended
    ``voicemail_left`` and ``CallWorkflow`` was signalled (the sim call is still readable)."""
    r.scenario(
        f"say: {{text: '{GREETING}'}}",
        "expect_hangup: {}",
        answer=f"{{after_ms: 200, answered_by: machine, beep_at_ms: {BEEP_MS}}}",
    )
    # No background bed: the tests read "nothing before the beep" from digital silence on the agent track.
    async with r.call(
        seeded=call,
        amd="premium",
        voicemail=voicemail,
        request_data={"name": "Dana"},
        background_track="none",
    ) as streamed:
        assert (await r.body.end()).end_reason == "voicemail_left"
        assert await signalled(r) == [("voicemail_left", "agent")]
        yield streamed


async def test_a_machine_with_hangup_ends_without_a_word() -> None:
    async with rig() as r:
        r.scenario(
            f"say: {{text: '{GREETING}'}}",
            "expect_hangup: {}",
            answer="{after_ms: 200, answered_by: machine}",
        )
        async with r.call(amd="premium", voicemail={"action": "hangup"}):
            end = await r.body.end()
        assert (end.end_reason, end.ended_by) == ("voicemail_hangup", "system")
        assert spoken(r.state) == []
        assert r.state.llm.requests == []


async def test_ivr_mode_ignores_the_machine_verdict() -> None:
    async with rig() as r:
        r.scenario("wait_agent_speech: {}", "hangup: {}", answer="{after_ms: 200, answered_by: machine}")
        async with r.call(amd="premium", ivr_mode=True, voicemail={"action": "hangup"}):
            end = await r.body.end()
        assert end == STREAM_CLOSED
        assert spoken(r.state)[0] == OPENER


@pytest.mark.integration
async def test_the_sensitive_classifier_detects_a_voicemail_greeting(media_db_url: str) -> None:
    async with ending_rig(media_db_url) as (r, call):
        r.scenario(
            "wait_agent_speech: {}",
            "say: {text: Please leave a message after the tone}",
            "expect_hangup: {}",
        )
        voicemail = {"action": "hangup", "sensitive": True, "detect_timeout_ms": 30_000}
        async with r.call(seeded=call, amd="premium", voicemail=voicemail):
            end = await r.body.end()
            assert await signalled(r) == [("voicemail_hangup", "system")]
        assert end.end_reason == "voicemail_hangup"
        classified = [req for req in r.state.llm.requests if "classify_answer" in str(req.tool_choice)]
        assert len(classified) == 1
        row, _ = await ended_row(media_db_url, call)
        assert (row.answered_by, row.disposition_tag) == ("voicemail", "VOICEMAIL")
        [data] = voicemail_events(await call_event_rows(media_db_url, call))
        assert (data["action"], data["trigger"]) == ("hangup", "classifier")
        assert data["confidence"] >= 0.8


# Voicemail SMS (specs/07 §14 Decision P05-T10e)

SMS_TEMPLATE: Final = "Hi {{name}}, sorry we missed you. Reply here or call us back."


async def org_sms_number(media_db_url: str, call: SeededCall, e164: str | None = None) -> str:
    async with open_database(media_db_url) as database:
        return await add_sms_number(database, call.org_id, e164)


async def sms_rows(media_db_url: str, call: SeededCall) -> list[Message]:
    async with open_database(media_db_url) as database, database.tenant_session(call.org_id) as session:
        return list(await session.scalars(select(Message).where(Message.org_id == call.org_id)))


def last_audio_ms(agent: bytes) -> float:
    """Offset (ms after the answer) of the end of the last non-silent agent sample."""
    for index in range(len(agent) - 2, -1, -2):
        if agent[index : index + 2] != b"\x00\x00":
            return (index + 2) / _PCM_BYTES_PER_MS
    message = "the agent never spoke"
    raise AssertionError(message)


def voicemail_sms_call(sender: str) -> dict[str, Any]:
    return {
        "action": "leave_message_and_sms",
        "message": VOICEMAIL_TEMPLATE,
        "detect_timeout_ms": 10_000,
        "sms": {"message": SMS_TEMPLATE, "from": sender},
    }


@pytest.mark.integration
async def test_the_voicemail_sms_goes_out_between_the_message_and_the_hangup(media_db_url: str) -> None:
    async with ending_rig(media_db_url, messaging=True) as (r, call):
        assert r.sms_number is not None
        await org_sms_number(media_db_url, call, r.sms_number)
        async with machine_call(r, call, voicemail_sms_call(r.sms_number)) as streamed:
            sim_call = streamed.sim_call()
            agent_audio = bytes(sim_call.audio.agent)
            answered_at = sim_call.answered_at
            hangup_at = [c.at for c in sim_call.commands if c.name == "hangup"]
        assert spoken(r.state) == ["Hi Dana, please call us back."]
        [record] = r.state.telnyx.accounts[DEFAULT_API_KEY].messages.values()
        assert record["text"] == "Hi Dana, sorry we missed you. Reply here or call us back."
        assert record["from"]["phone_number"] == r.sms_number
        # The SMS goes to the call context's callee number.
        assert record["to"][0]["phone_number"] == call_context(call.call_id).to_number
        # Sent after the last frame of the message played, before the hangup.
        assert answered_at is not None
        sent_at = datetime.fromisoformat(record["received_at"])
        assert sent_at >= answered_at + timedelta(milliseconds=last_audio_ms(agent_audio) - 100)
        [hangup] = hangup_at
        assert sent_at <= datetime.fromisoformat(hangup)
        [row] = await sms_rows(media_db_url, call)
        assert (row.telnyx_message_id, row.direction, row.body) == (record["id"], "outbound", record["text"])
        events = [e for e in await call_event_rows(media_db_url, call) if e.message == SMS_SENT_MESSAGE]
        assert [dict(e.data)["message_id"] for e in events] == [str(row.id)]
        ended, _ = await ended_row(media_db_url, call)
        assert (ended.end_reason, ended.ended_by) == ("voicemail_left", "agent")


@pytest.mark.integration
async def test_a_rejected_voicemail_sms_still_leaves_the_message_and_hangs_up(media_db_url: str) -> None:
    async with ending_rig(media_db_url, messaging=True) as (r, call):
        # An SMS number of the org that FakeTelnyx's account does not own: Telnyx refuses the send.
        sender = await org_sms_number(media_db_url, call)
        async with machine_call(r, call, voicemail_sms_call(sender)) as streamed:
            assert hangups(streamed) == [command_id("hangup", call.call_id, "primary")]
        assert r.state.telnyx.accounts[DEFAULT_API_KEY].messages == {}
        [row] = await sms_rows(media_db_url, call)
        assert row.status == "sending_failed"
        assert row.telnyx_message_id is None
        rows = await call_event_rows(media_db_url, call)
        assert [e for e in rows if e.message == SMS_SENT_MESSAGE] == []


# Maintenance message (specs/16 §4 Decision P05-T12d)

MAINTENANCE: Final = "We are closed for maintenance today. Please call back tomorrow."


@pytest.mark.integration
async def test_a_maintenance_message_is_spoken_and_the_call_ends_inbound_rejected(media_db_url: str) -> None:
    async with ending_rig(media_db_url) as (r, call):
        r.scenario("expect_hangup: {}")
        async with r.call(seeded=call, maintenance_message=MAINTENANCE) as streamed:
            assert await r.body.end() == SessionEnd("ended", "inbound_rejected", "system")
            assert await signalled(r) == [("inbound_rejected", "system")]
            assert hangups(streamed) == [command_id("hangup", call.call_id, "primary")]
        # Only the message was spoken (no opener) and the Brain was never asked.
        assert spoken(r.state) == [MAINTENANCE]
        assert r.state.llm.requests == []
    row, ends = await ended_row(media_db_url, call)
    assert (row.status, row.end_reason, row.ended_by) == ("completed", "inbound_rejected", "system")
    assert ends == [{"end_reason": "inbound_rejected", "ended_by": "system"}]


async def test_a_cached_maintenance_message_plays_from_the_phrase_cache() -> None:
    async with rig() as r:
        context = call_context(uuid.uuid4(), provider_voice_id=VOICE_ID)
        async with httpx.AsyncClient() as http:
            rest = ElevenLabsRest("sim-eleven-key", r.elevenlabs_url, http, clock=SystemClock())
            cache = PhraseCache(rest, r.redis)
            await cache.get_or_render(
                phrase_voice(context, model_id=DEFAULT_TTS_MODEL),
                cache_text(context, MAINTENANCE),
                phrase_format(context),
            )
            await cache.aclose()
        r.scenario("expect_hangup: {}")
        async with r.call(maintenance_message=MAINTENANCE) as streamed:
            end = await r.body.end()
            agent_audio = bytes(streamed.sim_call().audio.agent)
        assert end == SessionEnd("ended", "inbound_rejected", "system")
        assert len(agent_audio) > 16_000
        assert any(agent_audio)
        assert socket_texts(r.state) == []
        assert r.state.llm.requests == []


# Call audio options (specs/07 §10 Decision P05-T15).

LONG_OPENER: Final = (
    "Hi, this is Maya from Bella Pasta, thanks for picking up today. I am calling about the reservation "
    "request you sent us for this weekend, and I would like to confirm a few details with you before we "
    "hold the table."
)
LONG_REPLY: Final = (
    "Of course, I can help with that. Let me pull up the booking we have on file for you and walk through "
    "the date, the time and the number of guests one by one so that nothing gets missed along the way."
)


def window_dbfs(agent: bytes, start_ms: int, end_ms: int) -> float:
    """Level of the aligned 8 kHz agent track between two offsets after the answer."""
    window = agent[start_ms * _PCM_BYTES_PER_MS : end_ms * _PCM_BYTES_PER_MS]
    assert len(window) == (end_ms - start_ms) * _PCM_BYTES_PER_MS
    return rms_dbfs(np.frombuffer(window, dtype="<i2"))


def media_logs(stream: io.StringIO, event: str) -> list[dict[str, Any]]:
    return [line for line in log_events(stream) if line.get("event") == event]


@pytest.mark.parametrize(
    ("track", "noise", "level"),
    [("office", True, -22.0), (None, True, -40.0), ("none", False, None)],
)
async def test_the_background_bed_fills_the_agent_track_between_utterances(
    track: str | None, noise: bool, level: float | None
) -> None:
    async with rig() as r:
        r.scenario("silence: {ms: 4000}", "hangup: {}")
        async with r.call(
            background_track=track, noise_cancellation=noise, wait_for_greeting=False
        ) as streamed:
            assert await r.body.end() == STREAM_CLOSED
            call = streamed.sim_call()
            agent = bytes(call.audio.agent)
            echoes = list(call.mark_echoes)
        assert spoken(r.state) == [OPENER]
        # The opener's last mark echoes when its audio has played out (the caller stays silent, so nothing
        # else is spoken): the idle window starts there, wherever a loaded host pushed the opener.
        assert echoes, "the opener's marks were never echoed"
        opener_end_ms = echoes[-1].track_ms
        idle = window_dbfs(agent, opener_end_ms + 200, opener_end_ms + 1000)
        if level is None:
            assert idle == -math.inf
        else:
            assert idle == pytest.approx(level, abs=3.0)
        [session] = r.state.elevenlabs.stt.sessions
        assert session.filter_background_audio is noise


async def test_barge_in_clears_playback_and_keeps_only_the_spoken_opener() -> None:
    stream = json_log_stream()
    async with rig() as r:
        await add_rules(
            r.llm,
            "rules:\n  - id: table\n    match: {last_user_regex: 'table'}\n"
            "    respond: {content: 'Sure, for how many people?'}\n",
        )
        r.scenario(
            "say: {text: I would like to book a table please, barge_in_after_agent_ms: 1200}",
            "wait_agent_speech: {}",
            "hangup: {}",
        )
        async with r.call(first_sentence=LONG_OPENER, temperature=0.2):
            assert await r.body.end() == STREAM_CLOSED
        [request] = r.state.llm.requests
    assert request.temperature == 0.2
    [barge_in] = media_logs(stream, "pipeline_barge_in")
    assert 0 < barge_in["played_chars"] < barge_in["total_chars"]
    [closed] = media_logs(stream, "media.transport_closed")
    assert closed["clears"] >= 1
    history = [m for m in request.messages if isinstance(m, dict) and m.get("role") == "assistant"]
    spoken_opener = str(history[0]["content"])
    assert spoken_opener[:20] == LONG_OPENER[:20]
    assert len(spoken_opener) < len(LONG_OPENER)
    assert spoken(r.state)[-1] == "Sure, for how many people?"


@pytest.mark.parametrize(("level", "barge_ins"), [(0, 0), (2, 1), (4, 2)])
async def test_interruptibility_levels_decide_which_caller_speech_interrupts(
    level: int, barge_ins: int
) -> None:
    stream = json_log_stream()
    async with rig() as r:
        await add_rules(
            r.llm,
            "rules:\n  - id: any\n    match: {last_user_regex: '.'}\n"
            f"    respond: {{content: '{LONG_REPLY}'}}\n",
        )
        r.scenario(
            "say: {text: uh huh, barge_in_after_agent_ms: 1000}",
            "say: {text: I need to change my booking for tomorrow night, barge_in_after_agent_ms: 1000}",
            "silence: {ms: 1500}",
            "hangup: {}",
        )
        async with r.call(first_sentence=LONG_OPENER, interruptibility=level):
            assert await r.body.end() == STREAM_CLOSED
    assert len(media_logs(stream, "pipeline_barge_in")) == barge_ins
    [closed] = media_logs(stream, "media.transport_closed")
    assert (closed["clears"] > 0) is (barge_ins > 0)
