"""``PostCallWorkflow``: what happens after a call ended (specs/18-post-call-analysis.md §1, §11; Decision
P05-T18b).

Started by ``CallWorkflow`` as an abandoned child (``postcall-<call_id>``) on the post-call queue. This
version runs steps 1 (``finalize_call_record``), 2 (``await_recording``), 3 (``store_recording``), 11
(``finalize_billing``), and 13 (``emit_completed``); the analysis steps join in later phases by their step
names. A step completed by an earlier execution (``calls.config.postcall_steps``) is skipped; step 1 always
runs because it returns the facts the remaining steps are planned with.

Every step after step 1 is wrapped: a failure after its retries is logged and listed, and the workflow
continues, so ``emit_completed`` always runs last. Everything before it races the 30 min budget; when the
budget runs out the unfinished work is cancelled and ``call.completed`` is still emitted (the parent has
abandoned the child, so the child compensates itself).

The recording wait checks the stored ``call.recording.saved`` webhook first (the edge stores every event
before signalling, so a signal sent before this workflow existed is not lost) and then waits for the
``recording_saved`` signal up to 10 min; without one it records ``call.error`` ``RECORDING_MISSING`` and goes
on without the recording.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Final

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from va_domain.carrier import CarrierEvent
    from va_domain.post_call import (
        POSTCALL_WORKFLOW_NAME,
        RECORDING_SAVED_SIGNAL,
        PostCallStep,
        PostCallTimings,
        PostCallWorkflowInput,
    )
    from va_workflows.activities.errors import CALL_NOT_FOUND
    from va_workflows.activities.inputs import CallActivityInput
    from va_workflows.activities.post_call_inputs import (
        BILLING_RETRY,
        BILLING_TIMEOUT,
        BOOKKEEPING_RETRY,
        BOOKKEEPING_TIMEOUT,
        EMIT_RETRY,
        EMIT_TIMEOUT,
        RECORD_RETRY,
        RECORD_TIMEOUT,
        BillingResult,
        CallRecordFacts,
        CompletedResult,
        CompletedStatus,
        PostCallStepInput,
        SavedRecording,
    )
    from va_workflows.activities.recording_inputs import (
        STORE_RECORDING_RETRY,
        STORE_RECORDING_TIMEOUT,
        StoreRecordingInput,
        StoreRecordingResult,
    )
    from va_workflows.post_call.plan import planned_steps, remaining
    from va_workflows.post_call.results import PostCallResult, RecordingOutcome

__all__ = ["PostCallWorkflow"]

_MARKED_COMPLETIONS: Final = frozenset({"emitted", "already_completed"})


class _CallNotFoundError(Exception):
    """Step 1 found no call: the workflow ends without any other step."""


def _cause_type(error: ActivityError) -> str:
    cause = error.cause
    if isinstance(cause, ApplicationError):
        return cause.type or "ApplicationError"
    return type(cause).__name__


@workflow.defn(name=POSTCALL_WORKFLOW_NAME)
class PostCallWorkflow:
    """One per call, id ``postcall-<call_id>``; the edge signals ``recording_saved``."""

    _call: CallActivityInput
    """The call the activities work on (set first thing in ``run``)."""

    def __init__(self) -> None:
        self._recording_event: CarrierEvent | None = None
        self._phase = "starting"
        self._ran: list[PostCallStep] = []
        self._skipped: list[PostCallStep] = []
        self._failed: list[PostCallStep] = []
        self._recording: RecordingOutcome = "not_wanted"
        self._price_micros: int | None = None
        self._out_of_time = False

    @workflow.signal(name=RECORDING_SAVED_SIGNAL)
    def signal_recording_saved(self, event: CarrierEvent) -> None:
        """Keep the first ``call.recording.saved`` (a redelivery or a second recording is ignored)."""
        if self._recording_event is None:
            self._recording_event = event

    @workflow.query(name="postcall_phase")
    def postcall_phase(self) -> str:
        """``starting`` → ``finalize_call_record`` → (``await_recording`` → ``store_recording``) →
        ``finalize_billing`` → ``emit_completed`` → ``done``."""
        return self._phase

    @workflow.run
    async def run(self, start: PostCallWorkflowInput) -> PostCallResult:
        self._call = CallActivityInput(call_id=start.call_id, org_id=start.org_id)
        timings = start.timings or PostCallTimings()
        body = asyncio.create_task(self._before_completion(timings))
        timed_out = False
        try:
            await workflow.wait_condition(body.done, timeout=timedelta(seconds=timings.budget_s))
        except TimeoutError:
            timed_out = self._out_of_time = True
            workflow.logger.warning("postcall.budget_exhausted phase=%s", self._phase)
            body.cancel()
        try:
            await body
        except _CallNotFoundError:
            workflow.logger.warning("postcall.call_not_found")
            self._phase = "done"
            return PostCallResult(status="call_not_found", ran=tuple(self._ran))
        except asyncio.CancelledError:
            if not timed_out:
                raise
        completed = await self._emit_completed()
        self._phase = "done"
        return PostCallResult(
            status="completed",
            ran=tuple(self._ran),
            skipped=tuple(self._skipped),
            failed=tuple(self._failed),
            recording=self._recording,
            price_micros=self._price_micros,
            timed_out=timed_out,
            completed=completed,
        )

    async def _before_completion(self, timings: PostCallTimings) -> None:
        """Steps 1-11 (``emit_completed`` runs after this, budget or not)."""
        facts = await self._finalize_call_record()
        planned = planned_steps(facts)
        done = facts.done_steps if facts is not None else ()
        todo = remaining(planned, done)
        self._skipped.extend(step for step in planned if step not in todo)
        if facts is not None:
            self._recording = self._recording_plan(facts)
        if "await_recording" in todo or "store_recording" in todo:
            await self._recording_steps(todo, timings)
        if "finalize_billing" in todo:
            await self._finalize_billing()

    @staticmethod
    def _recording_plan(facts: CallRecordFacts) -> RecordingOutcome:
        if not (facts.connected and facts.recording_wanted):
            return "not_wanted"
        # ``skipped`` until ``store_recording`` reports its outcome (stays when earlier runs did both steps).
        return "skipped" if facts.recording_storage else "no_storage"

    async def _finalize_call_record(self) -> CallRecordFacts | None:
        self._begin("finalize_call_record")
        try:
            facts: CallRecordFacts = await workflow.execute_activity(
                "finalize_call_record",
                self._call,
                start_to_close_timeout=RECORD_TIMEOUT,
                retry_policy=RECORD_RETRY,
                result_type=CallRecordFacts,
            )
        except ActivityError as exc:
            if _cause_type(exc) == CALL_NOT_FOUND:
                raise _CallNotFoundError from exc
            self._fail("finalize_call_record", exc)
            return None
        await self._mark("finalize_call_record")
        return facts

    async def _recording_steps(self, todo: tuple[PostCallStep, ...], timings: PostCallTimings) -> None:
        if "await_recording" in todo:
            event = await self._await_recording(timings)
            if event is None:
                self._skipped.append("store_recording")
                return
            await self._mark("await_recording")
        else:
            # An earlier run received the recording but did not store it: the edge stored the webhook.
            self._phase = "await_recording"
            event = self._recording_event or (await self._find_saved_recording())
            if event is None:
                self._recording = "skipped"
                self._skipped.append("store_recording")
                return
        await self._store_recording(event)

    async def _await_recording(self, timings: PostCallTimings) -> CarrierEvent | None:
        self._begin("await_recording")
        if self._recording_event is None:
            found = await self._find_saved_recording()
            # A signal that arrived while the lookup ran wins (it is the first one the edge sent).
            self._recording_event = self._recording_event or found
        if self._recording_event is None:
            self._raise_if_out_of_time()
            try:
                await workflow.wait_condition(
                    lambda: self._recording_event is not None,
                    timeout=timedelta(seconds=timings.recording_wait_s),
                )
            except TimeoutError:
                await self._report_recording_missing()
                return None
        return self._recording_event

    async def _find_saved_recording(self) -> CarrierEvent | None:
        try:
            found: SavedRecording = await workflow.execute_activity(
                "find_saved_recording",
                self._call,
                start_to_close_timeout=BOOKKEEPING_TIMEOUT,
                retry_policy=BOOKKEEPING_RETRY,
                result_type=SavedRecording,
            )
        except ActivityError as exc:
            self._raise_if_out_of_time()
            workflow.logger.warning("postcall.recording_lookup_failed error=%s", _cause_type(exc))
            return None
        return found.event

    async def _report_recording_missing(self) -> None:
        self._recording = "missing"
        workflow.logger.warning("postcall.recording_missing")
        try:
            await workflow.execute_activity(
                "report_recording_missing",
                self._call,
                start_to_close_timeout=BOOKKEEPING_TIMEOUT,
                retry_policy=BOOKKEEPING_RETRY,
                result_type=bool,
            )
        except ActivityError as exc:
            self._raise_if_out_of_time()
            workflow.logger.warning("postcall.recording_missing_unrecorded error=%s", _cause_type(exc))
            return
        await self._mark("await_recording")

    async def _store_recording(self, event: CarrierEvent) -> None:
        self._begin("store_recording")
        try:
            stored: StoreRecordingResult = await workflow.execute_activity(
                "store_recording",
                StoreRecordingInput(call_id=self._call.call_id, org_id=self._call.org_id, event=event),
                start_to_close_timeout=STORE_RECORDING_TIMEOUT,
                retry_policy=STORE_RECORDING_RETRY,
                result_type=StoreRecordingResult,
            )
        except ActivityError as exc:
            self._recording = "skipped"
            self._fail("store_recording", exc)
            return
        self._recording = stored.status
        await self._mark("store_recording")

    async def _finalize_billing(self) -> None:
        self._begin("finalize_billing")
        try:
            billed: BillingResult = await workflow.execute_activity(
                "finalize_billing",
                self._call,
                start_to_close_timeout=BILLING_TIMEOUT,
                retry_policy=BILLING_RETRY,
                result_type=BillingResult,
            )
        except ActivityError as exc:
            # The billing reconcile scans ended calls (specs/24 §9), so this call is picked up there.
            self._fail("finalize_billing", exc)
            return
        self._price_micros = billed.price_micros
        await self._mark("finalize_billing")

    async def _emit_completed(self) -> CompletedStatus | None:
        """Step 13; the ``CompletedResult`` status, ``None`` when it failed."""
        self._phase = "emit_completed"
        self._ran.append("emit_completed")
        try:
            completed: CompletedResult = await workflow.execute_activity(
                "emit_completed",
                self._call,
                start_to_close_timeout=EMIT_TIMEOUT,
                retry_policy=EMIT_RETRY,
                result_type=CompletedResult,
            )
        except ActivityError as exc:
            workflow.logger.warning("postcall.step_failed step=emit_completed error=%s", _cause_type(exc))
            self._failed.append("emit_completed")
            return None
        if completed.status in _MARKED_COMPLETIONS:
            await self._mark("emit_completed")
        return completed.status

    def _begin(self, step: PostCallStep) -> None:
        """Enter ``step``; after the budget ran out nothing new starts (a cancelled activity may surface as
        an ``ActivityError`` that a step wrapper swallowed)."""
        self._raise_if_out_of_time()
        self._phase = step
        self._ran.append(step)

    def _fail(self, step: PostCallStep, error: ActivityError) -> None:
        self._raise_if_out_of_time()
        workflow.logger.warning("postcall.step_failed step=%s error=%s", step, _cause_type(error))
        self._failed.append(step)

    def _raise_if_out_of_time(self) -> None:
        """Temporal reports a cancelled activity as ``ActivityError(cause=CancelledError)``: every handler
        that swallows an ``ActivityError`` calls this first, so a budget cancel stops the body instead of
        letting it go on (to the recording wait, a mark, or the next step)."""
        if self._out_of_time:
            raise asyncio.CancelledError

    async def _mark(self, step: PostCallStep) -> None:
        """Record a completed step; a failure is logged only (the step is idempotent and re-runs)."""
        try:
            await workflow.execute_activity(
                "mark_postcall_step",
                PostCallStepInput(call_id=self._call.call_id, org_id=self._call.org_id, step=step),
                start_to_close_timeout=BOOKKEEPING_TIMEOUT,
                retry_policy=BOOKKEEPING_RETRY,
                result_type=bool,
            )
        except ActivityError as exc:
            self._raise_if_out_of_time()
            workflow.logger.warning("postcall.mark_failed step=%s error=%s", step, _cause_type(exc))
