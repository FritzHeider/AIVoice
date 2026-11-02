"""``CallWorkflow`` test rig: the Temporal time-skipping server, scripted fake activities, and a stub
``PostCallWorkflow``.

Time skips only while no activity is running, so a test whose conversation must wait on a timer uses the watch
mode ``fail`` (the workflow then continues without a watch) instead of ``park``. Carrier events and other
signals are sent from inside a fake activity (``FakeCallActivities.send``), the way the edge signals while the
workflow waits on the carrier: they are recorded before that activity's result is processed.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from postcall_stub import StubPostCall
from pydantic import BaseModel
from temporalio import activity
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowHandle
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from va_domain.call_workflow import CallWorkflowInput, call_workflow_id
from va_domain.carrier import CARRIER_SIGNAL, CarrierEvent, CarrierLeg
from va_domain.post_call import PostCallWorkflowInput, postcall_workflow_id
from va_workflows.activities.context_inputs import PrefetchResult
from va_workflows.activities.errors import CALL_NOT_FOUND
from va_workflows.activities.inputs import CallActivityInput
from va_workflows.activities.liveness_inputs import CallPresence, WatchInput, WatchResult
from va_workflows.activities.retry_inputs import RetryPlan
from va_workflows.activities.telephony_inputs import (
    DialInput,
    DialOutcome,
    EndCallInput,
    EndCallResult,
    HangupInput,
    HangupResult,
)
from va_workflows.calls import CallWorkflow, CallWorkflowResult

__all__ = [
    "POSTCALL_QUEUE",
    "RETRY",
    "TASK_QUEUE",
    "CallRun",
    "FakeCallActivities",
    "Signal",
    "WatchMode",
    "boom",
    "call_start",
    "carrier",
    "dialing",
    "not_found",
    "postcall_input",
    "postcall_running",
    "run_call",
    "workflow_env",
]

TASK_QUEUE = "va-test-main"
POSTCALL_QUEUE = "va-test-postcall"
HEARTBEAT_THROTTLE = timedelta(milliseconds=100)
"""Activity heartbeats (and so cancellations of the parked watch) reach the server within 100 ms."""

type Signal = tuple[str, BaseModel | None]
type WatchMode = Literal["park", "lose", "fail"]


def call_start(*, scheduled_for: datetime | None = None) -> CallWorkflowInput:
    return CallWorkflowInput(call_id=uuid.uuid4(), org_id=uuid.uuid4(), scheduled_for=scheduled_for)


def carrier(
    event_type: str,
    start: CallWorkflowInput,
    *,
    leg: CarrierLeg = "primary",
    payload: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> Signal:
    """A ``signal_carrier_event`` for the call, with a unique ``telnyx_event_id`` unless one is given."""
    event = CarrierEvent(
        type=event_type,
        leg=leg,
        call_id=start.call_id,
        org_id=start.org_id,
        telnyx_event_id=event_id or f"evt-{uuid.uuid4()}",
        occurred_at=datetime(2026, 11, 1, tzinfo=UTC),
        payload=payload or {},
    )
    return (CARRIER_SIGNAL, event)


def dialing(max_duration_s: int | None = 300) -> DialOutcome:
    return DialOutcome(
        result="dialing", status="dialing", call_control_id="v3:fake", max_duration_s=max_duration_s
    )


def not_found() -> ApplicationError:
    return ApplicationError("call not found", type=CALL_NOT_FOUND, non_retryable=True)


def boom(message: str = "broken") -> ApplicationError:
    """A non-retryable activity failure that is not ``CallNotFound``."""
    return ApplicationError(message, type="Boom", non_retryable=True)


def _scripted[T](value: T | Exception) -> T:
    if isinstance(value, Exception):
        raise value
    return value


RETRY = "schedule_retry_call"


@dataclass
class FakeCallActivities:
    """Activities with the production names, scripted per test; ``calls`` lists ``(name, input)`` in order and
    ``scheduled`` the server time each activity attempt was scheduled at. ``retry`` scripts
    ``schedule_retry_call`` (by default no retry)."""

    prefetch: PrefetchResult | Exception = field(
        default_factory=lambda: PrefetchResult(stored=True, status="queued", ttl_s=3900)
    )
    dials: list[DialOutcome | Exception] = field(default_factory=lambda: [dialing()])
    on_final_attempt: DialOutcome | None = None
    end_results: list[EndCallResult | Exception] = field(
        default_factory=lambda: [EndCallResult(changed=True, status="completed")]
    )
    hangup: HangupResult | Exception = field(default_factory=lambda: HangupResult(sent=True, reason="sent"))
    presence: list[CallPresence | Exception] = field(
        default_factory=lambda: [CallPresence(owner=False, heartbeat=False)]
    )
    retry: list[RetryPlan | Exception] = field(default_factory=lambda: [RetryPlan(reason="no_retry")])
    watch: WatchMode = "park"
    send: dict[str, list[Signal]] = field(default_factory=dict)
    on_watch_cancel: list[Signal] = field(default_factory=list)
    calls: list[tuple[str, BaseModel]] = field(default_factory=list)
    scheduled: list[tuple[str, datetime]] = field(default_factory=list)
    workflow_ids: list[str] = field(default_factory=list)
    watch_cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    client: Client | None = None

    def names(self) -> list[str]:
        """The lifecycle activities in order; ``schedule_retry_call``, which every finished call runs last,
        is left out (``retry_calls`` lists it)."""
        return [name for name, _ in self.calls if name != RETRY]

    def retry_calls(self, call_id: uuid.UUID | None = None) -> list[CallActivityInput]:
        """``schedule_retry_call`` inputs, only those of ``call_id``'s workflow when given."""
        calls = self.calls if call_id is None else self.calls_of(call_id)
        return [value for called, value in calls if called == RETRY and isinstance(value, CallActivityInput)]

    def calls_of(self, call_id: uuid.UUID) -> list[tuple[str, BaseModel]]:
        """The ``calls`` made by ``call_id``'s own ``CallWorkflow`` (a retry child shares these fakes)."""
        wanted = call_workflow_id(call_id)
        return [
            call
            for call, workflow_id in zip(self.calls, self.workflow_ids, strict=True)
            if workflow_id == wanted
        ]

    def inputs[M: BaseModel](self, name: str, model: type[M]) -> list[M]:
        return [value for called, value in self.calls if called == name and isinstance(value, model)]

    def activities(self) -> Sequence[Callable[..., Any]]:
        return [
            self.prefetch_call_context,
            self.dial,
            self.end_call,
            self.hangup_call,
            self.call_presence,
            self.watch_call_heartbeat,
            self.schedule_retry_call,
        ]

    async def _enter(self, name: str, request: BaseModel) -> None:
        info = activity.info()
        self.calls.append((name, request))
        self.workflow_ids.append(info.workflow_id or "")
        self.scheduled.append((name, info.current_attempt_scheduled_time))
        await self._signal(self.send.pop(name, []))

    async def _signal(self, signals: Sequence[Signal]) -> None:
        """Send ``signals`` to the workflow of the running activity."""
        client = self.client
        assert client is not None
        workflow_id = activity.info().workflow_id
        assert workflow_id is not None
        await _send(client.get_workflow_handle(workflow_id), signals)

    @staticmethod
    def _next[T](script: list[T | Exception]) -> T | Exception:
        return script.pop(0) if len(script) > 1 else script[0]

    @activity.defn(name="prefetch_call_context")
    async def prefetch_call_context(self, request: CallActivityInput) -> PrefetchResult:
        await self._enter("prefetch_call_context", request)
        return _scripted(self.prefetch)

    @activity.defn(name="dial")
    async def dial(self, request: DialInput) -> DialOutcome:
        await self._enter("dial", request)
        if request.final_attempt and self.on_final_attempt is not None:
            return self.on_final_attempt
        return _scripted(self._next(self.dials))

    @activity.defn(name="end_call")
    async def end_call(self, request: EndCallInput) -> EndCallResult:
        await self._enter("end_call", request)
        return _scripted(self._next(self.end_results))

    @activity.defn(name="hangup_call")
    async def hangup_call(self, request: HangupInput) -> HangupResult:
        await self._enter("hangup_call", request)
        return _scripted(self.hangup)

    @activity.defn(name="call_presence")
    async def call_presence(self, request: CallActivityInput) -> CallPresence:
        await self._enter("call_presence", request)
        return _scripted(self._next(self.presence))

    @activity.defn(name=RETRY)
    async def schedule_retry_call(self, request: CallActivityInput) -> RetryPlan:
        await self._enter(RETRY, request)
        return _scripted(self._next(self.retry))

    @activity.defn(name="watch_call_heartbeat")
    async def watch_call_heartbeat(self, request: WatchInput) -> WatchResult:
        await self._enter("watch_call_heartbeat", request)
        if self.watch == "lose":
            return WatchResult(reason="heartbeat_lost", missing_s=20.0)
        if self.watch == "fail":
            raise boom()
        try:
            while True:
                activity.heartbeat()
                await asyncio.sleep(0.05)
        except asyncio.CancelledError:
            self.watch_cancelled.set()
            await self._signal(self.on_watch_cancel)
            raise


