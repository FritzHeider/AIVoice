"""The worker's activity instances and their task queue registrations (specs/01-architecture.md §1,
Decision P05-T06d).

One instance of each activity class per process, sharing one call limiter, one event publisher (activities
write their ``call_events`` rows in their own transactions, so it has no sink), and one Redis call context
store. The shared Telnyx HTTP pool and client factory come from :mod:`va_telnyx.settings`; the opener
pre-render (specs/07 §11 Decision P05-T10d) shares the process's phrase cache. The post-call queue runs
``PostCallWorkflow`` with its activities (specs/18 §1 Decision P05-T18b); ``store_recording`` (specs/06 §11
Decision P05-T14) is polled there when object storage is configured, and ``PathwayTestWorkflow`` with its
activities (specs/12 §8 Decision P07-T11c) when the text-run dependencies (LLM gateway, tool HTTP client)
are given.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from prometheus_client import Histogram
from redis.asyncio import Redis

from va_billing.limits import CallLimiter
from va_common.settings import TemporalSettings
from va_common.storage import RecordingStorage
from va_common.time import Clock
from va_db.call_context import CallContextStore
from va_db.org_settings import OrgSettingsReader
from va_db.session import Database
from va_domain.post_call import postcall_task_queue
from va_events import DbWebhookRouter, EventPublisher
from va_pathways.conversation import TextDeps
from va_telnyx import TelnyxClientFactory
from va_worker.task_queues import TaskQueueRegistration
from va_workflows.activities.call_context import CallContextActivities
from va_workflows.activities.call_end import CallEndActivities
from va_workflows.activities.dial_request import DialSettings
from va_workflows.activities.liveness import CallLivenessActivities
from va_workflows.activities.pathway_tests import PathwayTestActivities
from va_workflows.activities.post_call import PostCallActivities
from va_workflows.activities.prerender import OpenerPrerender
from va_workflows.activities.recording import RecordingActivities
from va_workflows.activities.retry import RetryActivities
from va_workflows.activities.telephony import TelephonyActivities
from va_workflows.registration import CALL_WORKFLOWS, POSTCALL_WORKFLOWS

__all__ = [
    "RecordingStorage",
    "WorkerActivities",
    "main_registrations",
]


@dataclass(frozen=True, slots=True)
class WorkerActivities:
    """Every activity instance the worker polls."""

    context: CallContextActivities
    telephony: TelephonyActivities
    call_end: CallEndActivities
    liveness: CallLivenessActivities
    retry: RetryActivities
    post_call: PostCallActivities
    recording: RecordingActivities | None = None
    pathway_tests: PathwayTestActivities | None = None

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
        postcall_step_ms: Histogram | None = None,
        text: TextDeps | None = None,
    ) -> WorkerActivities:
        limiter = CallLimiter(redis, clock=clock)
        publisher = EventPublisher(redis, call_events=None, router=DbWebhookRouter(database))
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
            post_call=PostCallActivities(
                database=database,
                publisher=publisher,
                clock=clock,
                storage=storage,
                step_ms=postcall_step_ms,
            ),
            recording=recording,
            pathway_tests=None
            if text is None
            else PathwayTestActivities(
                database=database,
                org_settings=OrgSettingsReader(database, redis),
                text=text,
                clock=clock,
                redis=redis,
            ),
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
        """The ``PostCallWorkflow`` activities (``store_recording`` when storage is configured) and the
        ``PathwayTestWorkflow`` activities (when the text-run dependencies are configured)."""
        post_call = self.post_call
        activities: list[Callable[..., Any]] = [
            post_call.finalize_call_record,
            post_call.find_saved_recording,
            post_call.report_recording_missing,
            post_call.mark_postcall_step,
            post_call.finalize_billing,
            post_call.emit_completed,
        ]
        if self.recording is not None:
            activities.append(self.recording.store_recording)
        tests = self.pathway_tests
        if tests is not None:
            activities.extend(
                (
                    tests.begin_pathway_test_run,
                    tests.run_pathway_unit_test,
                    tests.record_pathway_test_error,
                    tests.run_pathway_simulation,
                    tests.record_pathway_simulation_error,
                    tests.finish_pathway_test_run,
                    tests.settle_regression_publish,
                )
            )
        return tuple(activities)


def main_registrations(
    settings: TemporalSettings, activities: WorkerActivities
) -> tuple[TaskQueueRegistration, ...]:
    """``CallWorkflow`` and its activities on ``TEMPORAL_TASK_QUEUE``, ``PostCallWorkflow`` and
    ``PathwayTestWorkflow`` with their activities on the post-call queue."""
    return (
        TaskQueueRegistration(settings.temporal_task_queue, CALL_WORKFLOWS, activities.main_queue()),
        TaskQueueRegistration(
            postcall_task_queue(settings.temporal_task_queue), POSTCALL_WORKFLOWS, activities.postcall_queue()
        ),
    )
