"""The executor-backed system tools of task-mode calls: ``send_sms`` and ``schedule_callback`` (specs/13 §6;
Decision P08-T02b2 under specs/13 §6).

Both run behind the call's :class:`~va_tools.executor.CallToolExecutor` as
:class:`~va_tools.executor.SystemTool` objects; the runtime offers a tool to the model only when it built its
handler (:func:`sms_capable` and the org settings for ``send_sms``; the settings, a database and a workflow
starter for ``schedule_callback``).
Every run gets fresh message and callback ids (providers may omit or reuse tool call ids, and the brain never
re-runs a tool call), so two calls of a tool send two messages or schedule two callbacks. Numbers, texts and
times are never logged.

:class:`ToolSms` sends the message from the call's agent number to the other party through
:class:`~va_media.call_sms.OutboundSms` and publishes ``call.sms_sent`` (``source: "tool"``); a refusal
answers ``{"error": "sms_failed", "reason": ...}`` and reports ``call.error`` ``TOOL_FAILED``.

:class:`CallbackScheduler` checks the requested time (:func:`va_domain.call_callback.callback_time`), creates
the scheduled child call with its ``call.scheduled`` event row (routed to the webhook outbox) in one tenant
transaction (:func:`va_db.callbacks.schedule_callback`), broadcasts the event after the commit and starts the
child's ``CallWorkflow``, which admits the child at dial time like every scheduled call. An unusable time
answers ``{"error": "invalid_datetime", "reason": ...}`` without an error event (the model may correct it); a
start failure fails the child (``call.error`` ``dispatch_failed`` on the child) unless the workflow already
moved it on.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol
from zoneinfo import ZoneInfo

import orjson
from pydantic import JsonValue
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.checks import TemporalConnector, WorkflowStartError, start_workflow
from va_common.errors import AppError
from va_common.ids import new_id
from va_common.logging import get_logger
from va_common.time import Clock
from va_db.callbacks import ScheduledCallback, schedule_callback
from va_db.models import CallEventLevel
from va_db.repositories import CallRepo, CallTransitionError
from va_db.session import Database
from va_db.voicemail_sms import sms_sender
from va_domain.call_callback import CallbackTimeError, callback_time
from va_domain.call_context import CallContext
from va_domain.call_retry import iso_utc
from va_domain.call_workflow import CALL_WORKFLOW_NAME, CallWorkflowInput
from va_events import (
    SCHEDULED_MESSAGE,
    DomainEvent,
    EventPublisher,
    EventPublishError,
    EventType,
    insert_rows,
    new_event,
    row_for,
)
from va_llm.models import ToolCall
from va_media.call_sms import OutboundSms, SmsNotSent, agent_number, counterpart_number
from va_tools.executor import SystemOutcome, ToolEvents

__all__ = [
    "CALLBACK_START_FAILED",
    "CallbackDeps",
    "CallbackScheduler",
    "CallbackStartError",
    "CallbackStarter",
    "TemporalCallbackStarter",
    "ToolSms",
    "sms_capable",
]

CALLBACK_START_FAILED: Final = "Call workflow could not be started."
"""``calls.error_message`` and ``call.error`` message of a callback whose workflow could not be started."""

_START_TIMEOUT_S: Final = 10.0
_START_RPC_TIMEOUT_S: Final = 5.0

_log = get_logger(__name__)


async def sms_capable(database: Database, context: CallContext) -> bool:
    """Whether the call's agent number is an active SMS-capable number of the org (the ``send_sms``
    capability; ``False`` when it cannot be read, logged ``media.sms_capability_unavailable``)."""
    try:
        async with database.tenant_session(context.org_id) as session:
            sender = await sms_sender(session, context.org_id, agent_number(context))
    except (SQLAlchemyError, OSError) as exc:
        _log.warning("media.sms_capability_unavailable", error_type=type(exc).__name__)
        return False
    return sender is not None


def _content(body: dict[str, JsonValue]) -> str:
    return orjson.dumps(body).decode()


def _argument(call: ToolCall, name: str) -> str | None:
    """The string argument ``name`` of ``call`` (``None`` when absent or not a string)."""
    try:
        arguments = orjson.loads(call.arguments_json or "{}")
    except orjson.JSONDecodeError:
        return None
    value = arguments.get(name) if isinstance(arguments, dict) else None
    return value if isinstance(value, str) else None


class ToolSms:
    """The ``send_sms`` tool of one call (see the module docstring)."""

    name: Final = "send_sms"

    def __init__(self, sender: OutboundSms, context: CallContext, events: ToolEvents | None) -> None:
        self._sender = sender
        self._context = context
        self._events = events

    async def run(self, call: ToolCall) -> SystemOutcome:
        context = self._context
        text = (_argument(call, "message") or "").strip()
        if not text:
            return SystemOutcome(
                _content({"error": "sms_failed", "reason": "blank_message"}), "blank_message"
            )
        try:
            sent = await self._sender.deliver(
                context,
                sender_number=agent_number(context),
                to=counterpart_number(context),
                text=text,
                message_id=new_id(),
            )
        except SmsNotSent as refused:
            _log.warning(refused.event, reason=refused.reason)
            return SystemOutcome(_content({"error": "sms_failed", "reason": refused.reason}), refused.reason)
        if self._events is not None:
            self._events.sms_sent(
                {
                    "message_id": str(sent.message_id),
                    "conversation_id": str(sent.conversation_id),
                    "segments": sent.segments,
                    "source": "tool",
                }
            )
        _log.info("media.tool_sms_sent", segments=sent.segments)
        return SystemOutcome(_content({"status": "sent", "segments": sent.segments}))


class CallbackStartError(Exception):
    """The callback's ``CallWorkflow`` could not be started (cause chained)."""


