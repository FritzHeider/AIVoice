"""``end_call``: record an end that ``CallWorkflow`` decided (specs/05-calls-api.md §8, §9; specs/06 §6
Decision P05-T06b).

A call that never got answered moves to ``canceled`` (reason ``canceled``) or ``failed``; its credit hold is
released only when the call never reached Telnyx (``scheduled``/``queued``, or ``dialing`` without a primary
leg), otherwise post-call billing settles it. An answered call moves to ``completed`` with its connected and
transfer seconds; an ``end_reason`` the media worker already stored wins over the workflow's reason. The
workflow calls ``end_call`` before ``hangup_call``, so the carrier's hangup webhook finds ``end_reason`` set
and leaves the call alone (specs/06 §5 Decision P05-T02 item 7). The concurrency slot is released whenever
the call is terminal afterwards, also when it already was (a carrier-ended call) or a race ended it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity
from temporalio.exceptions import ApplicationError

from va_billing import holds
from va_billing.limits import CallLimiter
from va_common.logging import bound_context, get_logger
from va_common.time import Clock
from va_db.call_states import END_REASONS, EndReason, call_durations, is_terminal
from va_db.models import Call, CallStatus
from va_db.repositories import CallLegRepo, CallRepo, CallTransitionError
from va_db.session import Database
from va_domain.carrier import carrier_state_key
from va_events import DomainEvent, EventPublisher, EventType
from va_workflows.activities.errors import INVALID_END_REASON, STATE_UNAVAILABLE, retryable
from va_workflows.activities.events import broadcast_events, call_event, ended_event, persist_events
from va_workflows.activities.lookup import load_call
from va_workflows.activities.telephony import release_slot
from va_workflows.activities.telephony_inputs import PRE_ANSWER_ONLY_REASONS, EndCallInput, EndCallResult

__all__ = ["ANSWERED_REASON_MESSAGE", "CANCELED_DISPOSITION", "DISPATCH_BLOCKED_MESSAGE", "CallEndActivities"]

CANCELED_DISPOSITION: Final = "CANCELED"
DISPATCH_BLOCKED_MESSAGE: Final = "The call was blocked at dispatch."
ANSWERED_REASON_MESSAGE: Final = "This end reason cannot end an answered call."
STATE_UNAVAILABLE_MESSAGE: Final = "The call carrier state is unavailable."
_UNANSWERED: Final[frozenset[CallStatus]] = frozenset({"scheduled", "queued", "dialing", "ringing"})
_ANSWERED: Final[frozenset[CallStatus]] = frozenset({"in_progress", "transferring", "transferred"})
_NOT_DIALED: Final[frozenset[CallStatus]] = frozenset({"scheduled", "queued"})
_TRANSFER_STARTED_AT: Final = "transfer_started_at"

_log = get_logger(__name__)


def _stored_reason(value: str | None) -> EndReason | None:
    """``calls.end_reason`` as an :data:`EndReason` (``None`` when unset or not a known reason)."""
    for reason in END_REASONS:
        if reason == value:
            return reason
    return None


class CallEndActivities:
    """Records workflow-decided call ends; one instance per worker process."""

    def __init__(
        self,
        *,
        database: Database,
        limiter: CallLimiter,
        publisher: EventPublisher,
        redis: Redis,
        clock: Clock,
    ) -> None:
        self._database = database
        self._limiter = limiter
        self._publisher = publisher
        self._redis = redis
        self._clock = clock

    @activity.defn(name="end_call")
    async def end_call(self, request: EndCallInput) -> EndCallResult:
        with bound_context(org_id=str(request.org_id), call_id=str(request.call_id)):
            transfer_started_at = await self._transfer_started_at(request.call_id)
            async with self._database.tenant_session(request.org_id) as session:
                call = await load_call(session, request)
                unchanged: CallStatus | None = call.status if is_terminal(call.status) else None
                if unchanged is None:
                    try:
                        ended, reason = await self._end(session, call, request, transfer_started_at)
                    except CallTransitionError as exc:
                        _log.info("calls.end_unrecorded", end_reason=request.end_reason, status=exc.current)
                        unchanged = exc.current
                    else:
                        events = await persist_events(
                            session, self._publisher, self._events(call, request, reason)
                        )
            if unchanged is not None:
                return await self._unchanged(request, unchanged)
            await release_slot(self._limiter, request.org_id, request.call_id)
            await broadcast_events(self._publisher, events)
            _log.info("calls.ended", end_reason=reason, status=ended.status)
            return EndCallResult(changed=True, status=ended.status)

    async def _unchanged(self, request: EndCallInput, status: CallStatus) -> EndCallResult:
        """No change applied: release the slot anyway when the call has ended (ZREM is idempotent)."""
        if is_terminal(status):
            await release_slot(self._limiter, request.org_id, request.call_id)
        return EndCallResult(changed=False, status=status)

    async def _transfer_started_at(self, call_id: uuid.UUID) -> datetime | None:
        try:
            raw = await self._redis.hget(carrier_state_key(call_id), _TRANSFER_STARTED_AT)
        except (RedisError, OSError) as exc:
            raise retryable(STATE_UNAVAILABLE_MESSAGE, STATE_UNAVAILABLE) from exc
        if raw is None:
            return None
        return datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else str(raw))

    async def _end(
        self, session: AsyncSession, call: Call, request: EndCallInput, transfer_started_at: datetime | None
    ) -> tuple[Call, EndReason]:
        if call.status in _UNANSWERED:
            return await self._end_unanswered(session, call, request), request.end_reason
        if request.end_reason in PRE_ANSWER_ONLY_REASONS:
            raise ApplicationError(
                ANSWERED_REASON_MESSAGE, request.end_reason, type=INVALID_END_REASON, non_retryable=True
            )
        return await self._end_answered(session, call, request, transfer_started_at)

    async def _end_unanswered(self, session: AsyncSession, call: Call, request: EndCallInput) -> Call:
        reason = request.end_reason
        values: dict[str, Any] = self._ended_values(reason, request.error_message)
        target: CallStatus = "failed"
        if reason == "canceled":
            target = "canceled"
            values["disposition_tag"] = CANCELED_DISPOSITION
        else:
            values["queue_status"] = "call_error"
        never_dialed = call.status in _NOT_DIALED
        if call.status == "dialing":
            never_dialed = await CallLegRepo(session, call.org_id).primary(call.id) is None
        ended = await CallRepo(session, call.org_id).transition(
            call.id, target, from_={call.status}, **values
        )
        if never_dialed and call.credit_hold_id is not None:
            await holds.release(session, call.org_id, call.credit_hold_id)
        return ended

    async def _end_answered(
        self, session: AsyncSession, call: Call, request: EndCallInput, transfer_started_at: datetime | None
    ) -> tuple[Call, EndReason]:
        reason = _stored_reason(call.end_reason) or request.end_reason
        values = self._ended_values(reason, call.error_message or request.error_message)
        if call.answered_at is not None:
            values.update(call_durations(call.answered_at, values["ended_at"], transfer_started_at))
        ended = await CallRepo(session, call.org_id).transition(
            call.id, "completed", from_=_ANSWERED, **values
        )
        return ended, reason

    def _ended_values(self, reason: EndReason, error_message: str | None) -> dict[str, Any]:
        values: dict[str, Any] = {
            "end_reason": reason,
            "ended_by": END_REASONS[reason],
            "ended_at": self._clock.now(),
        }
        if error_message is not None:
            values["error_message"] = error_message
        return values

    def _events(self, call: Call, request: EndCallInput, reason: EndReason) -> list[DomainEvent]:
        events: list[DomainEvent] = []
        if reason == "dispatch_blocked":
            message = request.error_message or DISPATCH_BLOCKED_MESSAGE
            events.append(
                call_event(
                    self._clock,
                    call.org_id,
                    call.id,
                    EventType.CALL_DISPATCH_BLOCKED,
                    level="warning",
                    message=message,
                    data={"reason": request.code or "dispatch_blocked", "message": message},
                )
            )
        events.append(ended_event(self._clock, call.org_id, call.id, reason))
        return events
