"""The call runtime: the session body that runs the pipeline of every attached call
(specs/07-media-pipeline.md §1, §16; specs/07 §16 Decision P05-T10a).

:func:`open_runtime` builds the per-process collaborators once (the LLM gateway over one HTTP client with the
org provider store behind the envelope, the org settings reader, the token counter, the Silero model and the
VAD thread pool) and yields a :class:`CallRuntime`. For each call :meth:`CallRuntime.__call__`:

1. refuses runners other than ``task`` (pathway/persona/agent Brains arrive in later phases) and calls whose
   org is gone, without opening a vendor socket;
2. wraps the session's socket in :class:`~va_media.transport.TelnyxMediaTransport` and maps the context onto
   the :class:`PipelineConfig` (``va_media.runtime_config``);
3. opens the realtime STT and TTS sockets concurrently (closed on every path, including cancellation by the
   session race); an open failure ends the call with ``error`` before any audio;
4. runs :class:`CallPipeline` with a per-call :class:`SileroVad` and :class:`TaskBrain` and maps its end
   reason onto the :class:`~va_media.session.SessionEnd`. The pipeline's ``before_opener`` is the
   :class:`~va_media.greeting.GreetingGate` (answered wait, precall DTMF), its action handler
   :class:`~va_media.actions.MediaActions` (``press_button``), both over the call's Telnyx client (platform
   or BYO account; an account that cannot be resolved is logged ``media.telnyx_unavailable`` and the call goes
   on without outbound DTMF); a :class:`~va_media.carrier_watch.CarrierWatch` task reads the carrier feed
   (Decision P05-T10b). With Redis, :class:`~va_media.phrases.CallPhrases` lets the Speaker play the opener
   pre-rendered at prefetch from the phrase cache instead of the TTS socket (Decision P05-T10d).
5. with :class:`~va_media.turns.CallPersistence` (built by :func:`open_persistence`), a
   :class:`~va_media.turns.TurnRecorder` observes the pipeline: ``call_turns`` rows through the write-behind
   :class:`~va_media.turn_writer.TurnWriter` and ``call.turn``/``call.interrupt``/``call.latency`` events
   through the per-process publisher and ``call_events`` buffer; when the pipeline ends the call's events are
   awaited and its turns and event rows flushed (specs/07 §8 Decision P05-T11a).
6. on outbound calls a :class:`~va_media.voicemail.VoicemailMonitor` acts on the AMD verdict and (with
   ``voicemail.sensitive``) on the ``classify_answer`` classifier: it takes the pipeline over for the
   voicemail action (hang up, or leave the message after the beep) and records ``call.voicemail`` /
   ``answered_by`` (specs/07 §14 Decision P05-T10c).
7. :attr:`CallRuntime.end_routine` (a :class:`~va_media.call_end.CallEnd` built by :func:`open_runtime`) is
   the session ender the lifespan hands to every session: it records the end, hangs up, emits ``call.ended``,
   releases the concurrency slot and signals ``CallWorkflow`` (specs/07 §18 Decision P05-T11b).
8. a call carrying a maintenance message (an inbound number in maintenance, accepted by the edge) is taken
   over before the pipeline runs (:func:`~va_media.maintenance.maintenance_takeover`): the opener is skipped,
   the message is spoken (from the phrase cache when present, else the TTS socket) and the call ends
   ``inbound_rejected`` without an LLM request (specs/16 §4 Decision P05-T12d).
"""

from __future__ import annotations

import asyncio
import functools
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

import httpx
from opentelemetry.sdk.trace import TracerProvider
from redis.asyncio import Redis
from sqlalchemy.exc import SQLAlchemyError

