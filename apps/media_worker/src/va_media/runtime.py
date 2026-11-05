"""The call runtime: the session body that runs the pipeline of every attached call
(specs/07-media-pipeline.md §1, §16; specs/07 §16 Decision P05-T10a).

:func:`open_runtime` builds the per-process collaborators once (the LLM gateway over one HTTP client with the
org provider store behind the envelope, the org settings reader, the token counter, the Silero model and the
VAD thread pool) and yields a :class:`CallRuntime`. For each call :meth:`CallRuntime.__call__`:

1. refuses runners other than ``task`` and ``pathway`` (persona/agent Brains arrive in later phases) and
   calls whose org is gone, without opening a vendor socket;
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
9. a ``transfer_call`` action runs the call's :class:`~va_media.transfer.ColdTransfer` (status
   ``transferring``, ``call.transfer_started``, Telnyx ``transfer``, the target leg's outcome): bridged → the
   pipeline is taken over with ``transferred`` (the end routine stops the stream, the call stays up); failed →
   the Brain learns it and the pipeline says the org's ``transfer_failed_message`` (specs/06 §10.1 Decision
   P05-T13).
10. the outbound mix carries the call's ``background_track`` (``null`` → the ``static`` bed, ``"none"`` → no
    bed) from the per-process :class:`~va_voice.background.BackgroundLibrary` over
    :data:`BACKGROUND_ASSETS_DIR`; an asset that cannot be read is logged ``media.background_unavailable`` and
    the call goes on without a bed. ``noise_cancellation`` gates the VAD path and turns Scribe's
    ``filter_background_audio`` on (``va_media.runtime_config``; specs/07 §10 Decision P05-T15).
11. a ``pathway`` call runs a :class:`~va_engine.pathway_brain.PathwayBrain` on its pinned version
    (:func:`~va_media.pathway_runtime.load_call_graph` and
    :func:`~va_media.pathway_runtime.build_pathway_brain` over
    :class:`~va_media.pathway_runtime.PathwayTools`: the per-process graph cache, the shared tool HTTP client,
    the SMS port over
    :class:`~va_media.call_sms.OutboundSms`), built before any vendor socket opens: a version that cannot be
    loaded ends the call with ``error`` (logged ``media.pathway_unavailable``). Its engine events become call
    events through the recorder, its rows carry ``pathway_turn``/``node_id``/``node_name``, and its state is
    snapshotted in ``call:{id}:engine`` after every turn
    (:class:`~va_media.pathway_runtime.EngineStateSaver`);
    on exit the brain settles and closes before the snapshot is flushed and the call's events are drained
    (specs/11 §10 Decision P07-T12b). Its reply nodes offer the system tools of item 12 (never
    ``transfer_call``; ``end_call`` per ``globalConfig.allowEndCallTool``) and run the custom tools their
    ``tools`` and ``globalConfig.globalTools`` name through the call's
    :class:`~va_media.call_tools.PathwayCallTools` (the stored tools of the pinned graph and of every graph
    the call switches to, inline tools, signed requests); ``call.tool`` events carry the engine turn and node
    (:class:`~va_media.pathway_runtime.PathwayToolEvents`), and system tool runs a barge-in left running are
    drained before the brain closes (specs/13 §4 Decision P08-T02c2).
12. a ``task`` call with custom tools (``tool_ids``, ``inline_tools``) hands its :class:`TaskBrain` the
    call's :class:`~va_tools.executor.CallToolExecutor` from :class:`~va_media.call_tools.CallToolSource`
    (stored tools with decrypted secret headers, inline tools, the shared tool HTTP client): tool runs become
    ``call.tool``/``call.error`` events through the recorder, and the variables they extract are snapshotted
    in ``call:{id}:engine`` (merged into ``calls.variables`` by the end routine) (specs/13 §5 Decision
    P08-T02b1). The same executor runs the call's ``send_sms`` (offered when the agent number is an active
    SMS-capable org number and ``tools.allow_send_sms``) and ``schedule_callback`` (offered with
    ``tools.allow_schedule_callback`` or ``engine.allow_callbacks``; the child's ``CallWorkflow`` is started
    through :class:`~va_media.call_system_tools.TemporalCallbackStarter` over the Temporal client the end
    routine signals with) (:mod:`va_media.call_system_tools`; specs/13 §6 Decision P08-T02b2). Neither is
    offered when the other party has no E.164 number; at call end the executor waits for system tool runs a
    barge-in left running before the snapshot and the events are flushed (Decision P08-FIX-13).
"""

from __future__ import annotations