class CallbackStarter(Protocol):
    """Starts the ``CallWorkflow`` of a committed callback; raises :class:`CallbackStartError`."""

    async def start(self, call: CallWorkflowInput) -> None: ...


class TemporalCallbackStarter:
    """Starts ``CallWorkflow`` (``REJECT_DUPLICATE``; an existing workflow counts as started) on
    ``task_queue`` through the media worker's lazily connected Temporal client."""

    def __init__(self, connector: TemporalConnector, task_queue: str) -> None:
        self._connector = connector
        self._task_queue = task_queue

    async def start(self, call: CallWorkflowInput) -> None:
        try:
            await start_workflow(
                self._connector,
                CALL_WORKFLOW_NAME,
                call.model_dump(mode="json"),
                workflow_id=call.workflow_id(),
                task_queue=self._task_queue,
                timeout_s=_START_TIMEOUT_S,
                rpc_timeout_s=_START_RPC_TIMEOUT_S,
            )
        except WorkflowStartError as exc:
            msg = f"CallWorkflow of callback {call.call_id} could not be started"
            raise CallbackStartError(msg) from exc.__cause__


class _EventsUnavailableError(Exception):
    """The callback's event could not be sequenced; its transaction rolls back."""


@dataclass(frozen=True, slots=True)
class CallbackDeps:
    """The per-process collaborators of callbacks: the publisher routes to the webhook outbox."""

    database: Database
    publisher: EventPublisher
    starter: CallbackStarter
    clock: Clock