from va_common.checks import TemporalConnector
from va_common.crypto import CryptoError, Envelope, open_kms_provider
from va_common.http import http_client
from va_common.logging import get_logger
from va_common.metrics import ServiceMetrics
from va_common.time import Clock
from va_db.org_settings import OrgSettingsReader
from va_db.repositories import OrgDataKeyStore
from va_db.session import Database
from va_db.telnyx_keys import TelnyxKeyResolver
from va_domain.call_context import CallContext
from va_domain.org import OrgSettings
from va_elevenlabs import (
    CatalogSttMetrics,
    CatalogTtsMetrics,
    ElevenLabsRealtimeStt,
    SttConnection,
    SttError,
    TtsConfig,
    TtsError,
    TtsSocket,
)
from va_engine.opener import phrase_voice
from va_engine.system_tools import facts_for_call
from va_engine.task_runner import DialogueSource, TaskBrain
from va_events import CallEventBuffer, DbCallEventWriter, EventPublisher
from va_llm import CostTable, LlmGateway, LlmMetrics, OrgProviderStore, ProviderRegistry, load_costs
from va_llm.providers import platform_provider
from va_llm.tokens import TokenCounter, default_counter
from va_media.actions import MediaActions
from va_media.call_end import CallEnd
from va_media.carrier_watch import CarrierWatch
from va_media.greeting import GreetingGate
from va_media.maintenance import maintenance_takeover
from va_media.phrases import CallPhrases
from va_media.runtime_config import (
    ERROR_END,
    pipeline_config,
    session_end,
    stt_config,
    tts_output_format,
    tts_voice,
    zero_retention,
)
from va_media.session import CallSession, SessionBody, SessionEnd
from va_media.settings import MediaSettings
from va_media.transport import TelnyxMediaTransport, UnsupportedMediaFormatError
from va_media.turn_writer import TurnWriter
from va_media.turns import CallPersistence, TurnRecorder
from va_media.vad_model import VadStatus
from va_media.voicemail import DbAnsweredBy, LlmAnswerClassifier, StructuredSource, VoicemailMonitor
from va_media.voicemail_sms import VoicemailSms
from va_media.workflow_signal import TemporalPipelineSignaller
from va_telnyx import TelnyxClient, TelnyxClientFactory, UnknownTelnyxAccount
from va_telnyx.settings import telnyx_factory, telnyx_http
from va_voice.latency import LatencyTracer, TurnMetrics, TurnTracer
from va_voice.phrase_cache import PhraseCacheMetrics
from va_voice.pipeline import NULL_OBSERVER, CallPipeline, ObserverFanout, PipelineConfig, PipelineObserver
from va_voice.vad import SileroVad, VadExecutor
from va_voice.vad_model import SileroModel, VadModel, VadModelError

__all__ = [
    "LLM_HTTP_TIMEOUT",
    "ORG_TIER_UNKNOWN",
    "CallRuntime",
    "OrgSettingsSource",
    "RuntimeDependencies",
    "RuntimeFactory",
    "RuntimeGateway",
    "open_persistence",
    "open_runtime",
]

LLM_HTTP_TIMEOUT: Final = httpx.Timeout(30.0, connect=2.0)
"""Socket bound of the LLM HTTP client; the gateway's own deadlines (specs/10 §2.1) are shorter."""

ORG_TIER_UNKNOWN: Final = "unknown"
"""``org_tier`` label of the turn metrics: the call context carries no plan id yet."""

_log = get_logger(__name__)


class RuntimeDependencies(Protocol):
    """What :func:`open_runtime` needs from the media container (plus a ``database`` attribute)."""

    @property
    def redis(self) -> Redis: ...


class RuntimeFactory(Protocol):
    """Builds the session body for the lifespan (:func:`open_runtime`, or a test factory)."""

    def __call__(  # noqa: PLR0913 - mirrors open_runtime
        self,
        container: RuntimeDependencies,
        settings: MediaSettings,
        *,
        clock: Clock,
        metrics: ServiceMetrics,
        vad: VadStatus,
        models_dir: Path,
    ) -> AbstractAsyncContextManager[SessionBody]: ...


class RuntimeGateway(DialogueSource, StructuredSource, Protocol):
    """The LLM gateway of the runtime: dialogue streams and structured outputs (:class:`LlmGateway`)."""


class OrgSettingsSource(Protocol):
    """Where the runtime reads org settings (:class:`va_db.org_settings.OrgSettingsReader` satisfies it)."""

    async def get(self, org_id: uuid.UUID) -> OrgSettings | None: ...


@dataclass(frozen=True, slots=True)
class _Vendors:
    stt: ElevenLabsRealtimeStt
    tts: TtsSocket


