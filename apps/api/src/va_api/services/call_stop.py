"""Stopping calls: ``POST /v1/calls/{call_id}/stop`` and ``POST /v1/calls/active/stop`` (specs/05-calls-api.md
§7.2, §7.3; Decision P05-T11c).

The call's status (read in a short ``tenant_session``) picks the action:

- ``scheduled``/``queued``/``dialing``/``ringing`` → **cancel**: ``signal_cancel`` on ``call-<id>``. The
  workflow owns the leg: it hangs up a ringing leg with its own ``command_id`` and records ``canceled``
  through ``end_call`` (hold and slot released). When no workflow runs (stopped between the commit and the
  start, or the workflow is already closing) a ``scheduled``/``queued`` call is canceled here in one
  transaction (row, hold, ``call.ended`` row, audit row), then the event is broadcast and the slot released;
  a call that ended meanwhile is ``CALL_NOT_ACTIVE``, any other (dialing, ringing or answered without a
  reachable workflow) is ``SERVICE_UNAVAILABLE`` (a retry re-reads the status).
- ``in_progress``/``transferring`` → **stop**: a ``stop`` control message (reason ``api``) on
  ``call:{<id>}:control``; the owning media worker hangs up at once without a goodbye line (specs/07 §17). No
  ack is awaited; a message nobody heard is logged (the heartbeat watch is the backstop).
- ``transferred`` and every terminal status → ``CALL_NOT_ACTIVE``.

The audit row ``call.stop`` is written after the side effect succeeded. Stop-all runs the single-call stop for
each stoppable call with bounded concurrency; a call that ended meanwhile is skipped, one that could not be
stopped is logged and skipped.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal, Protocol

from fastapi import Request
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.service import RPCError

from va_api.audit import audit
from va_api.auth.context import RequestContext
from va_api.services.call_event_rows import broadcast_event, persist_event
from va_billing import holds
from va_billing.limits import CallLimiter
from va_common.checks import TemporalConnector, signal_workflow
from va_common.errors import AppError
from va_common.ids import new_id
from va_common.logging import get_logger
from va_common.time import Clock
from va_db.call_end import ended_values
from va_db.call_states import is_terminal
from va_db.models import CallStatus
from va_db.repositories import CallRepo, CallTransitionError, NotFoundError
from va_db.session import Database
from va_domain.call_workflow import CANCEL_SIGNAL, call_workflow_id
from va_domain.control import StopControl, control_channel
from va_events import DomainEvent, EventPublisher, call_ended_event

__all__ = [
    "CALL_NOT_FOUND_MESSAGE",
    "CANCELABLE_STATUSES",
    "CANCELED_DISPOSITION",
    "CANCELED_MESSAGE",
    "NOT_ACTIVE_MESSAGE",
    "SIGNAL_TIMEOUT_S",
    "STOPPED_MESSAGE",
    "STOP_ALL_CONCURRENCY",
    "STOP_ALL_ROLE_MESSAGE",
    "STOP_FAILED_MESSAGE",
    "CallCancelError",
    "CallCanceller",
    "StopAction",
    "StopDeps",
    "StopOutcome",
    "StoppedCalls",
    "TemporalCallCanceller",
    "check_stop_all_role",
    "parse_call_id",
    "stop_active_calls",
    "stop_call",
    "stop_deps_of",
]

CANCELED_MESSAGE: Final = "Call canceled."
STOPPED_MESSAGE: Final = "Call ended successfully."
NOT_ACTIVE_MESSAGE: Final = "The call has already ended."
CALL_NOT_FOUND_MESSAGE: Final = "Call not found."
STOP_FAILED_MESSAGE: Final = "The call could not be stopped; retry."
STOP_ALL_ROLE_MESSAGE: Final = "Stopping all active calls requires the admin role."
CANCELED_DISPOSITION: Final = "CANCELED"
SIGNAL_TIMEOUT_S: Final = 5.0
STOP_ALL_CONCURRENCY: Final = 10

CANCELABLE_STATUSES: Final[frozenset[CallStatus]] = frozenset({"scheduled", "queued", "dialing", "ringing"})
"""Not answered yet: ``CallWorkflow`` honours ``signal_cancel`` (specs/01 §2.1 Decision P05-T06c)."""

_ANSWERED: Final[frozenset[CallStatus]] = frozenset({"in_progress", "transferring"})
_NOT_DIALED: Final[frozenset[CallStatus]] = frozenset({"scheduled", "queued"})
_STOP_ALL_ROLES: Final = frozenset({"owner", "admin"})

type StopAction = Literal["cancel", "stop"]

_log = get_logger(__name__)


class CallCancelError(Exception):
    """``signal_cancel`` could not be delivered (Temporal unreachable, refused, or too slow)."""


class CallCanceller(Protocol):
    """Sends ``signal_cancel`` to a call's workflow; ``False`` when no such workflow runs."""

    async def cancel(self, call_id: uuid.UUID) -> bool: ...


