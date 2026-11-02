"""The end-of-call routine of the media worker (specs/07-media-pipeline.md §18, §19; specs/05 §8, §9; specs/07
§18 Decision P05-T11b).

:class:`CallEnd` runs once per session, after the session race ended and before the session's cleanup
releases the ownership lock and deletes the heartbeat (so the edge still sees an owner and leaves the call row
alone, specs/06 §5 Decision P05-T02 item 7). Steps, in order:

1. **Resolve** the end (:class:`ResolvedEnd`): a failed body is ``error``; an ownership loss or the drain
   timeout (``worker_lost``) neither records nor hangs up (``CallWorkflow`` does both); a closed media stream
   waits up to 3 s for the carrier's hangup (the carrier state hash first, then the call's carrier channel):
   hung up → ``max_duration`` (``hangup_cause`` ``time_limit``), ``transferred`` (call status
   ``transferred``) or ``user_hangup``, without a hangup; not hung up → ``error`` and a hangup. Any other end
   keeps its reason and hangs up.
2. **Record** the end on the ``calls`` row in one tenant transaction (answered calls only, through
   :func:`va_db.call_end.record_answered_end`: a stored reason wins). A terminal call is left alone; an
   unanswered one is left to the workflow's cleanup.
3. **Hang up** the primary leg (after the record, so the edge's hangup webhook finds ``end_reason`` set) with
   the workflow's ``command_id``, so Telnyx dedupes a second hangup.
4. **Events**: ``call.ended`` when this routine recorded the end, then the call's event rows are flushed.
5. **Slot**: the org's concurrency slot is released (``ZREM``, idempotent).
6. **Signal** ``CallWorkflow.signal_pipeline_finished`` with the end reason.

Every step logs its failure (error types only, never transcript text or numbers) and the next step still runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from va_billing.limits import CallLimiter
from va_common.crypto import CryptoError
from va_common.logging import get_logger
from va_common.time import Clock
from va_db.call_end import ANSWERED_STATUSES, record_answered_end
from va_db.call_states import END_REASONS, EndReason
from va_db.models import Call
from va_db.repositories import CallLegRepo, CallRepo, CallTransitionError
from va_db.session import Database
from va_domain.call_workflow import PipelineFinished
from va_events import EventPublishError, call_ended_event
from va_media.carrier_state import CarrierSnapshot, read_carrier_state
from va_media.channels import CallFeed
from va_media.session import CallSession, SessionEnd
from va_media.turns import CallFlusher, EventSink
from va_media.workflow_signal import PipelineSignaller
from va_telnyx import TelnyxClientFactory, UnknownTelnyxAccount
from va_telnyx.client_state import ClientState, command_id, encode
from va_telnyx.errors import TelnyxError
from va_voice.pipeline.waits import within

__all__ = [
    "HANGUP_EVENT",
    "PIPELINE_FAILED_MESSAGE",
    "STREAM_CLOSED_WAIT_S",
    "STREAM_LOST_MESSAGE",
    "CallEnd",
    "ResolvedEnd",
]

STREAM_CLOSED_WAIT_S: Final = 3.0
"""How long a closed media stream waits for the carrier's hangup before it hangs up itself (specs/07 §19)."""
STREAM_LOST_MESSAGE: Final = "The media stream closed while the call was up."
PIPELINE_FAILED_MESSAGE: Final = "The call pipeline failed."
HANGUP_EVENT: Final = "call.hangup"
_TIME_LIMIT: Final = "time_limit"

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ResolvedEnd:
    """How the routine ends the call: the reason, whether it hangs up and records, and (``carrier``) whether
    the carrier already ended the call (``ended_at`` then comes from the primary leg)."""

    reason: EndReason
    hang_up: bool
    record: bool
    error_message: str | None = None
    carrier: bool = False


_WORKER_LOST: Final = ResolvedEnd("worker_lost", hang_up=False, record=False)
_BODY_FAILED: Final = ResolvedEnd("error", hang_up=True, record=True, error_message=PIPELINE_FAILED_MESSAGE)
_STREAM_LOST: Final = ResolvedEnd("error", hang_up=True, record=True, error_message=STREAM_LOST_MESSAGE)


def _known_reason(value: str | None) -> EndReason:
    """A session end reason as an :data:`EndReason` (``error`` for anything unknown)."""
    for reason in END_REASONS:
        if reason == value:
            return reason
    _log.error("media.unknown_end_reason", end_reason=value)
    return "error"


def carrier_end(hangup_cause: str | None) -> ResolvedEnd:
    """The end after the carrier hung up the primary leg (``transferred`` is decided at record time)."""
    reason: EndReason = "max_duration" if hangup_cause == _TIME_LIMIT else "user_hangup"
    return ResolvedEnd(reason, hang_up=False, record=True, carrier=True)


async def _primary_hangup(feed: CallFeed) -> str:
    """Read carrier events until the primary leg's ``call.hangup``; returns its ``hangup_cause`` ('' when
    absent)."""
    while True:
        event = await feed.next_carrier()
        if event.type == HANGUP_EVENT and event.leg == "primary":
            cause = event.payload.get("hangup_cause")
            return cause if isinstance(cause, str) else ""