async def _send(handle: WorkflowHandle[Any, Any], signals: Sequence[Signal]) -> None:
    for signal, arg in signals:
        if arg is None:
            await handle.signal(signal)
        else:
            await handle.signal(signal, arg)


@asynccontextmanager
async def workflow_env() -> AsyncIterator[WorkflowEnvironment]:
    """A time-skipping test server whose client uses the pydantic data converter (as the worker does)."""
    async with await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter) as env:
        yield env


@dataclass(frozen=True, slots=True)
class CallRun:
    """The workflow result and the input the stub ``PostCallWorkflow`` child ran with (``None``: no child
    awaited)."""

    result: CallWorkflowResult
    post_call: PostCallWorkflowInput | None
    phase: str
    history: list[str]


async def run_call(
    env: WorkflowEnvironment,
    fakes: FakeCallActivities,
    start: CallWorkflowInput,
    *,
    signals: Sequence[Signal] = (),
    await_post_call: bool = True,
) -> CallRun:
    """Run ``CallWorkflow`` to completion with ``fakes``; ``signals`` are sent right after the start. Unless
    the outcome is ``not_found`` (or ``await_post_call`` is false), the post-call child is awaited too."""
    fakes.client = env.client
    async with (
        Worker(
            env.client,
            task_queue=TASK_QUEUE,
            workflows=[CallWorkflow],
            activities=fakes.activities(),
            default_heartbeat_throttle_interval=HEARTBEAT_THROTTLE,
            max_heartbeat_throttle_interval=HEARTBEAT_THROTTLE,
        ),
        Worker(env.client, task_queue=POSTCALL_QUEUE, workflows=[StubPostCall]),
    ):
        handle = await env.client.start_workflow(
            CallWorkflow.run, start, id=start.workflow_id(), task_queue=TASK_QUEUE
        )
        await _send(handle, signals)
        result = await handle.result()
        phase = await handle.query("phase", result_type=str)
        history = [EventType.Name(event.event_type) for event in (await handle.fetch_history()).events]
        post_call = None
        if result.outcome != "not_found" and await_post_call:
            post_call = await postcall_input(env, start)
        return CallRun(result, post_call, phase, history)


async def postcall_input(env: WorkflowEnvironment, start: CallWorkflowInput) -> PostCallWorkflowInput:
    """The input the stub ``PostCallWorkflow`` child of the call ran with."""
    handle = env.client.get_workflow_handle(
        postcall_workflow_id(start.call_id), result_type=PostCallWorkflowInput
    )
    child: PostCallWorkflowInput = await handle.result()
    return child


async def postcall_running(env: WorkflowEnvironment, start: CallWorkflowInput) -> bool | None:
    """``None`` when no ``PostCallWorkflow`` exists for the call, else whether it is still running."""
    handle = env.client.get_workflow_handle(postcall_workflow_id(start.call_id))
    try:
        description = await handle.describe()
    except RPCError as exc:
        if exc.status == RPCStatusCode.NOT_FOUND:
            return None
        raise
    return description.close_time is None