class CallRuntime:
    """The :data:`~va_media.session.SessionBody` of a media-worker process."""

    def __init__(  # noqa: PLR0913 - per-process collaborators, all keyword-only
        self,
        *,
        settings: MediaSettings,
        gateway: RuntimeGateway,
        counter: TokenCounter,
        org_settings: OrgSettingsSource,
        vad_model: VadModel | None,
        vad_executor: VadExecutor,
        clock: Clock,
        metrics: ServiceMetrics,
        telnyx: TelnyxClientFactory | None = None,
        redis: Redis | None = None,
        persistence: CallPersistence | None = None,
        end_routine: CallEnd | None = None,
        tracer_provider: TracerProvider | None = None,
        database: Database | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._end_routine = end_routine
        self._persistence = persistence
        self._redis = redis
        self._telnyx = telnyx
        self._gateway = gateway
        self._counter = counter
        self._org_settings = org_settings
        self._vad_model = vad_model
        self._vad_executor = vad_executor
        self._clock = clock
        self._metrics = metrics
        self._tracer_provider = tracer_provider

    @property
    def end_routine(self) -> CallEnd | None:
        """The session ender of this process (``None``: sessions end without the end-of-call routine)."""
        return self._end_routine

    @property
    def vad_model(self) -> VadModel | None:
        """The process's Silero model (``None``: unavailable, every call ends with ``error``)."""
        return self._vad_model

    async def __call__(self, session: CallSession) -> SessionEnd:
        context = session.context
        if context.config.runner_type != "task":
            _log.error("media.runner_unsupported", runner_type=context.config.runner_type)
            return ERROR_END
        if self._vad_model is None:
            _log.error("media.vad_unavailable")
            return ERROR_END
        org = await self._org_settings.get(context.org_id)
        if org is None:
            _log.error("media.org_missing")
            return ERROR_END
        try:
            transport = TelnyxMediaTransport(session.socket, session.start)
            config = pipeline_config(
                context, org, fmt=transport.fmt, l16_byte_order=self._settings.telnyx_l16_byte_order
            )
        except (UnsupportedMediaFormatError, ValueError) as exc:
            _log.error("media.pipeline_config_invalid", detail=str(exc))
            return ERROR_END
        try:
            return await self._converse(session, org, transport, config, self._vad_model)
        finally:
            transport.log_summary()

    async def _converse(
        self,
        session: CallSession,
        org: OrgSettings,
        transport: TelnyxMediaTransport,
        config: PipelineConfig,
        vad_model: VadModel,
    ) -> SessionEnd:
        context = session.context
        async with AsyncExitStack() as stack:
            try:
                vendors = await self._open_vendors(stack, context, org, config)
            except (SttError, TtsError, OSError) as exc:
                _log.error("media.vendor_open_failed", detail=type(exc).__name__)
                return ERROR_END
            telnyx = await self._telnyx_client(context)
            watch = CarrierWatch(session.feed, session.carrier_state)
            call_control_id = session.start.call_control_id
            recorder = self._recorder(session)
            monitor = self._monitor(session, watch)
            pipeline = CallPipeline(
                transport=transport,
                stt=vendors.stt,
                tts=vendors.tts,
                vad=SileroVad(vad_model, self._vad_executor),
                brain=TaskBrain(
                    context=context,
                    settings=org,
                    gateway=self._gateway,
                    counter=self._counter,
                    facts=facts_for_call(context, org, sms_capable=False),
                    clock=self._clock,
                ),
                clock=self._clock,
                config=config,
                latency=self._latency(context),
                actions=MediaActions(
                    call_id=context.call_id, call_control_id=call_control_id, telnyx=telnyx, clock=self._clock
                ),
                before_opener=GreetingGate(
                    context=context,
                    call_control_id=call_control_id,
                    watch=watch,
                    telnyx=telnyx,
                    clock=self._clock,
                ),
                phrases=self._phrases(context, config),
                observer=_observer(recorder, monitor),
            )
            transport.on_rate_limited = pipeline.speaker.outbound.rate_limited
            maintenance = maintenance_takeover(context)
            if maintenance is not None:
                pipeline.take_over(maintenance)
            if recorder is not None and self._persistence is not None:
                stack.push_async_callback(self._persistence.finish_call, recorder, context)
            if monitor is not None:
                _start_monitor(stack, monitor, watch, pipeline)
            watcher = asyncio.create_task(watch.run(), name="carrier-watch")
            stack.push_async_callback(_stop_task, watcher, "media.carrier_watch_failed")
            result = await pipeline.run()
        return session_end(result)

    def _monitor(self, session: CallSession, watch: CarrierWatch) -> VoicemailMonitor | None:
        """The voicemail monitor of an outbound call (``None`` for inbound and web calls)."""
        context = session.context
        if context.direction != "outbound":
            return None
        return VoicemailMonitor(
            context=context,
            beep=watch.beep,
            clock=self._clock,
            classifier=LlmAnswerClassifier(self._gateway, org_id=context.org_id, call_id=context.call_id),
            events=None if self._persistence is None else self._persistence.events,
            answered_by=None if self._database is None else DbAnsweredBy(self._database, context.org_id),
            answered_at=session.carrier_state.answered_at,
            sms=self._voicemail_sms(context),
        )

    def _voicemail_sms(self, context: CallContext) -> Callable[[], Awaitable[None]] | None:
        """The voicemail SMS hook of the call (``None`` without a database: the SMS cannot be recorded)."""
        if self._database is None:
            return None
        sender = VoicemailSms(
            database=self._database,
            telnyx=self._telnyx,
            events=None if self._persistence is None else self._persistence.events,
            platform_profile_id=self._settings.telnyx_messaging_profile_id,
            edge_public_host=self._settings.edge_public_host,
            clock=self._clock,
        )
        return functools.partial(sender.send, context)

    def _recorder(self, session: CallSession) -> TurnRecorder | None:
        """The call's turn recorder (``None`` without persistence: turns and events are not kept)."""
        if self._persistence is None:
            return None
        return self._persistence.recorder(
            session.context, clock=self._clock, answered_at=session.carrier_state.answered_at
        )

    def _phrases(self, context: CallContext, config: PipelineConfig) -> CallPhrases | None:
        """Phrase cache lookups in the leg's TTS format (``None`` without Redis)."""
        if self._redis is None:
            return None
        return CallPhrases(
            self._redis,
            phrase_voice(context, model_id=self._settings.elevenlabs_tts_model),
            config.tts_format,
            metrics=PhraseCacheMetrics(self._metrics),
        )

    async def _telnyx_client(self, context: CallContext) -> TelnyxClient | None:
        """The call's Telnyx client (platform or BYO); ``None`` without a factory or when the BYO key cannot
        be resolved (logged)."""
        if self._telnyx is None:
            return None
        try:
            return await self._telnyx.for_account(context.config.telnyx_account_id)
        except (UnknownTelnyxAccount, CryptoError, SQLAlchemyError, OSError) as exc:
            _log.warning("media.telnyx_unavailable", error_type=type(exc).__name__)
            return None

    async def _open_vendors(
        self, stack: AsyncExitStack, context: CallContext, org: OrgSettings, config: PipelineConfig
    ) -> _Vendors:
        settings = self._settings
        no_logging = zero_retention(org, available=settings.elevenlabs_zero_retention_available)
        api_key = settings.elevenlabs_api_key.get_secret_value()
        stt = ElevenLabsRealtimeStt(
            SttConnection(
                settings.elevenlabs_ws_base,
                api_key,
                model_id=settings.elevenlabs_stt_model,
                zero_retention=no_logging,
                keyterms_encoding=settings.elevenlabs_stt_keyterms_encoding,
                rotate_after_s=settings.elevenlabs_stt_session_rotate_s,
            ),
            clock=self._clock,
            metrics=CatalogSttMetrics(self._metrics),
            tracer_provider=self._tracer_provider,
        )
        stack.push_async_callback(stt.close)
        tts = TtsSocket(
            TtsConfig(settings.elevenlabs_ws_base, api_key, zero_retention=no_logging),
            clock=self._clock,
            metrics=CatalogTtsMetrics(self._metrics),
            tracer_provider=self._tracer_provider,
        )
        stack.push_async_callback(tts.close)
        outcomes = await asyncio.gather(
            stt.open(stt_config(context, fmt=config.fmt)),
            tts.open(
                tts_output_format(config.fmt), tts_voice(context, model_id=settings.elevenlabs_tts_model)
            ),
            return_exceptions=True,
        )
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        return _Vendors(stt, tts)

    def _latency(self, context: CallContext) -> LatencyTracer:
        return LatencyTracer(
            self._clock,
            metrics=TurnMetrics(self._metrics, org_tier=ORG_TIER_UNKNOWN),
            tracer=TurnTracer(
                self._clock,
                call_id=str(context.call_id),
                org_id=str(context.org_id),
                tracer_provider=self._tracer_provider,
            ),
        )


def _start_monitor(
    stack: AsyncExitStack, monitor: VoicemailMonitor, watch: CarrierWatch, pipeline: CallPipeline
) -> None:
    """Bind the monitor to the pipeline and the carrier watch before either runs (an attach-time verdict acts
    now, so the opener is skipped); on exit the classifier stops first, then the monitor's recordings finish
    (before the call's events are flushed)."""
    monitor.bind(pipeline.take_over)
    watch.on_verdict = monitor.on_verdict
    monitor.start(watch.verdict)
    stack.push_async_callback(monitor.stop)
    classifier = asyncio.create_task(monitor.run(), name="voicemail-classifier")
    stack.push_async_callback(_stop_task, classifier, "media.voicemail_classifier_failed")


def _observer(recorder: TurnRecorder | None, monitor: VoicemailMonitor | None) -> PipelineObserver:
    """The pipeline observer: the turn recorder and the voicemail monitor, whichever exist."""
    observers = [observer for observer in (recorder, monitor) if observer is not None]
    if not observers:
        return NULL_OBSERVER
    if len(observers) == 1:
        return observers[0]
    return ObserverFanout(*observers)


async def _stop_task(task: asyncio.Task[None], failure_event: str) -> None:
    """Cancel a per-call side task (carrier watch, voicemail classifier); a failure it ended with is logged as
    ``failure_event`` (the call went on without it)."""
    task.cancel()
    await asyncio.wait({task})
    if not task.cancelled() and task.exception() is not None:
        _log.error(failure_event, error_type=type(task.exception()).__name__)


async def _load_model(vad: VadStatus, models_dir: Path) -> SileroModel | None:
    if not vad.loaded:
        return None
    try:
        return await asyncio.to_thread(SileroModel.load, models_dir)
    except VadModelError as exc:
        vad.loaded = False
        vad.error = str(exc)
        _log.error("media.vad_model_unavailable", detail=vad.error)
        return None


@asynccontextmanager
async def open_persistence(
    database: Database, redis: Redis, *, clock: Clock
) -> AsyncIterator[CallPersistence]:
    """The per-process ``call_events`` buffer, event publisher and turn writer, their flush loops running
    until exit; on exit both loops stop and what is pending is written (failures logged)."""
    rows = CallEventBuffer(DbCallEventWriter(database), clock=clock)
    turns = TurnWriter(database, clock=clock)
    loops = [
        asyncio.create_task(rows.run(), name="call-events-flush"),
        asyncio.create_task(turns.run(), name="call-turns-flush"),
    ]
    try:
        yield CallPersistence(turns=turns, events=EventPublisher(redis, call_events=rows), event_rows=rows)
    finally:
        await turns.aclose()
        await rows.aclose()
        for loop in loops:
            loop.cancel()
        await asyncio.wait(loops)


@asynccontextmanager
async def open_runtime(  # noqa: PLR0913 - the lifespan's collaborators, keyword-only
    container: RuntimeDependencies,
    settings: MediaSettings,
    *,
    clock: Clock,
    metrics: ServiceMetrics,
    vad: VadStatus,
    models_dir: Path,
) -> AsyncIterator[CallRuntime]:
    """The per-process runtime over the container's ``database`` and ``redis`` (``TypeError`` without a
    database). The Silero model loads only when ``vad`` was verified; a load failure fails readiness. The
    platform LLM connection is prewarmed (a failure is logged, never raised). The Telnyx pool and client
    factory (BYO keys through :class:`TelnyxKeyResolver`) serve outbound DTMF and the end-of-call hangup; the
    end routine signals ``CallWorkflow`` through a lazily connected Temporal client (not a readiness check,
    specs/07 §2)."""
    database = getattr(container, "database", None)
    if not isinstance(database, Database):
        msg = "the media container has no database"
        raise TypeError(msg)
    async with open_kms_provider(settings) as kms, AsyncExitStack() as stack:
        envelope = Envelope(kms, OrgDataKeyStore(database))
        http = http_client(settings, timeout=LLM_HTTP_TIMEOUT)
        stack.push_async_callback(http.aclose)
        gateway = LlmGateway(
            ProviderRegistry(platform_provider(settings), OrgProviderStore(database, envelope, clock=clock)),
            http,
            clock=clock,
            metrics=LlmMetrics(metrics.registry, metrics),
            costs=CostTable(load_costs(settings.llm_model_costs_json)),
        )
        telnyx_pool = telnyx_http(settings)
        stack.push_async_callback(telnyx_pool.aclose)
        telnyx = telnyx_factory(
            settings,
            http=telnyx_pool,
            redis=container.redis,
            resolve_key=TelnyxKeyResolver(database, envelope),
            clock=clock,
        )
        executor = await stack.enter_async_context(VadExecutor(settings.media_vad_threads))
        persistence = await stack.enter_async_context(
            open_persistence(database, container.redis, clock=clock)
        )
        await gateway.prewarm()
        end_routine = CallEnd(
            database=database,
            redis=container.redis,
            signaller=TemporalPipelineSignaller(
                TemporalConnector(settings.temporal_host, settings.temporal_namespace)
            ),
            clock=clock,
            telnyx=telnyx,
            events=persistence.events,
            event_rows=persistence.event_rows,
        )
        yield CallRuntime(
            settings=settings,
            gateway=gateway,
            counter=default_counter(),
            org_settings=OrgSettingsReader(database, container.redis),
            vad_model=await _load_model(vad, models_dir),
            vad_executor=executor,
            clock=clock,
            metrics=metrics,
            telnyx=telnyx,
            redis=container.redis,
            persistence=persistence,
            end_routine=end_routine,
            database=database,
        )