import asyncio
import functools
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
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
from va_engine.navigation import PathwayUnavailableError
from va_engine.opener import phrase_voice
from va_engine.pathway_brain import PathwayBrain
from va_engine.pathway_call import start_state
from va_engine.system_tools import facts_for_call
from va_engine.task_runner import TaskBrain
from va_events import CallEventBuffer, DbCallEventWriter, DbWebhookRouter, EventPublisher
from va_llm import LlmMetrics
from va_llm.platform import platform_gateway
from va_llm.tokens import TokenCounter, default_counter
from va_media.actions import MediaActions
from va_media.call_end import CallEnd
from va_media.call_sms import CallSmsPort, OutboundSms, counterpart_number, dialable
from va_media.call_system_tools import (
    CallbackDeps,
    CallbackScheduler,
    CallbackStarter,
    TemporalCallbackStarter,
    ToolSms,
    sms_capable,
)
from va_media.call_tools import CallToolSource
from va_media.carrier_watch import CarrierWatch
from va_media.greeting import GreetingGate
from va_media.maintenance import maintenance_takeover
from va_media.pathway_runtime import (
    EngineStateSaver,
    PathwayGateway,
    PathwayToolEvents,
    PathwayTools,
    build_pathway_brain,
    load_call_graph,
)
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
from va_media.transfer import ColdTransfer, DbTransferStatus
from va_media.transport import TelnyxMediaTransport, UnsupportedMediaFormatError, stream_format
from va_media.turn_writer import TurnWriter
from va_media.turns import CallPersistence, TurnRecorder
from va_media.vad_model import VadStatus
from va_media.voicemail import DbAnsweredBy, LlmAnswerClassifier, VoicemailMonitor
from va_media.voicemail_sms import VoicemailSms
from va_media.workflow_signal import TemporalPipelineSignaller
from va_pathways.call_graphs import CallGraphCache
from va_pathways.runs import insecure_urls_allowed
from va_telnyx import TelnyxClient, TelnyxClientFactory, UnknownTelnyxAccount, webhook_scheme
from va_telnyx.settings import telnyx_factory, telnyx_http
from va_tools.executor import SystemTool
from va_tools.http import tools_client
from va_voice.background import BackgroundAssetError, BackgroundLibrary
from va_voice.latency import LatencyTracer, TurnMetrics, TurnTracer
from va_voice.outbound import FrameMixer
from va_voice.phrase_cache import PhraseCacheMetrics
from va_voice.pipeline import (
    NULL_OBSERVER,
    CallPipeline,
    ObserverFanout,
    PipelineConfig,
    PipelineObserver,
    Takeover,
)
from va_voice.vad import SileroVad, VadExecutor
from va_voice.vad_model import SileroModel, VadModel, VadModelError

__all__ = [
    "BACKGROUND_ASSETS_DIR",
    "LLM_HTTP_TIMEOUT",
    "ORG_TIER_UNKNOWN",
    "CallRuntime",
    "OrgSettingsSource",
    "RuntimeDependencies",
    "RuntimeFactory",
    "RuntimeGateway",
    "TransferAware",
    "background_mixer",
    "open_persistence",
    "open_runtime",
]

LLM_HTTP_TIMEOUT: Final = httpx.Timeout(30.0, connect=2.0)
"""Socket bound of the LLM HTTP client; the gateway's own deadlines (specs/10 §2.1) are shorter."""

BACKGROUND_ASSETS_DIR: Final = Path("assets") / "audio"
"""Background loops, relative to the working directory (``/app`` in the image, the repository root
locally)."""

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


class RuntimeGateway(PathwayGateway, Protocol):
    """The LLM gateway of the runtime: dialogue streams, structured outputs and completions
    (:class:`LlmGateway`)."""