class TemporalCallCanceller:
    """Signals ``CallWorkflow`` through ``connector``; connect and signal share ``timeout_s``."""

    def __init__(self, connector: TemporalConnector, *, timeout_s: float = SIGNAL_TIMEOUT_S) -> None:
        self._connector = connector
        self._timeout_s = timeout_s

    async def cancel(self, call_id: uuid.UUID) -> bool:
        try:
            async with asyncio.timeout(self._timeout_s):
                return await signal_workflow(self._connector, call_workflow_id(call_id), CANCEL_SIGNAL)
        except (RPCError, TimeoutError, OSError, RuntimeError) as exc:
            msg = f"CallWorkflow of call {call_id} could not be signalled"
            raise CallCancelError(msg) from exc


@dataclass(frozen=True, slots=True)
class StopDeps:
    """What stopping a call needs from the container."""

    database: Database
    redis: Redis
    limiter: CallLimiter
    publisher: EventPublisher
    canceller: CallCanceller
    clock: Clock


@dataclass(frozen=True, slots=True)
class StopOutcome:
    call_id: uuid.UUID
    action: StopAction
    message: str


@dataclass(frozen=True, slots=True)
class StoppedCalls:
    """The calls stop-all stopped, in processing order (oldest first)."""

    call_ids: list[uuid.UUID]

    @property
    def message(self) -> str:
        return f"Stopped {len(self.call_ids)} calls."


def stop_deps_of(request: Request) -> StopDeps:
    """The stop dependencies of the running app."""
    container = request.app.state.container
    return StopDeps(
        database=container.database,
        redis=container.redis,
        limiter=container.call_limiter,
        publisher=container.event_publisher,
        canceller=container.call_canceller,
        clock=container.clock,
    )


def parse_call_id(value: str) -> uuid.UUID:
    """The path value as a call id; anything that is not a UUID is ``CALL_NOT_FOUND`` (specs/05 §1)."""
    try:
        return uuid.UUID(value)
    except ValueError:
        raise AppError("CALL_NOT_FOUND", CALL_NOT_FOUND_MESSAGE) from None


def check_stop_all_role(ctx: RequestContext) -> None:
    """Dashboard sessions need role ``owner``/``admin`` to stop every call (specs/05 §7.3); keys do not."""
    if ctx.principal_type == "user" and ctx.role not in _STOP_ALL_ROLES:
        raise AppError("FORBIDDEN", STOP_ALL_ROLE_MESSAGE)


async def stop_call(deps: StopDeps, ctx: RequestContext, call_id: uuid.UUID) -> StopOutcome:
    """Stop one call of ``ctx.org_id``; ``CALL_NOT_FOUND``, ``CALL_NOT_ACTIVE`` or ``SERVICE_UNAVAILABLE``."""
    async with deps.database.tenant_session(ctx.org_id) as session:
        try:
            status = (await CallRepo(session, ctx.org_id).get(call_id)).status
        except NotFoundError:
            raise AppError("CALL_NOT_FOUND", CALL_NOT_FOUND_MESSAGE) from None
    return await _stop(deps, ctx, call_id, status)


async def stop_active_calls(
    deps: StopDeps, ctx: RequestContext, call_ids: Sequence[uuid.UUID] | None
) -> StoppedCalls:
    """Stop every stoppable call of ``ctx.org_id`` (only ``call_ids`` when given; foreign ids are ignored).

    ``SERVICE_UNAVAILABLE`` when calls could not be stopped and none was.
    """
    wanted = None if call_ids is None else list(dict.fromkeys(call_ids))
    async with deps.database.tenant_session(ctx.org_id) as session:
        targets = await CallRepo(session, ctx.org_id).active_ids(wanted)
    gate = asyncio.Semaphore(STOP_ALL_CONCURRENCY)

    async def attempt(call_id: uuid.UUID, status: CallStatus) -> bool | None:
        async with gate:
            try:
                await _stop(deps, ctx, call_id, status)
            except AppError as exc:
                if exc.code == "CALL_NOT_ACTIVE":
                    return None
                _log.warning("calls.stop_failed", call_id=str(call_id), code=exc.code)
                return False
            return True

    async with asyncio.TaskGroup() as group:
        tasks = [(call_id, group.create_task(attempt(call_id, status))) for call_id, status in targets]
    results = [(call_id, task.result()) for call_id, task in tasks]
    stopped = [call_id for call_id, result in results if result]
    if not stopped and any(result is False for _, result in results):
        raise AppError("SERVICE_UNAVAILABLE", STOP_FAILED_MESSAGE)
    return StoppedCalls(call_ids=stopped)


