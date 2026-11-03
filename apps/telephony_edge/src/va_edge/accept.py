"""Inbound accept (specs/06-telephony-telnyx.md §7 steps 8-11, Decision P05-T12c).

:class:`InboundAccept` is the router's :class:`~va_edge.routing.InboundAcceptor`. For a call that holds a
concurrency slot it runs, within :data:`ACCEPT_BUDGET_S` of clock time:

1. **persist** (one ``tenant_session``): resolve the inbound request through the shared loader, upsert the
   caller's contact, insert the ``calls`` row (``inbound``, ``ringing``; ``to_number`` = the called number,
   ``from_number`` = the caller, as Bland reports them), the primary leg, and the ``call.ringing`` event row;
2. **context** (best effort, :data:`CONTEXT_BUDGET_S`): build and store the call context; on a miss the media
   worker rebuilds it from Postgres (specs/07 §3);
3. **start** ``CallWorkflow`` in inbound mode;
4. **answer** the leg with the stream parameters, the recording fields, ``client_state``, and
   ``command_id=answer-{call_id}``.

Any failure after the commit (or the budget running out) records the call ``failed`` (``error``), terminates
the workflow if it started, and re-raises so the router releases the slot and refuses the call (fallback
transfer or reject). A caller who hung up before the answer is not a failure: the carrier events end the call.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol

from opentelemetry import propagate
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from va_billing.dispatch import load_billing_org
from va_common.checks import TemporalConnector, start_workflow, terminate_workflow
from va_common.logging import get_logger
from va_common.metrics import ServiceMetrics
from va_common.stream_tokens import media_ws_scheme, mint_stream_token, stream_token_expiry
from va_common.time import Clock
from va_common.waits import within
from va_db.call_context import CallContextStore, build_call_context
from va_db.call_end import ended_values
from va_db.call_inputs.loader import InboundNumber, domain_errors, load_inbound_inputs, resolve_inputs
from va_db.call_rows import NewCall, call_row
from va_db.repositories import CallLegRepo, CallRepo, CallTransitionError, ContactRepo, OrgRepo
from va_db.session import Database
from va_domain.call_context import CallContext
from va_domain.call_resolution import PlatformModels, inbound_request
from va_domain.call_workflow import (
    CALL_WORKFLOW_NAME,
    CallWorkflowInput,
    call_workflow_id,
    max_duration_seconds,
)
from va_domain.calls import SendCallConfig
from va_domain.numbers import MAINTENANCE_MESSAGE_KEY
from va_edge.routing import InboundCall
from va_edge.settings import EdgeSettings
from va_events import (
    DomainEvent,
    EventPublisher,
    EventPublishError,
    EventType,
    insert_rows,
    new_event,
    row_for,
)
from va_events.call_ended import call_ended_event
from va_telnyx import AnswerRequest, ClientState
from va_telnyx.client_state import encode
from va_telnyx.errors import CallAlreadyEnded
from va_telnyx.factory import TelnyxClientFactory
from va_telnyx.streams import RECORDING_FIELDS, media_stream_url, stream_params

__all__ = [
    "ACCEPT_BUDGET_S",
    "ACCEPT_FAILED_MESSAGE",
    "CONTEXT_BUDGET_S",
    "INBOUND_ANSWER_MS",
    "INBOUND_HANDLER_BUDGET_S",
    "RINGING_MESSAGE",
    "START_RPC_TIMEOUT_S",
    "START_TIMEOUT_S",
    "AcceptSettings",
    "InboundAccept",
    "InboundAcceptTimeoutError",
    "InboundStarter",
    "TemporalInboundStarter",
    "answer_command_id",
]

ACCEPT_BUDGET_S: Final = 3.0
"""Persist, context, start, and answer together (the router's refusal and the cleanup fit the handler
budget)."""
CONTEXT_BUDGET_S: Final = 1.5
INBOUND_HANDLER_BUDGET_S: Final = 5.0
"""The processor budget of ``call.initiated`` (every other event type keeps the default 2 s)."""
START_TIMEOUT_S: Final = 1.0
START_RPC_TIMEOUT_S: Final = 0.8
INBOUND_ANSWER_MS: Final = "va_inbound_answer_ms"
ACCEPT_FAILED_MESSAGE: Final = "The inbound call could not be answered."
RINGING_MESSAGE: Final = "Inbound call ringing."
CLEANUP_TERMINATE_REASON: Final = "inbound_accept_failed"

_log = get_logger(__name__)


def answer_command_id(call_id: uuid.UUID) -> str:
    """``answer-{call_id}``: every retry of the answer is deduplicated by Telnyx."""
    return f"answer-{call_id}"


class InboundAcceptTimeoutError(Exception):
    """The accept did not finish within :data:`ACCEPT_BUDGET_S` (its remaining work was cancelled)."""


class InboundStarter(Protocol):
    """Starts and (on cleanup) terminates the ``CallWorkflow`` of an inbound call."""

    async def start(self, call: CallWorkflowInput) -> None: ...

    async def terminate(self, call_id: uuid.UUID, reason: str) -> None: ...


class TemporalInboundStarter:
    """:class:`InboundStarter` over Temporal (``REJECT_DUPLICATE``; an existing workflow counts as
    started)."""

    def __init__(self, connector: TemporalConnector, task_queue: str) -> None:
        self._connector = connector
        self._task_queue = task_queue

    async def start(self, call: CallWorkflowInput) -> None:
        started = await start_workflow(
            self._connector,
            CALL_WORKFLOW_NAME,
            call.model_dump(mode="json"),
            workflow_id=call.workflow_id(),
            task_queue=self._task_queue,
            timeout_s=START_TIMEOUT_S,
            rpc_timeout_s=START_RPC_TIMEOUT_S,
        )
        if not started:
            _log.info("telnyx.inbound_workflow_already_started", call_id=str(call.call_id))

    async def terminate(self, call_id: uuid.UUID, reason: str) -> None:
        await terminate_workflow(self._connector, call_workflow_id(call_id), reason)


@dataclass(frozen=True, slots=True)
class AcceptSettings:
    """What the accept needs from the edge settings (the stream secret is hidden from ``repr``)."""

    media_public_host: str
    media_ws_scheme: Literal["ws", "wss"]
    stream_secret: bytes = field(repr=False)
    models: PlatformModels
    task_queue: str

    @classmethod
    def from_settings(cls, settings: EdgeSettings) -> AcceptSettings:
        """``RuntimeError`` when ``STREAM_TOKEN_SECRET`` is not configured."""
        secret = settings.stream_token_secret.get_secret_value()
        if not secret:
            msg = "STREAM_TOKEN_SECRET is not configured"
            raise RuntimeError(msg)
        return cls(
            media_public_host=settings.media_public_host,
            media_ws_scheme=media_ws_scheme(settings.va_env.value),
            stream_secret=bytes.fromhex(secret),
            models=PlatformModels(settings.llm_dialogue_model, settings.llm_router_model),
            task_queue=settings.temporal_task_queue,
        )


@dataclass(slots=True)
class _Progress:
    committed: bool = False
    workflow_started: bool = False


@dataclass(frozen=True, slots=True)
class _Persisted:
    config: SendCallConfig
    context: CallContext
    event: DomainEvent


class InboundAccept:
    """Creates and answers accepted inbound calls (see the module docstring)."""

    def __init__(  # noqa: PLR0913 - collaborators plus keyword-only seams
        self,
        database: Database,
        contexts: CallContextStore,
        starter: InboundStarter,
        telnyx: TelnyxClientFactory,
        *,
        publisher: EventPublisher,
        settings: AcceptSettings,
        clock: Clock,
        metrics: ServiceMetrics,
    ) -> None:
        self._database = database
        self._contexts = contexts
        self._starter = starter
        self._telnyx = telnyx
        self._publisher = publisher
        self._settings = settings
        self._clock = clock
        self._answer_ms = metrics.histogram(INBOUND_ANSWER_MS)

    async def accept(self, call: InboundCall) -> None:
        progress = _Progress()
        try:
            finished = await within(self._steps(call, progress), self._clock, ACCEPT_BUDGET_S)
        except Exception:
            await self._cleanup(call, progress)
            raise
        if not finished:
            await self._cleanup(call, progress)
            msg = f"the inbound accept did not finish within {ACCEPT_BUDGET_S} s"
            raise InboundAcceptTimeoutError(msg)

    async def _steps(self, call: InboundCall, progress: _Progress) -> None:
        persisted = await self._persist(call)
        progress.committed = True
        await self._broadcast(persisted.event)
        await self._save_context(persisted.context)
        await self._starter.start(
            CallWorkflowInput(
                call_id=call.call_id,
                org_id=call.number.org_id,
                direction="inbound",
                max_duration_s=max_duration_seconds(persisted.config.max_duration_min),
            )
        )
        progress.workflow_started = True
        await self._answer(call, persisted.config)

    async def _persist(self, call: InboundCall) -> _Persisted:
        """Resolve and write the call in one tenant transaction (nothing is written when resolution fails)."""
        org_id = call.number.org_id
        number = call.number
        now = self._clock.now()
        async with self._database.tenant_session(org_id) as session:
            billing = await load_billing_org(session, org_id)
            org = await OrgRepo(session).get(org_id)
            with domain_errors():
                request = inbound_request(
                    call.config,
                    caller=call.payload.from_ or "anonymous",
                    called=number.e164,
                    org=billing.settings,
                    org_name="" if org is None else org.name,
                )
            inputs = await load_inbound_inputs(
                session,
                org_id,
                request,
                plan_id=billing.plan_id,
                org=billing.settings,
                number=InboundNumber(number.phone_number_id, number.e164, number.telnyx_account_id),
                models=self._settings.models,
            )
            config = resolve_inputs(inputs, request, now).config
            dialable = call.caller.startswith("+")
            contact_id = await ContactRepo(session, org_id).upsert_phone(call.caller) if dialable else None
            extra: dict[str, Any] = {"pricing": billing.pricing.snapshot()}
            if call.maintenance_message and call.config.maintenance.message is not None:
                extra[MAINTENANCE_MESSAGE_KEY] = call.config.maintenance.message
            new = NewCall(
                direction="inbound",
                status="ringing",
                queue_status="started",
                to_number=number.e164,
                from_number=call.caller,
                contact_id=contact_id,
            )
            row = call_row(call.call_id, org_id, config, new, extra)
            session.add(row)
            await session.flush()
            payload = call.payload
            await CallLegRepo(session, org_id).record_initiated(
                call.call_id,
                "primary",
                payload.call_control_id,
                call_leg_id=payload.call_leg_id,
                call_session_id=payload.call_session_id,
                telnyx_account_id=number.telnyx_account_id,
                started_at=payload.start_time or now,
            )
            ringing = new_event(
                EventType.CALL_RINGING,
                org_id=org_id,
                clock=self._clock,
                call_id=call.call_id,
                message=RINGING_MESSAGE,
                data={"direction": "inbound"},
            )
            event = await self._persist_event(session, ringing)
            carrier: dict[str, str] = {}
            propagate.inject(carrier)
            context = build_call_context(row, now=now, trace_context=carrier)
        return _Persisted(config=config, context=context, event=event)

    async def _persist_event(self, session: AsyncSession, event: DomainEvent) -> DomainEvent:
        """Sequence ``event`` (Redis), write its ``call_events`` row and route its webhook deliveries in
        ``session``'s transaction."""
        sequenced = await self._publisher.sequence(event)
        row = row_for(sequenced)
        await insert_rows(session, [] if row is None else [row])
        await self._publisher.route([sequenced], session=session)
        return sequenced

    async def _broadcast(self, event: DomainEvent) -> None:
        try:
            await self._publisher.broadcast(event)
        except EventPublishError:
            _log.warning("telnyx.inbound_event_broadcast_failed", event_type=event.type.value)

    async def _save_context(self, context: CallContext) -> None:
        """Store the context within :data:`CONTEXT_BUDGET_S`; a miss is logged (the media worker rebuilds
        it)."""
        try:
            saved = await within(self._contexts.save(context), self._clock, CONTEXT_BUDGET_S)
        except (RedisError, OSError) as exc:
            _log.warning("telnyx.inbound_context_skipped", reason=type(exc).__name__)
            return
        if not saved:
            _log.warning("telnyx.inbound_context_skipped", reason="timeout")

    async def _answer(self, call: InboundCall, config: SendCallConfig) -> None:
        settings = self._settings
        now = self._clock.now()
        token = mint_stream_token(
            call.call_id, stream_token_expiry(now, config.max_duration_min), settings.stream_secret
        )
        url = media_stream_url(settings.media_ws_scheme, settings.media_public_host, call.call_id, token)
        request = AnswerRequest.model_validate(
            {
                "client_state": encode(
                    ClientState(call_id=call.call_id, leg="primary", org_id=call.number.org_id)
                ),
                "command_id": answer_command_id(call.call_id),
                "stream": stream_params(url, hd_audio=config.hd_audio),
                **(RECORDING_FIELDS if config.record else {}),
            }
        )
        client = await self._telnyx.for_account(call.number.telnyx_account_id)
        try:
            await client.answer(call.payload.call_control_id, request)
        except CallAlreadyEnded:
            _log.info("telnyx.inbound_caller_gone_before_answer", call_id=str(call.call_id))
            return
        ring_to_answer_ms = (self._clock.monotonic() - call.received_monotonic) * 1000
        self._answer_ms.observe(ring_to_answer_ms)
        _log.info(
            "telnyx.inbound_answered",
            call_id=str(call.call_id),
            org_id=str(call.number.org_id),
            ring_to_answer_ms=round(ring_to_answer_ms, 1),
        )

    async def _cleanup(self, call: InboundCall, progress: _Progress) -> None:
        """Record the failed call and stop its workflow (nothing when nothing was committed); errors are
        logged and never mask the original one."""
        if not progress.committed:
            return
        try:
            await self._fail_call(call)
        except Exception as exc:  # the refusal must still go out
            _log.error(
                "telnyx.inbound_cleanup_failed",
                call_id=str(call.call_id),
                step="fail_call",
                error_type=type(exc).__name__,
            )
        if not progress.workflow_started:
            return
        try:
            await self._starter.terminate(call.call_id, CLEANUP_TERMINATE_REASON)
        except Exception as exc:  # the workflow's answer backstop ends it otherwise
            _log.error(
                "telnyx.inbound_cleanup_failed",
                call_id=str(call.call_id),
                step="terminate",
                error_type=type(exc).__name__,
            )

    async def _fail_call(self, call: InboundCall) -> None:
        org_id = call.number.org_id
        ended: DomainEvent | None = None
        async with self._database.tenant_session(org_id) as session:
            try:
                await CallRepo(session, org_id).transition(
                    call.call_id,
                    "failed",
                    from_={"ringing"},
                    queue_status="call_error",
                    **ended_values("error", self._clock.now(), ACCEPT_FAILED_MESSAGE),
                )
            except CallTransitionError as exc:
                _log.info("telnyx.inbound_cleanup_moved_on", call_id=str(call.call_id), status=exc.current)
            else:
                ended = await self._persist_event(
                    session, call_ended_event(self._clock, org_id, call.call_id, "error")
                )
        if ended is not None:
            await self._broadcast(ended)
