"""The media :class:`~va_media.runtime.CallRuntime` over the simulators (wall clock): FakeElevenLabs for
STT/TTS, FakeLLM through the LLM gateway, and FakeTelnyx (in-process) for outbound DTMF, the voicemail SMS and
the end-of-call hangup. Shared by the runtime sim tests and the in-process inbound flow."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import fakeredis
import httpx
from media_helpers import RecordingSignaller
from redis.asyncio import Redis
from service_helpers import settings_of
from starlette.applications import Starlette
from telnyx_client_helpers import sms_number
from telnyx_helpers import VOICE_URL

from va_common.http import http_client
from va_common.metrics import Service, build_metrics, new_registry
from va_common.time import SystemClock
from va_db.session import Database
from va_domain.org import OrgSettings
from va_llm import LlmGateway, LlmMetrics, ProviderRegistry
from va_llm.providers import platform_provider
from va_llm.tokens import default_counter
from va_media.call_end import CallEnd
from va_media.runtime import LLM_HTTP_TIMEOUT, CallRuntime, OrgSettingsSource
from va_media.session import CallSession, SessionEnd
from va_media.settings import MediaSettings
from va_media.turns import CallPersistence
from va_sim.server import SimName, simulator_routes
from va_sim.state import SimState
from va_sim.telnyx.store import DEFAULT_API_KEY
from va_telnyx import TelnyxClientFactory
from va_telnyx.settings import telnyx_factory
from va_voice.vad import VadExecutor
from va_voice.vad_model import SileroModel

__all__ = [
    "MODELS_DIR",
    "WAIT_S",
    "RecordingRuntime",
    "SimRuntime",
    "StaticOrgSettings",
    "sim_runtime",
]

MODELS_DIR: Final = Path(__file__).resolve().parents[4] / "models"
"""The real Silero model (``make setup``)."""

WAIT_S: Final = 20.0


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
class SimRuntime:
    body: RecordingRuntime
    settings: MediaSettings
    telnyx: TelnyxClientFactory
    """FakeTelnyx's platform account (in-process), the runtime's and the end routine's Telnyx."""
    signaller: RecordingSignaller
    sms_number: str | None


async def _no_byo_key(_account_id: uuid.UUID) -> str | None:
    return None


@asynccontextmanager
async def sim_runtime(  # noqa: PLR0913 - keyword-only collaborators of the runtime
    state: SimState,
    urls: Mapping[SimName, str],
    *,
    org_settings: OrgSettingsSource,
    phrase_redis: Redis,
    persistence: CallPersistence | None = None,
    database: Database | None = None,
    end_redis: Redis | None = None,
    messaging: bool = False,
) -> AsyncIterator[SimRuntime]:
    """The runtime over ``state`` (FakeTelnyx) and the served FakeElevenLabs/FakeLLM ``urls``. With
    ``database`` its end-of-call routine runs too (over ``end_redis``, FakeTelnyx, ``persistence``'s events
    and a :class:`RecordingSignaller`). ``messaging``: FakeTelnyx's platform account gets a messaging profile
    (the runtime's ``TELNYX_MESSAGING_PROFILE_ID``) and an SMS number on it (``SimRuntime.sms_number``)."""
    settings = settings_of(
        MediaSettings,
        elevenlabs_ws_base=urls["elevenlabs"].replace("http://", "ws://"),
        llm_default_base_url=f"{urls['llm']}/v1",
        telnyx_api_base="http://sim/v2",
        telnyx_api_key=DEFAULT_API_KEY,
    )
    metrics = build_metrics(Service.MEDIA_WORKER, new_registry(process_collectors=False))
    clock = SystemClock()
    telnyx_app = Starlette(routes=simulator_routes("telnyx", state))
    async with (
        http_client(settings, timeout=LLM_HTTP_TIMEOUT) as http,
        VadExecutor(1) as executor,
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
            resolve_key=_no_byo_key,
            clock=clock,
        )
        sms = None
        if messaging:
            sms, profile_id = await sms_number(await telnyx.for_account(None), state, VOICE_URL, VOICE_URL)
            settings = settings.model_copy(update={"telnyx_messaging_profile_id": profile_id})
        signaller = RecordingSignaller()
        end_routine = None
        if database is not None:
            end_routine = CallEnd(
                database=database,
                redis=end_redis if end_redis is not None else fakeredis.FakeAsyncRedis(),
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
            database=database,
        )
        yield SimRuntime(RecordingRuntime(runtime), settings, telnyx, signaller, sms)
