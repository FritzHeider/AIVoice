"""The ``PostCallWorkflow`` activities of steps 1, 2, 11, and 13 plus the step bookkeeping
(specs/18-post-call-analysis.md §1, §10, §11; Decision P05-T18b).

* ``finalize_call_record`` (step 1) backfills what the call's end left unset (``ended_at`` from the primary
  leg, ``ended_by`` from the end reason, connected/transfer seconds from the legs) and returns the facts the
  workflow plans with. Turns need no flush here: the media worker flushes its turn writer before the session
  closes (specs/07 §18 Decision P05-T11b).
* ``find_saved_recording`` (step 2) reads the stored ``call.recording.saved`` webhook (system scope,
  ``TelnyxEventRepo``, specs/03 §2) so a signal sent before the workflow started is never lost;
  ``report_recording_missing`` records ``call.error`` ``RECORDING_MISSING`` once.
* ``finalize_billing`` (step 11) prices the call with ``va_billing.metering.finalize_call``.
* ``emit_completed`` (step 13) sets ``queue_status='complete'`` and records ``call.completed`` with the Call
  Object (the ``GET /v1/calls/{id}`` JSON, 7-day recording URL) in one transaction, once per call.
* ``mark_postcall_step`` appends a completed step to ``calls.config.postcall_steps``.

Each activity observes ``va_postcall_duration_ms{step}`` (the catalog's name for specs/18 §11's histogram);
activity failures are counted by the Temporal SDK metrics.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any, Final

from prometheus_client import Histogram
from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from va_billing.metering import CallNotEndedError, finalize_call
from va_common.errors import AppError
from va_common.logging import bound_context, get_logger
from va_common.storage import RecordingStorage, SignedUrl, signed_recording_url
from va_common.time import Clock
from va_db.call_end import stored_reason
from va_db.call_object import load_call_objects
from va_db.call_states import END_REASONS, call_durations, is_terminal
from va_db.models import Call, CallEvent, CallLeg
from va_db.post_call_steps import done_steps, mark_step
from va_db.repositories import TelnyxEventRepo
from va_db.session import Database
from va_domain.carrier import CarrierEvent
from va_events import EventPublisher, EventType
from va_workflows.activities.errors import (
    BILLING_UNAVAILABLE,
    CALL_NOT_ENDED,
    call_not_found,
    retryable,
)
from va_workflows.activities.events import broadcast_events, call_event, persist_events
from va_workflows.activities.inputs import CallActivityInput
from va_workflows.activities.lookup import load_call
from va_workflows.activities.post_call_inputs import (
    BillingResult,
    CallRecordFacts,
    CompletedResult,
    PostCallStepInput,
    SavedRecording,
)

__all__ = [
    "COMPLETED_MESSAGE",
    "RECORDING_MISSING_CODE",
    "RECORDING_MISSING_MESSAGE",
    "PostCallActivities",
]

COMPLETED_MESSAGE: Final = "Call completed."
RECORDING_MISSING_CODE: Final = "RECORDING_MISSING"
RECORDING_MISSING_MESSAGE: Final = "The call recording did not arrive."
CALL_NOT_ENDED_MESSAGE: Final = "The call has not ended yet."
BILLING_UNAVAILABLE_MESSAGE: Final = "The call cannot be priced right now."
_NOT_FOUND_CODES: Final = frozenset({"CALL_NOT_FOUND", "NOT_FOUND"})
_TRANSFER_TARGET: Final = "transfer_target"

_log = get_logger(__name__)


def _recording_wanted(config: dict[str, Any]) -> bool:
    """``record``, ``post_call_evals``, or ``retranscribe`` asks for the recording (specs/18 §1 step 2)."""
    return (
        config.get("record") is True
        or bool(config.get("post_call_evals"))
        or config.get("retranscribe") is True
    )


def _leg(legs: list[CallLeg], role: str) -> CallLeg | None:
    return next((leg for leg in legs if leg.role == role), None)


def _transfer_start(legs: list[CallLeg]) -> datetime | None:
    target = _leg(legs, _TRANSFER_TARGET)
    return target.started_at if target is not None else None


def _not_ended(status: str) -> Exception:
    return retryable(f"{CALL_NOT_ENDED_MESSAGE} ({status})", CALL_NOT_ENDED)


class PostCallActivities:
    """The post-call activities; one instance per worker process."""

    def __init__(
        self,
        *,
        database: Database,
        publisher: EventPublisher,
        clock: Clock,
        storage: RecordingStorage | None = None,
        step_ms: Histogram | None = None,
    ) -> None:
        self._database = database
        self._publisher = publisher
        self._clock = clock
        self._storage = storage
        self._step_ms = step_ms

    @property
    def has_storage(self) -> bool:
        return self._storage is not None

    @asynccontextmanager
    async def _step(self, step: str, request: CallActivityInput | PostCallStepInput) -> AsyncIterator[None]:
        """Bind the call's log fields and observe the step duration (also when it fails)."""
        started = self._clock.monotonic()
        with bound_context(org_id=str(request.org_id), call_id=str(request.call_id)):
            try:
                yield
            finally:
                elapsed_ms = (self._clock.monotonic() - started) * 1000
                if self._step_ms is not None:
                    self._step_ms.labels(step=step).observe(elapsed_ms)
                _log.info("postcall.step_finished", step=step, duration_ms=round(elapsed_ms, 1))

    @activity.defn(name="finalize_call_record")
    async def finalize_call_record(self, request: CallActivityInput) -> CallRecordFacts:
        async with self._step("finalize_call_record", request):
            async with self._database.tenant_session(request.org_id) as session:
                call = await load_call(session, request)
                if not is_terminal(call.status):
                    raise _not_ended(call.status)
                values = await self._backfill(session, call)
                if values:
                    for name, value in values.items():
                        setattr(call, name, value)
                    await session.flush()
                    _log.info("postcall.call_record_backfilled", fields=sorted(values))
                facts = CallRecordFacts(
                    connected=call.answered_at is not None,
                    recording_wanted=_recording_wanted(call.config),
                    recording_storage=self.has_storage,
                    done_steps=done_steps(call.config),
                )
            if facts.connected and facts.recording_wanted and not facts.recording_storage:
                _log.warning("postcall.recording_storage_missing")
            return facts

    async def _backfill(self, session: AsyncSession, call: Call) -> dict[str, Any]:
        """The values the end left unset; never overwrites a stored value."""
        legs = list(
            await session.scalars(
                select(CallLeg).where(CallLeg.org_id == call.org_id, CallLeg.call_id == call.id)
            )
        )
        values: dict[str, Any] = {}
        ended_at = call.ended_at
        if ended_at is None:
            ended_at = values["ended_at"] = self._leg_end(legs)
        reason = stored_reason(call.end_reason)
        if call.ended_by is None and reason is not None:
            values["ended_by"] = END_REASONS[reason]
        if call.answered_at is not None and not call.connected_seconds and not call.transfer_seconds:
            values.update(call_durations(call.answered_at, ended_at, _transfer_start(legs)))
        return values

    def _leg_end(self, legs: list[CallLeg]) -> datetime:
        """The primary leg's end, else now."""
        primary = _leg(legs, "primary")
        if primary is not None and primary.ended_at is not None:
            return primary.ended_at
        return self._clock.now()

    @activity.defn(name="find_saved_recording")
    async def find_saved_recording(self, request: CallActivityInput) -> SavedRecording:
        async with self._step("find_saved_recording", request):
            async with self._database.tenant_session(request.org_id) as session:
                control_ids = list(
                    await session.scalars(
                        select(CallLeg.call_control_id).where(
                            CallLeg.org_id == request.org_id, CallLeg.call_id == request.call_id
                        )
                    )
                )
            # System scope (specs/03 §2): webhooks are stored before any org is known.
            async with self._database.system_session() as session:
                row = await TelnyxEventRepo(session).recording_saved(request.call_id, control_ids)
                if row is None:
                    return SavedRecording()
                data = row.payload.get("data")
                payload = data.get("payload") if isinstance(data, dict) else None
                event = CarrierEvent(
                    type=row.event_type,
                    leg="primary",
                    call_id=request.call_id,
                    org_id=request.org_id,
                    telnyx_event_id=row.telnyx_event_id,
                    occurred_at=row.occurred_at,
                    payload=payload if isinstance(payload, dict) else {},
                )
            _log.info("postcall.recording_found", telnyx_event_id=event.telnyx_event_id)
            return SavedRecording(event=event)

    @activity.defn(name="report_recording_missing")
    async def report_recording_missing(self, request: CallActivityInput) -> bool:
        """Record ``call.error`` ``RECORDING_MISSING``; ``False`` when an earlier attempt already did."""
        async with self._step("report_recording_missing", request):
            async with self._database.tenant_session(request.org_id) as session:
                await load_call(session, request)
                existing = await session.scalar(
                    select(CallEvent.id)
                    .where(
                        CallEvent.org_id == request.org_id,
                        CallEvent.call_id == request.call_id,
                        CallEvent.category == "error",
                        CallEvent.data["code"].astext == RECORDING_MISSING_CODE,
                    )
                    .limit(1)
                )
                if existing is not None:
                    return False
                data: dict[str, JsonValue] = {"code": RECORDING_MISSING_CODE}
                event = call_event(
                    self._clock,
                    request.org_id,
                    request.call_id,
                    EventType.CALL_ERROR,
                    message=RECORDING_MISSING_MESSAGE,
                    data=data,
                    level="warning",
                )
                events = await persist_events(session, self._publisher, [event])
            await broadcast_events(self._publisher, events)
            _log.warning("postcall.recording_missing")
            return True

    @activity.defn(name="mark_postcall_step")
    async def mark_postcall_step(self, request: PostCallStepInput) -> bool:
        """Append the step to ``calls.config.postcall_steps``; ``False`` when it was recorded before."""
        with bound_context(org_id=str(request.org_id), call_id=str(request.call_id)):
            async with self._database.tenant_session(request.org_id) as session:
                return await mark_step(session, request.org_id, request.call_id, request.step)

    @activity.defn(name="finalize_billing")
    async def finalize_billing(self, request: CallActivityInput) -> BillingResult:
        async with self._step("finalize_billing", request):
            try:
                async with self._database.tenant_session(request.org_id) as session:
                    finalized = await finalize_call(
                        session, request.org_id, request.call_id, now=self._clock.now()
                    )
            except CallNotEndedError as exc:
                raise _not_ended(exc.status) from exc
            except AppError as exc:
                if exc.code in _NOT_FOUND_CODES:
                    raise call_not_found() from exc
                _log.warning("postcall.billing_unavailable", code=exc.code)
                raise retryable(BILLING_UNAVAILABLE_MESSAGE, BILLING_UNAVAILABLE) from exc
            _log.info(
                "postcall.billing_finalized",
                price_micros=finalized.price_micros,
                hold=finalized.hold,
                posted=len(finalized.posted),
            )
            return BillingResult(
                price_micros=finalized.price_micros, hold=finalized.hold, posted=len(finalized.posted)
            )

    @activity.defn(name="emit_completed")
    async def emit_completed(self, request: CallActivityInput) -> CompletedResult:
        async with self._step("emit_completed", request):
            async with self._database.tenant_session(request.org_id) as session:
                call = await session.scalar(
                    select(Call)
                    .where(Call.org_id == request.org_id, Call.id == request.call_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if call is None:
                    raise call_not_found()
                if not is_terminal(call.status):
                    _log.error("postcall.completed_not_ended", status=call.status)
                    return CompletedResult(status="not_ended")
                if call.queue_status == "complete":
                    return CompletedResult(status="already_completed")
                call.queue_status = "complete"
                await session.flush()
                urls = {call.id: await self._recording_url(call)}
                (call_object,) = await load_call_objects(session, [call], recording_urls=urls)
                data: dict[str, JsonValue] = call_object.model_dump(mode="json", by_alias=True)
                event = call_event(
                    self._clock,
                    request.org_id,
                    request.call_id,
                    EventType.CALL_COMPLETED,
                    message=COMPLETED_MESSAGE,
                    data=data,
                )
                events = await persist_events(session, self._publisher, [event])
            await broadcast_events(self._publisher, events)
            _log.info("postcall.completed_emitted")
            return CompletedResult(status="emitted")

    async def _recording_url(self, call: Call) -> SignedUrl | None:
        storage = self._storage
        if storage is None or call.recording_s3_key is None:
            return None
        return await signed_recording_url(
            storage.store, storage.bucket, call.recording_s3_key, now=self._clock.now()
        )