async def _stop(deps: StopDeps, ctx: RequestContext, call_id: uuid.UUID, status: CallStatus) -> StopOutcome:
    if status in CANCELABLE_STATUSES:
        if await _signal_cancel(deps.canceller, call_id):
            await _audit(deps, ctx, call_id, status, "cancel")
        else:
            await _cancel_without_workflow(deps, ctx, call_id, status)
        return StopOutcome(call_id=call_id, action="cancel", message=CANCELED_MESSAGE)
    if status in _ANSWERED:
        await _publish_stop(deps.redis, call_id)
        await _audit(deps, ctx, call_id, status, "stop")
        return StopOutcome(call_id=call_id, action="stop", message=STOPPED_MESSAGE)
    raise AppError("CALL_NOT_ACTIVE", NOT_ACTIVE_MESSAGE)


async def _signal_cancel(canceller: CallCanceller, call_id: uuid.UUID) -> bool:
    try:
        return await canceller.cancel(call_id)
    except CallCancelError as exc:
        _log.warning("calls.cancel_signal_failed", call_id=str(call_id), error=str(exc.__cause__ or exc))
        raise AppError("SERVICE_UNAVAILABLE", STOP_FAILED_MESSAGE) from exc


async def _publish_stop(redis: Redis, call_id: uuid.UUID) -> None:
    message = StopControl(id=new_id(), reason="api")
    try:
        receivers = await redis.publish(control_channel(call_id), message.model_dump_json())
    except (RedisError, OSError) as exc:
        _log.warning("calls.stop_publish_failed", call_id=str(call_id), error=type(exc).__name__)
        raise AppError("SERVICE_UNAVAILABLE", STOP_FAILED_MESSAGE) from exc
    if receivers == 0:
        _log.warning("calls.stop_unheard", call_id=str(call_id))


async def _audit(
    deps: StopDeps, ctx: RequestContext, call_id: uuid.UUID, status: CallStatus, action: StopAction
) -> None:
    async with deps.database.tenant_session(ctx.org_id) as session:
        await _audit_in(session, ctx, call_id, status, action)


async def _audit_in(
    session: AsyncSession, ctx: RequestContext, call_id: uuid.UUID, status: CallStatus, action: StopAction
) -> None:
    await audit(
        session, ctx, "call.stop", "call", call_id, before={"status": status}, after={"action": action}
    )


async def _cancel_without_workflow(
    deps: StopDeps, ctx: RequestContext, call_id: uuid.UUID, status: CallStatus
) -> None:
    """Cancel a call no workflow runs for (specs/05 §7.3 Decision P05-T11c)."""
    org_id = ctx.org_id
    event: DomainEvent | None
    async with deps.database.tenant_session(org_id) as session:
        try:
            call = await CallRepo(session, org_id).transition(
                call_id,
                "canceled",
                from_=_NOT_DIALED,
                disposition_tag=CANCELED_DISPOSITION,
                **ended_values("canceled", deps.clock.now(), None),
            )
        except CallTransitionError as exc:
            if is_terminal(exc.current) or exc.current == "transferred":
                raise AppError("CALL_NOT_ACTIVE", NOT_ACTIVE_MESSAGE) from None
            _log.warning("calls.cancel_without_workflow", call_id=str(call_id), status=exc.current)
            raise AppError("SERVICE_UNAVAILABLE", STOP_FAILED_MESSAGE) from None
        if call.credit_hold_id is not None:
            await holds.release(session, org_id, call.credit_hold_id)
        try:
            event = await persist_event(
                session, deps.publisher, call_ended_event(deps.clock, org_id, call_id, "canceled")
            )
        except AppError:
            event = None  # the cancellation itself must still be recorded; the event is lost with Redis
        await _audit_in(session, ctx, call_id, status, "cancel")
    if event is not None:
        await broadcast_event(deps.publisher, event)
    try:
        await deps.limiter.release_call(org_id, call_id)
    except (RedisError, OSError) as exc:
        _log.warning("calls.slot_release_failed", call_id=str(call_id), error=type(exc).__name__)
