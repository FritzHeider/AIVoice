"""Cold transfer of an attached call (specs/06-telephony-telnyx.md §10.1; specs/01 §2; specs/06 §10.1 Decision
P05-T13).

:class:`ColdTransfer` runs one ``transfer_call`` action after the Brain's transfer line has played (the
pipeline speaks it before it hands the action over, so the mark echo of its last segment has arrived):

1. the call moves ``in_progress`` → ``transferring`` (:class:`TransferStatusWriter`); a call in any other
   status is ``not_active`` and nothing is sent (a row that cannot be written is ``command_failed``);
2. ``call.transfer_started`` (``data.type = "cold"``, ``data.target``) is published;
3. Telnyx ``transfer`` on the primary leg: ``to`` = the target, ``from`` = :func:`transfer_caller_id`
   of the org's ``transfer_caller_id_mode``, ``timeout_secs`` = 30, ``time_limit_secs`` =
   :func:`time_limit_s` of the call's remaining duration, ``target_leg_client_state`` naming the
   ``transfer_target`` leg (so the edge attributes the target's webhooks),
   ``command_id("transfer", call_id, "primary", n)`` with ``n`` the transfer of the call;
4. the outcome comes from the carrier events of the ``transfer_target`` leg
   (:attr:`~va_media.carrier_watch.CarrierWatch.transfer_events`) within :data:`TRANSFER_TIMEOUT_S` +
   :data:`TRANSFER_OUTCOME_GRACE_S`: ``call.bridged`` → ``bridged`` (the edge records ``transferred``
   and emits ``call.transferred``); the target's ``call.hangup`` → ``target_hangup``; nothing →
   ``timeout``; a refused command → ``command_failed``. Every failure moves the call back to
   ``in_progress``.

The media stream stays up while the target rings (the pipeline waits inside the action, so it neither replies
nor runs the no-input timer); it stops only after the bridge (the end-of-call routine's ``streaming_stop``).
The target number and caller id are never logged.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from datetime import datetime
from typing import Final, Literal, Protocol

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from sqlalchemy.exc import SQLAlchemyError

from va_common.logging import get_logger
from va_common.time import Clock
from va_common.waits import within
from va_db.repositories import CallRepo, CallTransitionError
from va_db.session import Database
from va_domain.call_context import CallContext
from va_domain.carrier import CarrierEvent
from va_events import EventPublishError, EventType, new_event
from va_media.turns import EventSink
from va_telnyx.client_state import ClientState, command_id, encode
from va_telnyx.errors import TelnyxError
from va_telnyx.models import CommandResult, TransferRequest

__all__ = [
    "MAX_TIME_LIMIT_S",
    "MIN_TIME_LIMIT_S",
    "TRANSFER_OUTCOME_GRACE_S",
    "TRANSFER_STARTED_MESSAGE",
    "TRANSFER_TIMEOUT_S",
    "CallerIdMode",
    "ColdTransfer",
    "DbTransferStatus",
    "StatusStart",
    "TransferClient",
    "TransferOutcome",
    "TransferStatusWriter",
    "time_limit_s",
    "transfer_caller_id",
]

TRANSFER_TIMEOUT_S: Final = 30
"""Telnyx ``timeout_secs``: how long the target rings."""
TRANSFER_OUTCOME_GRACE_S: Final = 10.0
"""Extra wait after the ring timeout for the target's webhook to reach the media worker."""
MIN_TIME_LIMIT_S: Final = 30
"""Floor of ``time_limit_secs`` (a transfer right before the duration limit still gets a bridged minute)."""
MAX_TIME_LIMIT_S: Final = 14_400
"""Telnyx's ceiling of ``time_limit_secs``."""
TRANSFER_STARTED_MESSAGE: Final = "Transfer started."

type CallerIdMode = Literal["agent_number", "original_caller"]
type StatusStart = Literal["started", "not_active", "unavailable"]
type TransferOutcome = Literal["bridged", "target_hangup", "timeout", "command_failed", "not_active"]