class TransferAware(Protocol):
    """A Brain that learns about a failed transfer (:class:`TaskBrain`, :class:`PathwayBrain`)."""

    def on_transfer_failed(self, spoken: str) -> None: ...


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
        background: BackgroundLibrary,
        telnyx: TelnyxClientFactory | None = None,
        redis: Redis | None = None,
        persistence: CallPersistence | None = None,
        end_routine: CallEnd | None = None,
        tracer_provider: TracerProvider | None = None,
        database: Database | None = None,
        pathways: PathwayTools | None = None,
        call_tools: CallToolSource | None = None,
        callback_starter: CallbackStarter | None = None,
    ) -> None:
        self._settings = settings
        self._pathways = pathways
        self._call_tools = call_tools
        self._callbacks = (
            None
            if database is None or redis is None or callback_starter is None
            else CallbackDeps(
                database=database,
                publisher=EventPublisher(redis, call_events=None, router=DbWebhookRouter(database)),
                starter=callback_starter,
                clock=clock,
            )
        )
        self._background = background
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
        self._underruns = metrics.counter("va_audio_underruns_total")
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
        if context.config.runner_type not in {"task", "pathway"}:
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
            recorder = self._recorder(session)
            if recorder is not None and self._persistence is not None:
                stack.push_async_callback(self._persistence.finish_call, recorder, context)
            try:
                brain = await self._brain(stack, context, org, recorder)
            except PathwayUnavailableError as exc:
                _log.error("media.pathway_unavailable", reason=exc.reason)
                return ERROR_END
            try:
                vendors = await self._open_vendors(stack, context, org, config)
            except (SttError, TtsError, OSError) as exc:
                _log.error("media.vendor_open_failed", detail=type(exc).__name__)
                return ERROR_END
            telnyx = await self._telnyx_client(context)
            watch = CarrierWatch(session.feed, session.carrier_state)
            call_control_id = session.start.call_control_id
            monitor = self._monitor(session, watch)
            actions = MediaActions(
                call_id=context.call_id,
                call_control_id=call_control_id,
                telnyx=telnyx,
                clock=self._clock,
                transfer=self._cold_transfer(session, org, telnyx, watch),
            )
            mixer = await background_mixer(self._background, context, config)
            pipeline = CallPipeline(
                transport=transport,
                stt=vendors.stt,
                tts=vendors.tts,
                vad=SileroVad(vad_model, self._vad_executor),
                brain=brain,
                clock=self._clock,
                config=config,
                latency=self._latency(context),
                actions=actions,
                before_opener=GreetingGate(
                    context=context,
                    call_control_id=call_control_id,
                    watch=watch,
                    telnyx=telnyx,
                    clock=self._clock,
                ),
                phrases=self._phrases(context, config),
                observer=_observer(recorder, monitor),
                mixer=mixer,
                on_underrun=self._underruns.inc,
            )
            transport.on_rate_limited = pipeline.speaker.outbound.rate_limited
            _bind_transfer(actions, pipeline, brain, org.transfer_failed_message)
            stack.callback(_unwire, transport, actions, watch, monitor)
            maintenance = maintenance_takeover(context)
            if maintenance is not None:
                pipeline.take_over(maintenance)
            if monitor is not None:
                _start_monitor(stack, monitor, watch, pipeline)
            watcher = asyncio.create_task(watch.run(), name="carrier-watch")
            stack.push_async_callback(_stop_task, watcher, "media.carrier_watch_failed")
            if recorder is not None:
                recorder.started(stream_format(session.start.media_format))
            result = await pipeline.run()
        return session_end(result)

    async def _brain(
        self, stack: AsyncExitStack, context: CallContext, org: OrgSettings, recorder: TurnRecorder | None
    ) -> TaskBrain | PathwayBrain:
        """The call's Brain. A pathway brain closes on exit (settling its background work), then its last
        snapshot is flushed, both before the call's events are drained (registered after the recorder's
        ``finish_call``). ``PathwayUnavailableError`` when the pathway cannot run."""
        if context.config.runner_type == "task":
            return await self._task_brain(stack, context, org, recorder)
        return await self._pathway_brain(stack, context, org, recorder)

    async def _pathway_brain(
        self, stack: AsyncExitStack, context: CallContext, org: OrgSettings, recorder: TurnRecorder | None
    ) -> PathwayBrain:
        """A pathway brain on the call's pinned version with the tools of its graphs and the call's system
        tools (specs/13 §4 Decision P08-T02c2). On exit (LIFO) the system tool runs a barge-in left running
        are drained, then the brain closes, then the snapshot is flushed."""
        if self._pathways is None:
            raise PathwayUnavailableError("not_configured")
        active = await load_call_graph(context, self._pathways)
        state = start_state(active, context)
        sms_ok, system = await self._system_tools(context, org, recorder)
        call_tools = None
        if self._call_tools is not None:
            events = None if recorder is None else PathwayToolEvents(recorder, state)
            call_tools = await self._call_tools.pathway_tools(context, active.graph, events, system=system)
        saver = None if self._redis is None else EngineStateSaver(self._redis, context.call_id)
        brain = build_pathway_brain(
            context,
            org,
            active=active,
            state=state,
            tools=self._pathways,
            gateway=self._gateway,
            counter=self._counter,
            clock=self._clock,
            sms=self._sms_port(context),
            recorder=recorder,
            saver=saver,
            call_tools=call_tools,
            system_tools=facts_for_call(context, org, sms_capable=sms_ok),
        )
        if saver is not None:
            stack.push_async_callback(saver.aclose)
        stack.push_async_callback(brain.aclose)
        if call_tools is not None:
            stack.push_async_callback(call_tools.executor.aclose)
        return brain

    async def _task_brain(
        self, stack: AsyncExitStack, context: CallContext, org: OrgSettings, recorder: TurnRecorder | None
    ) -> TaskBrain:
        """A task brain with the call's tools; with tools its state snapshot is flushed on exit."""
        sms_ok, system = await self._system_tools(context, org, recorder)
        executor = (
            None
            if self._call_tools is None
            else await self._call_tools.executor(context, recorder, system=system)
        )
        saver = None
        if executor is not None and self._redis is not None:
            saver = EngineStateSaver(self._redis, context.call_id)
            stack.push_async_callback(saver.aclose)
        if executor is not None:
            stack.push_async_callback(executor.aclose)  # LIFO: drains before the snapshot flush
        return TaskBrain(
            context=context,
            settings=org,
            gateway=self._gateway,
            counter=self._counter,
            facts=facts_for_call(context, org, sms_capable=sms_ok),
            clock=self._clock,
            executor=executor,
            on_state=None if saver is None else saver.on_state,
        )

    async def _system_tools(
        self, context: CallContext, org: OrgSettings, recorder: TurnRecorder | None
    ) -> tuple[bool, list[SystemTool]]:
        """Whether the agent number can send SMS, and the executor-backed system tools the call may use
        (specs/13 §6 Decision P08-T02b2): only tools the runtime can run are built (a database for both, a
        workflow starter for callbacks), and the TaskBrain offers only built ones. An anonymous or withheld
        other party gets neither (Decision P08-FIX-13): nothing could reach it."""
        database = self._database
        if database is None or self._call_tools is None:
            return False, []
        if not dialable(counterpart_number(context)):
            _log.info("media.system_tools_skipped", reason="no_callable_number")
            return False, []
        tools: list[SystemTool] = []
        sms_ok = org.tools.allow_send_sms and await sms_capable(database, context)
        if sms_ok:
            tools.append(ToolSms(self._outbound_sms(database, "media.tool_sms"), context, recorder))
        if (org.tools.allow_schedule_callback or org.engine.allow_callbacks) and self._callbacks is not None:
            tools.append(CallbackScheduler(self._callbacks, context))
        return sms_ok, tools

    def _outbound_sms(self, database: Database, log_prefix: str) -> OutboundSms:
        return OutboundSms(
            database=database,
            telnyx=self._telnyx,
            platform_profile_id=self._settings.telnyx_messaging_profile_id,
            edge_public_host=self._settings.edge_public_host,
            clock=self._clock,
            log_prefix=log_prefix,
            webhook_scheme=webhook_scheme(self._settings.va_env.value),
        )

    def _sms_port(self, context: CallContext) -> CallSmsPort | None:
        """The SMS port of a pathway call (``None`` without a database: the message cannot be recorded)."""
        if self._database is None:
            return None
        return CallSmsPort(self._outbound_sms(self._database, "media.pathway_sms"), context)

    def _cold_transfer(
        self, session: CallSession, org: OrgSettings, telnyx: TelnyxClient | None, watch: CarrierWatch
    ) -> ColdTransfer:
        """The call's cold transfer (specs/06 §10.1 Decision P05-T13): status writes need the database,
        ``time_limit_secs`` counts from the answer (or now) to ``max_duration``."""
        context = session.context
        if context.config.stream_post_transfer_transcript:
            _log.info("media.post_transfer_transcript_deferred")
        started = session.carrier_state.answered_at or self._clock.now()
        return ColdTransfer(
            context=context,
            call_control_id=session.start.call_control_id,
            telnyx=telnyx,
            transfer_events=watch.transfer_events,
            status=None if self._database is None else DbTransferStatus(self._database, context.org_id),
            events=None if self._persistence is None else self._persistence.events,
            caller_id_mode=org.transfer_caller_id_mode,
            deadline=started + timedelta(minutes=context.config.max_duration_min),
            clock=self._clock,
            tracer_provider=self._tracer_provider,
        )

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
            webhook_scheme=webhook_scheme(self._settings.va_env.value),
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


