"""``PostCallWorkflow`` test rig: scripted fake activities with the production names on the time-skipping
server (specs/18 §1 Decision P05-T18b).

Signals are sent from inside a fake activity (``FakePostCallActivities.send``), the way the edge signals while
the workflow runs; time skips only while no activity is running, so the 10 min recording wait elapses at once.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from workflow_rig import HEARTBEAT_THROTTLE, POSTCALL_QUEUE

from va_domain.carrier import CarrierEvent
from va_domain.post_call import (
    RECORDING_SAVED_SIGNAL,
    PostCallTimings,
    PostCallWorkflowInput,
    postcall_workflow_id,
)
from va_workflows.activities.inputs import CallActivityInput
from va_workflows.activities.post_call_inputs import (
    BillingResult,
    CallRecordFacts,
    CompletedResult,
    PostCallStepInput,
    SavedRecording,
)
from va_workflows.activities.recording_inputs import StoreRecordingInput, StoreRecordingResult
from va_workflows.post_call import PostCallResult, PostCallWorkflow

__all__ = [
    "CONNECTED_RECORDED",
    "FakePostCallActivities",
    "PostCallRun",
    "facts",
    "postcall_start",
    "recording_event",
    "run_post_call",
]

type Script[T] = T | Exception


def facts(
    *,
    connected: bool = True,
    wanted: bool = False,
    storage: bool = True,
    done: Sequence[str] = (),
) -> CallRecordFacts:
    return CallRecordFacts(
        connected=connected, recording_wanted=wanted, recording_storage=storage, done_steps=tuple(done)
    )


CONNECTED_RECORDED = facts(wanted=True)


def postcall_start(timings: PostCallTimings | None = None) -> PostCallWorkflowInput:
    return PostCallWorkflowInput(call_id=uuid.uuid4(), org_id=uuid.uuid4(), timings=timings)


def recording_event(start: PostCallWorkflowInput, event_id: str = "evt-saved") -> CarrierEvent:
    return CarrierEvent(
        type="call.recording.saved",
        leg="primary",
        call_id=start.call_id,
        org_id=start.org_id,
        telnyx_event_id=event_id,
        occurred_at=datetime(2026, 11, 2, tzinfo=UTC),
        payload={"recording_id": event_id},
    )


def _scripted[T](value: Script[T]) -> T:
    if isinstance(value, Exception):
        raise value
    return value


@dataclass
class FakePostCallActivities:
    """The post-call activities scripted per test; ``calls`` lists ``(name, input)`` in order, ``marks`` the
    steps ``mark_postcall_step`` recorded. ``send`` maps an activity name to ``recording_saved`` events to
    signal from inside it (before it returns). An activity named in ``block`` heartbeats until it is
    cancelled (then it is listed in ``cancelled``) and never returns its script; it keeps the time-skipping
    server on wall time, so a test pairs it with a budget of a few seconds."""

    record: Script[CallRecordFacts] = field(default_factory=facts)
    found: list[Script[SavedRecording]] = field(default_factory=lambda: [SavedRecording()])
    missing: Script[bool] = True
    store: Script[StoreRecordingResult] = field(
        default_factory=lambda: StoreRecordingResult(status="stored", s3_key="k", duration_ms=1000)
    )
    billing: Script[BillingResult] = field(
        default_factory=lambda: BillingResult(price_micros=180_000, hold="captured", posted=2)
    )
    completed: Script[CompletedResult] = field(default_factory=lambda: CompletedResult(status="emitted"))
    mark: Script[bool] = True
    send: dict[str, list[CarrierEvent]] = field(default_factory=dict)
    block: frozenset[str] = frozenset()
    cancelled: list[str] = field(default_factory=list)
    calls: list[tuple[str, BaseModel]] = field(default_factory=list)
    client: Client | None = None

    def names(self) -> list[str]:
        """The step activities in order (``mark_postcall_step`` left out; see ``marks``)."""
        return [name for name, _ in self.calls if name != "mark_postcall_step"]

    def marks(self) -> list[str]:
        return [value.step for name, value in self.calls if isinstance(value, PostCallStepInput)]

    def stored_events(self) -> list[CarrierEvent]:
        return [value.event for _, value in self.calls if isinstance(value, StoreRecordingInput)]

    def activities(self) -> Sequence[Callable[..., Any]]:
        return [
            self.finalize_call_record,
            self.find_saved_recording,
            self.report_recording_missing,
            self.mark_postcall_step,
            self.store_recording,
            self.finalize_billing,
            self.emit_completed,
        ]

    async def _enter(self, name: str, request: BaseModel) -> None:
        self.calls.append((name, request))
        events = self.send.pop(name, [])
        if not events:
            return
        client = self.client
        workflow_id = activity.info().workflow_id
        assert client is not None
        assert workflow_id is not None
        handle = client.get_workflow_handle(workflow_id)
        for event in events:
            await handle.signal(RECORDING_SAVED_SIGNAL, event)

    async def _hold(self, name: str) -> None:
        if name not in self.block:
            return
        try:
            while True:
                activity.heartbeat()
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            self.cancelled.append(name)
            raise

    @activity.defn(name="finalize_call_record")
    async def finalize_call_record(self, request: CallActivityInput) -> CallRecordFacts:
        await self._enter("finalize_call_record", request)
        await self._hold("finalize_call_record")
        return _scripted(self.record)

    @activity.defn(name="find_saved_recording")
    async def find_saved_recording(self, request: CallActivityInput) -> SavedRecording:
        await self._enter("find_saved_recording", request)
        await self._hold("find_saved_recording")
        return _scripted(self.found.pop(0) if len(self.found) > 1 else self.found[0])

    @activity.defn(name="report_recording_missing")
    async def report_recording_missing(self, request: CallActivityInput) -> bool:
        await self._enter("report_recording_missing", request)
        await self._hold("report_recording_missing")
        return _scripted(self.missing)

    @activity.defn(name="mark_postcall_step")
    async def mark_postcall_step(self, request: PostCallStepInput) -> bool:
        await self._enter("mark_postcall_step", request)
        await self._hold("mark_postcall_step")
        return _scripted(self.mark)

    @activity.defn(name="store_recording")
    async def store_recording(self, request: StoreRecordingInput) -> StoreRecordingResult:
        await self._enter("store_recording", request)
        await self._hold("store_recording")
        return _scripted(self.store)

    @activity.defn(name="finalize_billing")
    async def finalize_billing(self, request: CallActivityInput) -> BillingResult:
        await self._enter("finalize_billing", request)
        await self._hold("finalize_billing")
        return _scripted(self.billing)

    @activity.defn(name="emit_completed")
    async def emit_completed(self, request: CallActivityInput) -> CompletedResult:
        await self._enter("emit_completed", request)
        await self._hold("emit_completed")
        return _scripted(self.completed)


@dataclass(frozen=True, slots=True)
class PostCallRun:
    result: PostCallResult
    phase: str
    history: list[Any]

    def timers(self) -> list[float]:
        """The durations (s) of the workflow timers started, in order."""
        return [
            event.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta().total_seconds()
            for event in self.history
            if EventType.Name(event.event_type) == "EVENT_TYPE_TIMER_STARTED"
        ]


async def run_post_call(
    env: WorkflowEnvironment,
    fakes: FakePostCallActivities,
    start: PostCallWorkflowInput | None = None,
    *,
    signals: Sequence[CarrierEvent] = (),
) -> PostCallRun:
    """Run ``PostCallWorkflow`` to completion with ``fakes``; ``signals`` go out right after the start."""
    begin = start or postcall_start()
    fakes.client = env.client
    async with Worker(
        env.client,
        task_queue=POSTCALL_QUEUE,
        workflows=[PostCallWorkflow],
        activities=fakes.activities(),
        default_heartbeat_throttle_interval=HEARTBEAT_THROTTLE,
        max_heartbeat_throttle_interval=HEARTBEAT_THROTTLE,
    ):
        handle = await env.client.start_workflow(
            PostCallWorkflow.run, begin, id=postcall_workflow_id(begin.call_id), task_queue=POSTCALL_QUEUE
        )
        for event in signals:
            await handle.signal(RECORDING_SAVED_SIGNAL, event)
        result = await handle.result()
        phase = await handle.query("postcall_phase", result_type=str)
        history = list((await handle.fetch_history()).events)
    return PostCallRun(result, phase, history)