_BRIDGED: Final = "call.bridged"
_HANGUP: Final = "call.hangup"

_log = get_logger(__name__)


def transfer_caller_id(context: CallContext, mode: CallerIdMode) -> str:
    """The ``from`` number the target sees: the agent's number (outbound ``from_number``, inbound/web
    ``to_number``) or the party being transferred (outbound ``to_number``, inbound/web ``from_number``)."""
    agent_is_caller = context.direction == "outbound"
    if mode == "agent_number":
        return context.from_number if agent_is_caller else context.to_number
    return context.to_number if agent_is_caller else context.from_number


def time_limit_s(deadline: datetime, now: datetime) -> int:
    """``time_limit_secs``: the seconds left until the call's duration deadline (rounded up), at least
    :data:`MIN_TIME_LIMIT_S` and at most :data:`MAX_TIME_LIMIT_S`."""
    remaining = math.ceil((deadline - now).total_seconds())
    return min(MAX_TIME_LIMIT_S, max(MIN_TIME_LIMIT_S, remaining))


class TransferClient(Protocol):
    """The Telnyx command a cold transfer needs (:class:`va_telnyx.TelnyxClient` satisfies it)."""

    async def transfer(self, call_control_id: str, request: TransferRequest) -> CommandResult: ...


class TransferStatusWriter(Protocol):
    """Moves the call into and out of ``transferring`` (:class:`DbTransferStatus`): :meth:`start` =
    ``in_progress`` → ``transferring`` (``not_active`` when the call is not ``in_progress``, ``unavailable``
    when the row could not be written), :meth:`revert` = ``transferring`` → ``in_progress`` after a failed
    transfer (failures are logged)."""

    async def start(self, call_id: uuid.UUID) -> StatusStart: ...

    async def revert(self, call_id: uuid.UUID) -> None: ...


class DbTransferStatus:
    """:class:`TransferStatusWriter` over the ``calls`` row in the org's tenant session."""

    def __init__(self, database: Database, org_id: uuid.UUID) -> None:
        self._database = database
        self._org_id = org_id

    async def start(self, call_id: uuid.UUID) -> StatusStart:
        try:
            async with self._database.tenant_session(self._org_id) as session:
                await CallRepo(session, self._org_id).transition(
                    call_id, "transferring", from_={"in_progress"}
                )
        except CallTransitionError as exc:
            _log.info("media.transfer_not_active", status=exc.current)
            return "not_active"
        except (SQLAlchemyError, OSError) as exc:
            _log.error("media.transfer_status_failed", error_type=type(exc).__name__)
            return "unavailable"
        return "started"

    async def revert(self, call_id: uuid.UUID) -> None:
        try:
            async with self._database.tenant_session(self._org_id) as session:
                await CallRepo(session, self._org_id).transition(
                    call_id, "in_progress", from_={"transferring"}
                )
        except CallTransitionError as exc:
            _log.info("media.transfer_revert_skipped", status=exc.current)
        except (SQLAlchemyError, OSError) as exc:
            _log.error("media.transfer_revert_failed", error_type=type(exc).__name__)