class CallbackScheduler:
    """The ``schedule_callback`` tool of one call (see the module docstring)."""

    name: Final = "schedule_callback"

    def __init__(self, deps: CallbackDeps, context: CallContext) -> None:
        self._deps = deps
        self._context = context

    async def run(self, call: ToolCall) -> SystemOutcome:
        context = self._context
        deps = self._deps
        try:
            moment = callback_time(
                _argument(call, "datetime_iso") or "", context.config.timezone, deps.clock.now()
            )
        except CallbackTimeError as exc:
            _log.info("media.callback_refused", reason=exc.reason)
            return SystemOutcome(_content({"error": "invalid_datetime", "reason": exc.reason}))
        try:
            scheduled, event = await self._create(call, moment)
        except (SQLAlchemyError, OSError, AppError, _EventsUnavailableError) as exc:
            failure = "events" if isinstance(exc, _EventsUnavailableError) else "database"
            _log.warning("media.callback_failed", failure=failure, error_type=type(exc).__name__)
            return SystemOutcome(_content({"error": "callback_failed"}), failure)
        if event is not None:
            await self._broadcast(event)
            if not await self._start(scheduled):
                return SystemOutcome(_content({"error": "callback_failed"}), "workflow_start")
        _log.info("media.callback_scheduled", created=scheduled.created)
        local = scheduled.scheduled_for.astimezone(ZoneInfo(context.config.timezone))
        return SystemOutcome(
            _content(
                {
                    "status": "scheduled",
                    "scheduled_call_id": str(scheduled.call_id),
                    "scheduled_for": local.isoformat(),
                }
            )
        )

    async def _create(self, call: ToolCall, moment: datetime) -> tuple[ScheduledCallback, DomainEvent | None]:
        """The callback child (created, or found when its id already exists) and, when created, its committed
        event."""
        context = self._context
        deps = self._deps
        async with deps.database.tenant_session(context.org_id) as session:
            scheduled = await schedule_callback(
                session,
                context.org_id,
                context.call_id,
                child_id=new_id(),
                scheduled_for=moment,
                note=_argument(call, "note"),
            )
            if not scheduled.created:
                return scheduled, None
            data: dict[str, JsonValue] = {
                "status": "scheduled",
                "scheduled_for": iso_utc(scheduled.scheduled_for),
                "warnings": [],
                "parent_call_id": str(context.call_id),
                "source": "callback",
            }
            event = await self._persist(
                session, scheduled.call_id, EventType.CALL_SCHEDULED, SCHEDULED_MESSAGE, data
            )
        return scheduled, event

    async def _persist(
        self,
        session: AsyncSession,
        call_id: uuid.UUID,
        event_type: EventType,
        message: str,
        data: dict[str, JsonValue],
    ) -> DomainEvent:
        """The sequenced event of call ``call_id``, its row inserted and routed in ``session``'s
        transaction."""
        deps = self._deps
        level: CallEventLevel = "error" if event_type is EventType.CALL_ERROR else "info"
        event = new_event(
            event_type,
            org_id=self._context.org_id,
            clock=deps.clock,
            call_id=call_id,
            level=level,
            message=message,
            data=data,
        )
        try:
            sequenced = await deps.publisher.sequence(event)
        except EventPublishError as exc:
            raise _EventsUnavailableError from exc
        await insert_rows(session, [row for row in (row_for(sequenced),) if row is not None])
        await deps.publisher.route([sequenced], session=session)
        return sequenced

    async def _broadcast(self, event: DomainEvent) -> None:
        try:
            await self._deps.publisher.broadcast(event)
        except (EventPublishError, OSError):
            _log.warning("media.callback_event_broadcast_failed", event_type=event.type.value)

    async def _start(self, scheduled: ScheduledCallback) -> bool:
        """Start the child's workflow; ``False`` when it failed and the child was marked failed."""
        context = self._context
        try:
            await self._deps.starter.start(
                CallWorkflowInput(
                    call_id=scheduled.call_id, org_id=context.org_id, scheduled_for=scheduled.scheduled_for
                )
            )
        except CallbackStartError as exc:
            _log.warning("media.callback_start_failed", error_type=type(exc.__cause__).__name__)
            return not await self._fail_child(scheduled.call_id)
        return True

    async def _fail_child(self, child_id: uuid.UUID) -> bool:
        """Mark the child failed with its ``call.error``; ``False`` when the workflow already moved it on
        (a timed-out start that reached Temporal) or the failure could not be recorded."""
        context = self._context
        deps = self._deps
        event: DomainEvent | None = None
        try:
            async with deps.database.tenant_session(context.org_id) as session:
                await CallRepo(session, context.org_id).transition(
                    child_id,
                    "failed",
                    from_={"scheduled"},
                    error_message=CALLBACK_START_FAILED,
                    end_reason="error",
                    ended_by="error",
                    ended_at=deps.clock.now(),
                    queue_status="call_error",
                )
                data: dict[str, JsonValue] = {"code": "dispatch_failed", "detail": "workflow_start"}
                try:
                    event = await self._persist(
                        session, child_id, EventType.CALL_ERROR, CALLBACK_START_FAILED, data
                    )
                except _EventsUnavailableError:
                    event = None  # the failure itself must still be recorded; the event is lost with Redis
        except CallTransitionError as exc:
            _log.info("media.callback_moved_on", status=exc.current)
            return False
        except (SQLAlchemyError, OSError) as exc:
            _log.warning("media.callback_failure_unrecorded", error_type=type(exc).__name__)
            return True
        if event is not None:
            await self._broadcast(event)
        return True
