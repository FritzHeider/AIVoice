"""``CallWorkflow``: the durable lifecycle of one call (specs/01-architecture.md §2.1 steps 2-9 and §3,
Decision P05-T06c).

The workflow waits for a scheduled start, prefetches the call context, dials (requeueing while the org's
concurrency cap is full), waits for the answer, and then guards the conversation the media worker runs: the
heartbeat watch, the ``max_duration + 120 s`` backstop, fax detection, and a failed media stream. Every end
it decides is recorded with ``end_call`` *before* ``hangup_call`` (specs/06 §6 Decision P05-T06b item 5).
Finally it starts the ``PostCallWorkflow`` child (abandoned on close) for every call it found.

Workflow code is deterministic: signals only record state, time comes from ``workflow.now()``, and activities
are scheduled by name with inputs from import-free modules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Final, Literal

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError, WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from va_domain.call_workflow import (
        CALL_WORKFLOW_NAME,
        CANCEL_SIGNAL,
        PIPELINE_FINISHED_SIGNAL,
        CallWorkflowInput,
        PipelineFinished,
    )
    from va_domain.carrier import CARRIER_SIGNAL, CarrierEvent
    from va_domain.post_call import (
        POSTCALL_WORKFLOW_NAME,
        PostCallWorkflowInput,
        postcall_task_queue,
        postcall_workflow_id,
    )
    from va_workflows.activities.context_inputs import PREFETCH_RETRY, PREFETCH_TIMEOUT, PrefetchResult
    from va_workflows.activities.errors import CALL_NOT_FOUND
    from va_workflows.activities.inputs import CallActivityInput
    from va_workflows.activities.liveness_inputs import (
        PRESENCE_RETRY,
        PRESENCE_TIMEOUT,
        WATCH_HEARTBEAT_TIMEOUT,
        WATCH_RETRY,
        CallPresence,
        WatchInput,
        WatchResult,
        watch_timeout,
    )
    from va_workflows.activities.telephony_inputs import (
        DIAL_RETRY,
        DIAL_TIMEOUT,
        END_CALL_RETRY,
        END_CALL_TIMEOUT,
        HANGUP_RETRY,
        HANGUP_TIMEOUT,
        DialInput,
        DialOutcome,
        EndCallInput,
        EndCallResult,
        HangupInput,
        HangupResult,
        WorkflowEndReason,
    )
    from va_workflows.calls.policies import (
        DEFAULT_MAX_DURATION_S,
        MAX_DURATION_GRACE,
        PIPELINE_GRACE,
        REQUEUE_BUDGET,
        REQUEUE_DELAY,
        RING_BACKSTOP,
        STREAM_FAILURE_GRACE,
    )
    from va_workflows.calls.results import CallOutcome, CallWorkflowResult
    from va_workflows.calls.state import CallSignals

__all__ = [
    "DIAL_FAILED_MESSAGE",
    "NOT_ANSWERED_MESSAGE",
    "PIPELINE_UNRECORDED_MESSAGE",
    "RING_BACKSTOP_MESSAGE",
    "STREAM_FAILED_MESSAGE",
    "CallWorkflow",
]

DIAL_FAILED_MESSAGE: Final = "The call could not be dialed."
RING_BACKSTOP_MESSAGE: Final = "No answer or hangup from the carrier in time."
NOT_ANSWERED_MESSAGE: Final = "Carrier hung up before answer."
PIPELINE_UNRECORDED_MESSAGE: Final = "Pipeline finished without recording the end."
STREAM_FAILED_MESSAGE: Final = "Media stream failed."

type _Step = Literal["pipeline_finished", "fax", "heartbeat_lost", "hung_up", "stream_failed", "max_duration"]

_HANGUP_ENDS: Final[dict[_Step, tuple[CallOutcome, WorkflowEndReason, str, str | None]]] = {
    "fax": ("fax_detected", "fax_detected", "fax_detected", None),
    "heartbeat_lost": ("worker_crash", "worker_crash", "worker_crash", None),
    "stream_failed": ("stream_failed", "error", "stream_failed", STREAM_FAILED_MESSAGE),
    "max_duration": ("max_duration", "max_duration", "max_duration", None),
}
"""In-call steps that end the call from the workflow: (outcome, end reason, hangup reason, error message)."""

_UNDIALED: Final[dict[str, tuple[CallOutcome, WorkflowEndReason]]] = {
    "skipped": ("skipped", "canceled"),
    "blocked": ("blocked", "dispatch_blocked"),
    "failed": ("failed", "carrier_error"),
}
"""Dial outcomes after which no call is up: (outcome, cleanup reason). ``end_call`` is a no-op (apart from the
idempotent slot release) for the calls these outcomes already ended."""


class _CallNotFoundError(Exception):
    """The call does not exist in the org: the workflow ends without cleanup or post-call."""


def _cause_type(error: ActivityError) -> str:
    """The ``ApplicationError`` type behind an activity failure (or the cause's class name)."""
    cause = error.cause
    if isinstance(cause, ApplicationError):
        return cause.type or "ApplicationError"
    return type(cause).__name__


def _raise_if_not_found(error: ActivityError) -> None:
    if _cause_type(error) == CALL_NOT_FOUND:
        raise _CallNotFoundError from error


@workflow.defn(name=CALL_WORKFLOW_NAME)
class CallWorkflow:
    """One per call, id ``call-<call_id>``; signals come from the edge, the stop API, and the media worker."""

    _call: CallActivityInput
    """The call the activities work on (set first thing in ``run``)."""

    def __init__(self) -> None:
        self._signals = CallSignals()
        self._phase = "starting"
        self._watch: workflow.ActivityHandle[WatchResult] | None = None

    @workflow.signal(name=CARRIER_SIGNAL)
    def signal_carrier_event(self, event: CarrierEvent) -> None:
        recorded = self._signals.record(event, workflow.now())
        if recorded != "recorded":
            workflow.logger.debug("calls.carrier_event_%s type=%s leg=%s", recorded, event.type, event.leg)

    @workflow.signal(name=CANCEL_SIGNAL)
    def signal_cancel(self) -> None:
        if not self._signals.request_cancel():
            workflow.logger.info("calls.cancel_ignored_in_progress")

    @workflow.signal(name=PIPELINE_FINISHED_SIGNAL)
    def signal_pipeline_finished(self, finished: PipelineFinished) -> None:
        self._signals.finish_pipeline(finished)

    @workflow.query(name="phase")
    def phase(self) -> str:
        """``starting`` → (``scheduled``) → ``prefetch`` → ``dialing`` (⇄ ``requeued``) → ``ringing`` →
        ``in_progress`` (→ ``hung_up``) → ``ending`` → ``post_call`` → ``ended``."""
        return self._phase

    @workflow.run
    async def run(self, start: CallWorkflowInput) -> CallWorkflowResult:
        self._call = CallActivityInput(call_id=start.call_id, org_id=start.org_id)
        try:
            result = await self._lifecycle(start)
        except _CallNotFoundError:
            workflow.logger.warning("calls.workflow_call_not_found")
            self._phase = "ended"
            return CallWorkflowResult(outcome="not_found")
        self._phase = "post_call"
        await self._start_post_call(start)
        self._phase = "ended"
        return result

    async def _lifecycle(self, start: CallWorkflowInput) -> CallWorkflowResult:
        if await self._wait_until_scheduled(start.scheduled_for):
            return await self._cleanup("canceled", "canceled")
        prefetch = await self._prefetch()
        if prefetch is not None and not prefetch.stored:
            return await self._cleanup("skipped", "canceled")
        if self._signals.cancel_requested:
            return await self._cleanup("canceled", "canceled")
        dialed = await self._dial()
        if isinstance(dialed, CallWorkflowResult):
            return dialed
        return await self._ring(dialed)

    async def _wait_until_scheduled(self, scheduled_for: datetime | None) -> bool:
        """Sleep until a scheduled start; ``True`` when a cancel arrived meanwhile."""
        if scheduled_for is None or scheduled_for <= workflow.now():
            return False
        self._phase = "scheduled"
        return await self._wait_for(lambda: self._signals.cancel_requested, scheduled_for - workflow.now())

    async def _prefetch(self) -> PrefetchResult | None:
        """The prefetch result; ``None`` when it failed (the media worker rebuilds the context, specs/07
        §3)."""
        self._phase = "prefetch"
        try:
            prefetched: PrefetchResult = await workflow.execute_activity(
                "prefetch_call_context",
                self._call,
                start_to_close_timeout=PREFETCH_TIMEOUT,
                retry_policy=PREFETCH_RETRY,
                result_type=PrefetchResult,
            )
        except ActivityError as exc:
            _raise_if_not_found(exc)
            workflow.logger.warning("calls.prefetch_failed error=%s", _cause_type(exc))
            return None
        return prefetched

    async def _dial(self) -> DialOutcome | CallWorkflowResult:
        """Dial, requeueing every 15 s while the cap is full; a result when no call is up afterwards."""
        self._phase = "dialing"
        first_requeue_at: datetime | None = None
        while True:
            final = first_requeue_at is not None and (
                workflow.now() + REQUEUE_DELAY - first_requeue_at >= REQUEUE_BUDGET
            )
            try:
                outcome = await self._execute_dial(final_attempt=final)
            except ActivityError as exc:
                _raise_if_not_found(exc)
                workflow.logger.warning("calls.dial_activity_failed error=%s", _cause_type(exc))
                return await self._end_and_hangup("error", "error", "dial_failed", DIAL_FAILED_MESSAGE)
            if outcome.result != "requeue":
                break
            if first_requeue_at is None:
                first_requeue_at = workflow.now()
            self._phase = "requeued"
            if await self._wait_for(lambda: self._signals.cancel_requested, REQUEUE_DELAY):
                return await self._cleanup("canceled", "canceled")
            self._phase = "dialing"
        undialed = _UNDIALED.get(outcome.result)
        if undialed is None:
            return outcome
        return await self._cleanup(*undialed)

    async def _execute_dial(self, *, final_attempt: bool) -> DialOutcome:
        target = self._call
        dialed: DialOutcome = await workflow.execute_activity(
            "dial",
            DialInput(call_id=target.call_id, org_id=target.org_id, final_attempt=final_attempt),
            start_to_close_timeout=DIAL_TIMEOUT,
            retry_policy=DIAL_RETRY,
            result_type=DialOutcome,
        )
        return dialed

    async def _ring(self, dialed: DialOutcome) -> CallWorkflowResult:
        self._phase = "ringing"
        signals = self._signals
        if not await self._wait_for(
            lambda: signals.answered or signals.hung_up or signals.cancel_requested, RING_BACKSTOP
        ):
            return await self._end_and_hangup("error", "error", "ring_backstop", RING_BACKSTOP_MESSAGE)
        if signals.answered:
            return await self._converse(dialed.max_duration_s or DEFAULT_MAX_DURATION_S)
        if signals.cancel_requested:
            return await self._end_and_hangup("canceled", "canceled", "canceled")
        return await self._cleanup("not_answered", "carrier_error", NOT_ANSWERED_MESSAGE)

    async def _converse(self, max_duration_s: int) -> CallWorkflowResult:
        """Guard the answered call until the conversation ends or a backstop fires."""
        self._phase = "in_progress"
        deadline = workflow.now() + timedelta(seconds=max_duration_s) + MAX_DURATION_GRACE
        target = self._call
        self._watch = workflow.start_activity(
            "watch_call_heartbeat",
            WatchInput(call_id=target.call_id, org_id=target.org_id),
            start_to_close_timeout=watch_timeout(max_duration_s),
            heartbeat_timeout=WATCH_HEARTBEAT_TIMEOUT,
            retry_policy=WATCH_RETRY,
            result_type=WatchResult,
        )
        try:
            step = await self._next_step(deadline)
            while step is None:
                step = await self._next_step(deadline)
        finally:
            await self._stop_watch()
        if step == "pipeline_finished":
            return await self._cleanup("completed", "error", PIPELINE_UNRECORDED_MESSAGE)
        if step == "hung_up":
            return await self._after_carrier_hangup()
        return await self._end_and_hangup(*_HANGUP_ENDS[step])

    async def _next_step(self, deadline: datetime) -> _Step | None:
        """Wait for the next thing to act on (or a timer) and decide; ``None`` to keep waiting."""
        signals = self._signals
        stream_seen = signals.stream_failed_at
        timeout = deadline - workflow.now()
        if stream_seen is not None:
            timeout = min(timeout, stream_seen + STREAM_FAILURE_GRACE - workflow.now())
        if timeout > timedelta(0):
            await self._wait_for(lambda: self._converse_wake(stream_seen), timeout)
        return await self._decide(deadline)

    def _converse_wake(self, stream_seen: datetime | None) -> bool:
        signals = self._signals
        return (
            signals.pipeline_finished is not None
            or signals.fax
            or signals.hung_up
            or signals.stream_failed_at != stream_seen
            or (self._watch is not None and self._watch.done())
        )

    async def _decide(self, deadline: datetime) -> _Step | None:
        signalled = self._signalled_step()
        if signalled is not None:
            return signalled
        failed_at = self._signals.stream_failed_at
        if (
            failed_at is not None
            and workflow.now() >= failed_at + STREAM_FAILURE_GRACE
            and await self._orphaned()
        ):
            return "stream_failed"
        if workflow.now() >= deadline:
            return "max_duration"
        return None

    def _signalled_step(self) -> _Step | None:
        """The step a signal (or the watch) asks for, in priority order."""
        signals = self._signals
        if signals.pipeline_finished is not None:
            return "pipeline_finished"
        if signals.fax:
            return "fax"
        if self._heartbeat_lost():
            return "heartbeat_lost"
        if signals.hung_up:
            return "hung_up"
        return None

    def _heartbeat_lost(self) -> bool:
        """Whether the watch reported a lost heartbeat; a failed watch is dropped (the backstop still
        runs)."""
        watch = self._watch
        if watch is None or not watch.done():
            return False
        try:
            watch.result()
        except ActivityError as exc:
            workflow.logger.warning("calls.heartbeat_watch_failed error=%s", _cause_type(exc))
            self._watch = None
            return False
        return True

    async def _orphaned(self) -> bool:
        """After a failed stream: ``True`` when no media worker owns the call (the stream is gone for
        good)."""
        self._signals.stream_failed_at = None
        try:
            presence = await workflow.execute_activity(
                "call_presence",
                self._call,
                start_to_close_timeout=PRESENCE_TIMEOUT,
                retry_policy=PRESENCE_RETRY,
                result_type=CallPresence,
            )
        except ActivityError as exc:
            workflow.logger.warning("calls.presence_failed error=%s", _cause_type(exc))
            return False
        return not presence.owner

    async def _after_carrier_hangup(self) -> CallWorkflowResult:
        """The caller (or carrier) hung up: give the media worker time to record the end, then clean up."""
        self._phase = "hung_up"
        signals = self._signals
        await self._wait_for(lambda: signals.pipeline_finished is not None, PIPELINE_GRACE)
        if signals.pipeline_finished is not None:
            return await self._cleanup("completed", "error", PIPELINE_UNRECORDED_MESSAGE)
        result = await self._cleanup("worker_crash", "worker_crash")
        if result.end_reason is None:
            return CallWorkflowResult(outcome="completed")
        return result

    async def _stop_watch(self) -> None:
        watch, self._watch = self._watch, None
        if watch is None:
            return
        if not watch.done():
            watch.cancel()
        try:
            await watch
        except (ActivityError, asyncio.CancelledError):
            workflow.logger.debug("calls.heartbeat_watch_stopped")

    async def _wait_for(self, condition: Callable[[], bool], within: timedelta) -> bool:
        """``True`` when ``condition`` held within ``within``."""
        try:
            await workflow.wait_condition(condition, timeout=within)
        except TimeoutError:
            return condition()
        return True

    async def _cleanup(
        self, outcome: CallOutcome, reason: WorkflowEndReason, message: str | None = None
    ) -> CallWorkflowResult:
        """Record the end (a no-op apart from the slot release when the call already ended)."""
        self._phase = "ending"
        ended = await self._end_call(reason, message)
        return CallWorkflowResult(outcome=outcome, end_reason=reason if ended.changed else None)

    async def _end_and_hangup(
        self, outcome: CallOutcome, reason: WorkflowEndReason, hangup_reason: str, message: str | None = None
    ) -> CallWorkflowResult:
        """``end_call`` first, then ``hangup_call``; the hangup is attempted even when ``end_call`` failed."""
        self._phase = "ending"
        try:
            ended = await self._end_call(reason, message)
        except ActivityError:
            await self._hangup(hangup_reason)
            raise
        await self._hangup(hangup_reason)
        return CallWorkflowResult(outcome=outcome, end_reason=reason if ended.changed else None)

    async def _end_call(self, reason: WorkflowEndReason, message: str | None) -> EndCallResult:
        target = self._call
        ended: EndCallResult = await workflow.execute_activity(
            "end_call",
            EndCallInput(
                call_id=target.call_id, org_id=target.org_id, end_reason=reason, error_message=message
            ),
            start_to_close_timeout=END_CALL_TIMEOUT,
            retry_policy=END_CALL_RETRY,
            result_type=EndCallResult,
        )
        return ended

    async def _hangup(self, reason: str) -> None:
        target = self._call
        try:
            await workflow.execute_activity(
                "hangup_call",
                HangupInput(call_id=target.call_id, org_id=target.org_id, reason=reason),
                start_to_close_timeout=HANGUP_TIMEOUT,
                retry_policy=HANGUP_RETRY,
                result_type=HangupResult,
            )
        except ActivityError as exc:
            workflow.logger.warning("calls.hangup_failed reason=%s error=%s", reason, _cause_type(exc))

    async def _start_post_call(self, start: CallWorkflowInput) -> None:
        """Start ``PostCallWorkflow`` (abandoned when this workflow closes); an existing one is kept."""
        try:
            await workflow.start_child_workflow(
                POSTCALL_WORKFLOW_NAME,
                PostCallWorkflowInput(call_id=start.call_id, org_id=start.org_id),
                id=postcall_workflow_id(start.call_id),
                task_queue=postcall_task_queue(workflow.info().task_queue),
                parent_close_policy=workflow.ParentClosePolicy.ABANDON,
            )
        except WorkflowAlreadyStartedError:
            workflow.logger.info("calls.post_call_already_started")