class ColdTransfer:
    """The cold transfers of one call (see the module docstring); :meth:`run` once per ``transfer_call``."""

    def __init__(  # noqa: PLR0913 - the call's collaborators, all keyword-only
        self,
        *,
        context: CallContext,
        call_control_id: str,
        telnyx: TransferClient | None,
        transfer_events: asyncio.Queue[CarrierEvent],
        status: TransferStatusWriter | None,
        events: EventSink | None,
        caller_id_mode: CallerIdMode,
        deadline: datetime,
        clock: Clock,
        tracer_provider: TracerProvider | None = None,
    ) -> None:
        self._context = context
        self._call_control_id = call_control_id
        self._telnyx = telnyx
        self._queue = transfer_events
        self._status = status
        self._events = events
        self._caller_id_mode = caller_id_mode
        self._deadline = deadline
        self._clock = clock
        self._tracer = trace.get_tracer("va_media", tracer_provider=tracer_provider)
        self._transfers = 0

    @property
    def transfers(self) -> int:
        """Transfers sent to Telnyx so far (the ``n`` of the next ``command_id``)."""
        return self._transfers

    async def run(self, target: str) -> TransferOutcome:
        with self._tracer.start_as_current_span("call.transfer") as span:
            span.set_attribute("va.transfer.type", "cold")
            outcome = await self._run(target)
            span.set_attribute("va.transfer.outcome", outcome)
        _log.info("media.transfer_finished", outcome=outcome, attempt=self._transfers)
        return outcome

    async def _run(self, target: str) -> TransferOutcome:
        telnyx = self._telnyx
        if telnyx is None:
            _log.warning("media.transfer_skipped", detail="telnyx_unavailable")
            return "command_failed"
        started = "started" if self._status is None else await self._status.start(self._context.call_id)
        if started != "started":
            return "not_active" if started == "not_active" else "command_failed"
        await self._publish_started(target)
        self._drain_stale()
        outcome = await self._transfer(telnyx, target)
        if outcome != "bridged" and self._status is not None:
            await self._status.revert(self._context.call_id)
        return outcome

    async def _transfer(self, telnyx: TransferClient, target: str) -> TransferOutcome:
        """Send the command and wait for the target leg's outcome."""
        attempt = self._transfers
        self._transfers += 1
        try:
            await telnyx.transfer(self._call_control_id, self._request(target, attempt))
        except TelnyxError as exc:
            _log.warning("media.transfer_command_failed", error_type=type(exc).__name__)
            return "command_failed"
        outcomes: list[TransferOutcome] = []

        async def wait() -> None:
            outcomes.append(await self._outcome())

        if not await within(wait(), self._clock, TRANSFER_TIMEOUT_S + TRANSFER_OUTCOME_GRACE_S):
            return "timeout"
        return outcomes[0]

    def _request(self, target: str, attempt: int) -> TransferRequest:
        context = self._context
        return TransferRequest(
            to=target,
            from_=transfer_caller_id(context, self._caller_id_mode),
            timeout_secs=TRANSFER_TIMEOUT_S,
            time_limit_secs=time_limit_s(self._deadline, self._clock.now()),
            target_leg_client_state=encode(
                ClientState(call_id=context.call_id, leg="transfer_target", org_id=context.org_id)
            ),
            client_state=encode(ClientState(call_id=context.call_id, leg="primary", org_id=context.org_id)),
            command_id=command_id("transfer", context.call_id, "primary", attempt),
        )

    async def _outcome(self) -> TransferOutcome:
        """The first bridge or hangup of the target leg (other target events are skipped)."""
        while True:
            event = await self._queue.get()
            if event.type == _BRIDGED:
                return "bridged"
            if event.type == _HANGUP:
                return "target_hangup"

    def _drain_stale(self) -> None:
        """Drop target-leg events of an earlier transfer, so they cannot decide this one."""
        dropped = 0
        while not self._queue.empty():
            self._queue.get_nowait()
            dropped += 1
        if dropped:
            _log.info("media.transfer_stale_events", dropped=dropped)

    async def _publish_started(self, target: str) -> None:
        if self._events is None:
            return
        event = new_event(
            EventType.CALL_TRANSFER_STARTED,
            org_id=self._context.org_id,
            clock=self._clock,
            call_id=self._context.call_id,
            message=TRANSFER_STARTED_MESSAGE,
            data={"type": "cold", "target": target},
        )
        try:
            await self._events.publish(event)
        except (EventPublishError, OSError) as exc:
            _log.warning(
                "media.event_publish_failed", event_type=event.type.value, error_type=type(exc).__name__
            )
