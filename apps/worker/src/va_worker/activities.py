"""The worker's activity instances and their task queue registrations (specs/01-architecture.md §1,
Decision P05-T06d).

One instance of each activity class per process, sharing one call limiter, one event publisher (activities
write their ``call_events`` rows in their own transactions, so it has no sink), and one Redis call context
store. The shared Telnyx HTTP pool and client factory come from :mod:`va_telnyx.settings`; the opener
pre-render (specs/07 §11 Decision P05-T10d) shares the process's phrase cache. With object storage
configured, ``store_recording`` (specs/06 §11 Decision P05-T14) is polled on the post-call queue.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from va_billing.limits import CallLimiter
from va_common.settings import TemporalSettings
from va_common.storage import ObjectStore, ServerSideEncryption
from va_common.time import Clock
from va_db.call_context import CallContextStore
from va_db.org_settings import OrgSettingsReader
from va_db.session import Database
from va_domain.post_call import postcall_task_queue
from va_events import EventPublisher
from va_telnyx import TelnyxClientFactory
from va_worker.task_queues import TaskQueueRegistration
from va_workflows.activities.call_context import CallContextActivities
from va_workflows.activities.call_end import CallEndActivities
from va_workflows.activities.dial_request import DialSettings
from va_workflows.activities.liveness import CallLivenessActivities
from va_workflows.activities.prerender import OpenerPrerender
from va_workflows.activities.recording import RecordingActivities
from va_workflows.activities.retry import RetryActivities
from va_workflows.activities.telephony import TelephonyActivities
from va_workflows.registration import CALL_WORKFLOWS

__all__ = [
    "RecordingStorage",
    "WorkerActivities",
    "main_registrations",
]


@dataclass(frozen=True, slots=True)
class RecordingStorage:
    """Where recordings are copied: the object store, ``S3_BUCKET_RECORDINGS``, and the SSE headers."""

    store: ObjectStore
    bucket: str
    sse: ServerSideEncryption | None


@dataclass(frozen=True, slots=True)
class WorkerActivities:
    """Every activity instance the worker polls."""

    context: CallContextActivities
    telephony: TelephonyActivities
    call_end: CallEndActivities
    liveness: CallLivenessActivities
    retry: RetryActivities
    recording: RecordingActivities | None = None

    @classmethod
    def build(  # noqa: PLR0913 - the process's shared collaborators, keyword-only
        cls,
        *,
        database: Database,
        redis: Redis,
        telnyx: TelnyxClientFactory,
        dial: DialSettings,
        clock: Clock,
        prerender: OpenerPrerender | None = None,
        storage: RecordingStorage | None = None,
    ) -> WorkerActivities:
        limiter = CallLimiter(redis, clock=clock)
        publisher = EventPublisher(redis, call_events=None)
        recording = (
            None
            if storage is None
            else RecordingActivities(
                database=database,
                telnyx=telnyx,
                store=storage.store,
                bucket=storage.bucket,
                sse=storage.sse,
                publisher=publisher,
                org_settings=OrgSettingsReader(database, redis),
                clock=clock,
            )
        )
        return cls(
            context=CallContextActivities(database, CallContextStore(redis), clock, prerender=prerender),
            telephony=TelephonyActivities(
                database=database,
                telnyx=telnyx,
                limiter=limiter,
                publisher=publisher,
                settings=dial,
                clock=clock,
            ),
            call_end=CallEndActivities(
                database=database, limiter=limiter, publisher=publisher, redis=redis, clock=clock
            ),
            liveness=CallLivenessActivities(redis=redis, clock=clock),
            retry=RetryActivities(database=database, publisher=publisher, clock=clock),
            recording=recording,
        )

    def main_queue(self) -> tuple[Callable[..., Any], ...]:
        """The ``CallWorkflow`` activities, by their registered names."""
        return (
            self.context.prefetch_call_context,
            self.telephony.dial,
            self.telephony.hangup_call,
            self.call_end.end_call,
            self.liveness.watch_call_heartbeat,
            self.liveness.call_presence,
            self.retry.schedule_retry_call,
        )

    def postcall_queue(self) -> tuple[Callable[..., Any], ...]:
        """The post-call activities that exist so far (``store_recording`` when storage is configured)."""
        return () if self.recording is None else (self.recording.store_recording,)


def main_registrations(
    settings: TemporalSettings, activities: WorkerActivities
) -> tuple[TaskQueueRegistration, ...]:
    """``CallWorkflow`` and its activities on ``TEMPORAL_TASK_QUEUE``, plus the post-call activities on the
    post-call queue when there are any.

    No workflow is registered on the post-call queue until ``PostCallWorkflow`` exists: an activities-only
    worker polls no workflow tasks, so the abandoned child keeps waiting there.
    """
    main = TaskQueueRegistration(settings.temporal_task_queue, CALL_WORKFLOWS, activities.main_queue())
    postcall = activities.postcall_queue()
    if not postcall:
        return (main,)
    return (main, TaskQueueRegistration(postcall_task_queue(settings.temporal_task_queue), (), postcall))