class CallEnd:
    """The end-of-call routine of one media-worker process (see the module docstring)."""

    def __init__(  # noqa: PLR0913 - per-process collaborators, all keyword-only
        self,
        *,
        database: Database,
        redis: Redis,
        signaller: PipelineSignaller,
        clock: Clock,
        telnyx: TelnyxClientFactory | None = None,
        events: EventSink | None = None,
        event_rows: CallFlusher | None = None,
    ) -> None:
        self._database = database
        self._redis = redis
        self._signaller = signaller
        self._clock = clock
        self._telnyx = telnyx
        self._events = events
        self._event_rows = event_rows
        self._limiter = CallLimiter(redis, clock=clock)

    async def finish(self, session: CallSession, end: SessionEnd | None) -> None:
        state = await self._carrier_state(session)
        resolved = await self.resolve(session, end, state)
        reason = resolved.reason
        recorded: EndReason | None = None
        if resolved.record:
            recorded = await self._record(session, resolved, state)
        if resolved.hang_up:
            await self._hang_up(session)
        if recorded is not None:
            reason = recorded
            await self._publish_ended(session, recorded)
        await self._flush(session.call_id)
        await self._release_slot(session)
        _log.info(
            "media.call_end", end_reason=reason, recorded=recorded is not None, hung_up=resolved.hang_up
        )
        await self._signaller.pipeline_finished(
            session.call_id, PipelineFinished(end_reason=reason, ended_by=END_REASONS[reason])
        )

    async def resolve(
        self, session: CallSession, end: SessionEnd | None, state: CarrierSnapshot
    ) -> ResolvedEnd:
        """Step 1 (``state``: the carrier state read at the end)."""
        if end is None:
            return _BODY_FAILED
        if end.end_reason == "worker_lost":
            return _WORKER_LOST
        if end.cause == "stream_closed":
            return await self._after_stream_closed(session.feed, state)
        return ResolvedEnd(_known_reason(end.end_reason), hang_up=True, record=True)

    async def _carrier_state(self, session: CallSession) -> CarrierSnapshot:
        """The carrier state hash now (the attach snapshot when Redis fails)."""
        try:
            return await read_carrier_state(self._redis, session.call_id)
        except (RedisError, OSError) as exc:
            _log.warning("media.carrier_state_unavailable", error_type=type(exc).__name__)
            return session.carrier_state

    async def _after_stream_closed(self, feed: CallFeed, state: CarrierSnapshot) -> ResolvedEnd:
        """The edge stores a hangup in the hash before it publishes it, so a hangup the carrier watch already
        consumed is in ``state``; otherwise wait for it on the channel (this is its only reader now)."""
        if state.primary_hung_up:
            return carrier_end(state.hangup_cause)
        causes: list[str] = []

        async def wait() -> None:
            causes.append(await _primary_hangup(feed))

        if await within(wait(), self._clock, STREAM_CLOSED_WAIT_S):
            return carrier_end(causes[0] or None)
        _log.warning("media.stream_lost", waited_s=STREAM_CLOSED_WAIT_S)
        return _STREAM_LOST

    async def _record(
        self, session: CallSession, resolved: ResolvedEnd, state: CarrierSnapshot
    ) -> EndReason | None:
        """Step 2; the reason recorded, or ``None`` when nothing was written."""
        context = session.context
        try:
            async with self._database.tenant_session(context.org_id) as db:
                call = await CallRepo(db, context.org_id).get(context.call_id)
                if call.status not in ANSWERED_STATUSES:
                    _log.info("media.end_not_recorded", status=call.status)
                    return None
                reason = resolved.reason
                if resolved.carrier and reason == "user_hangup" and call.status == "transferred":
                    reason = "transferred"
                _, recorded = await record_answered_end(
                    db,
                    call,
                    reason,
                    ended_at=await self._ended_at(db, call, resolved),
                    transfer_started_at=state.transfer_started_at,
                    error_message=resolved.error_message,
                )
        except CallTransitionError as exc:
            _log.info("media.end_unrecorded", status=exc.current)
            return None
        except (SQLAlchemyError, OSError) as exc:
            _log.error("media.end_record_failed", error_type=type(exc).__name__)
            return None
        return recorded

    async def _ended_at(self, db: AsyncSession, call: Call, resolved: ResolvedEnd) -> datetime:
        """The primary leg's carrier ``ended_at`` after a carrier hangup, else now."""
        if resolved.carrier:
            leg = await CallLegRepo(db, call.org_id).primary(call.id)
            if leg is not None and leg.ended_at is not None:
                return leg.ended_at
        return self._clock.now()

    async def _hang_up(self, session: CallSession) -> None:
        """Step 3; ``False`` from Telnyx (already ended) is fine."""
        if self._telnyx is None:
            _log.warning("media.hangup_unavailable")
            return
        context = session.context
        state = encode(ClientState(call_id=context.call_id, leg="primary", org_id=context.org_id))
        try:
            client = await self._telnyx.for_account(context.config.telnyx_account_id)
            sent = await client.hangup(
                session.start.call_control_id,
                command_id=command_id("hangup", context.call_id, "primary"),
                client_state=state,
            )
        except (TelnyxError, UnknownTelnyxAccount, CryptoError, SQLAlchemyError, OSError) as exc:
            _log.warning("media.hangup_failed", error_type=type(exc).__name__)
            return
        _log.info("media.hangup_sent", result="sent" if sent else "already_ended")

    async def _publish_ended(self, session: CallSession, reason: EndReason) -> None:
        if self._events is None:
            return
        event = call_ended_event(self._clock, session.context.org_id, session.call_id, reason)
        try:
            await self._events.publish(event)
        except EventPublishError:
            _log.warning("media.event_publish_failed", event_type=event.type.value)

    async def _flush(self, call_id: uuid.UUID) -> None:
        if self._event_rows is None:
            return
        try:
            await self._event_rows.flush_call(call_id)
        except Exception as exc:  # the buffer keeps the rows for its periodic flush; the end must go on
            _log.warning("media.call_flush_failed", target="call_events", error=type(exc).__name__)

    async def _release_slot(self, session: CallSession) -> None:
        try:
            await self._limiter.release_call(session.context.org_id, session.call_id)
        except (RedisError, OSError) as exc:
            _log.warning("media.slot_release_failed", error_type=type(exc).__name__)