async def background_mixer(
    library: BackgroundLibrary, context: CallContext, config: PipelineConfig
) -> FrameMixer:
    """The leg's outbound mixer with the call's background loop (specs/07 §10.2); an unreadable asset is
    logged ``media.background_unavailable`` and the mixer has no bed (the call goes on)."""
    track = context.config.background_track
    try:
        loop = await library.loop(track, config.tts_format)
    except (BackgroundAssetError, ValueError) as exc:
        _log.warning("media.background_unavailable", track=track, detail=type(exc).__name__)
        loop = None
    return FrameMixer(config.tts_format, background=loop, wire_byte_order=config.wire_byte_order)


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


def _bind_transfer(
    actions: MediaActions, pipeline: CallPipeline, brain: TransferAware, failed_message: str
) -> None:
    """A bridged transfer ends the AI's part of the call (takeover ``transferred``, no message); a failed one
    tells the Brain and speaks the org's ``transfer_failed_message``, then the conversation goes on."""

    def transferred() -> None:
        if not pipeline.take_over(Takeover(reason="transferred")):
            _log.info("media.transfer_takeover_refused")

    def transfer_failed() -> None:
        brain.on_transfer_failed(failed_message)
        if not pipeline.say(failed_message):
            _log.info("media.transfer_failed_message_dropped")

    actions.bind(on_transferred=transferred, on_transfer_failed=transfer_failed)


def _unwire(
    transport: TelnyxMediaTransport,
    actions: MediaActions,
    watch: CarrierWatch,
    monitor: VoicemailMonitor | None,
) -> None:
    """Drop the callbacks wired between the call's parts when the call's exit stack unwinds.

    Each points back into the pipeline (its outbound clock, takeover, say), so left in place a finished call
    would be a set of reference cycles freed only by the cyclic collector, in one long pause of the media loop
    for every leg still talking (specs/07 §9 Decision P08-FIX-3).

    The stack is LIFO (specs/07 §9 Decision P08-FIX-4): this runs after the parts registered after it stopped
    (the carrier-watch task, the voicemail monitor) and before the ones registered earlier close (the brain,
    the vendor sockets, then ``persistence.finish_call``). A takeover or transfer outcome raised while those
    later closes run is dropped (``media.voicemail_unbound``), which is harmless: the call is already over.
    """
    transport.on_rate_limited = None
    actions.unbind()
    watch.on_verdict = None
    if monitor is not None:
        monitor.unbind()


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
    until exit; on exit both loops stop and what is pending is written (failures logged).

    The buffer's writer routes the flushed rows' events to the webhook outbox in the same per-org
    transaction (specs/19 §1 step 5, Decision P06-T07), so the publisher itself has no router."""
    rows = CallEventBuffer(DbCallEventWriter(database, router=DbWebhookRouter(database)), clock=clock)
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
    platform LLM connection is prewarmed (a failure is logged, never raised). Pathway calls and task-mode
    tools share one guarded tool HTTP client (``WEBHOOK_ALLOW_PRIVATE``); pathway calls share one graph
    cache. The Telnyx pool and client factory (BYO keys through :class:`TelnyxKeyResolver`) serve outbound
    DTMF and the end-of-call hangup; the end routine signals ``CallWorkflow`` through a lazily connected
    Temporal client (not a readiness check, specs/07 §2)."""
    database = getattr(container, "database", None)
    if not isinstance(database, Database):
        msg = "the media container has no database"
        raise TypeError(msg)
    async with open_kms_provider(settings) as kms, AsyncExitStack() as stack:
        envelope = Envelope(kms, OrgDataKeyStore(database))
        http = http_client(settings, timeout=LLM_HTTP_TIMEOUT)
        stack.push_async_callback(http.aclose)
        gateway = platform_gateway(
            settings,
            database=database,
            envelope=envelope,
            http=http,
            clock=clock,
            metrics=LlmMetrics(metrics.registry, metrics),
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
        tools_http = tools_client(settings)
        stack.push_async_callback(tools_http.aclose)
        executor = await stack.enter_async_context(VadExecutor(settings.media_vad_threads))
        persistence = await stack.enter_async_context(
            open_persistence(database, container.redis, clock=clock)
        )
        await gateway.prewarm()
        temporal = TemporalConnector(settings.temporal_host, settings.temporal_namespace)
        end_routine = CallEnd(
            database=database,
            redis=container.redis,
            signaller=TemporalPipelineSignaller(temporal),
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
            background=BackgroundLibrary(BACKGROUND_ASSETS_DIR),
            telnyx=telnyx,
            redis=container.redis,
            persistence=persistence,
            end_routine=end_routine,
            database=database,
            pathways=PathwayTools(
                graphs=CallGraphCache(database),
                tools_http=tools_http,
                allow_http=insecure_urls_allowed(settings.va_env),
            ),
            call_tools=CallToolSource(
                database=database,
                envelope=envelope,
                tools_http=tools_http,
                allow_http=insecure_urls_allowed(settings.va_env),
                clock=clock,
            ),
            callback_starter=TemporalCallbackStarter(temporal, settings.temporal_task_queue),
        )
