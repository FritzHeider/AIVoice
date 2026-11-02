"""The worker's activity instances and the main task queue registration (specs/01-architecture.md §1,
Decision P05-T06d).

One instance of each activity class per process, sharing one call limiter, one event publisher (activities
write their ``call_events`` rows in their own transactions, so it has no sink), and one Redis call context
store. :func:`telnyx_http` builds the shared Telnyx HTTP pool of specs/06 §2.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

import httpx
from redis.asyncio import Redis

from va_billing.limits import CallLimiter
from va_common.http import http_client
from va_common.settings import BaseServiceSettings, TemporalSettings
from va_common.time import Clock
from va_db.call_context import CallContextStore
from va_db.session import Database
from va_events import EventPublisher
from va_telnyx import KeyResolver, TelnyxClientFactory, TelnyxRateLimiter
from va_worker.settings import WorkerSettings
from va_worker.task_queues import TaskQueueRegistration
from va_workflows.activities.call_context import CallContextActivities
from va_workflows.activities.call_end import CallEndActivities
from va_workflows.activities.dial_request import DialSettings
from va_workflows.activities.liveness import CallLivenessActivities
from va_workflows.activities.telephony import TelephonyActivities
from va_workflows.registration import CALL_WORKFLOWS

__all__ = [
    "TELNYX_HTTP_TIMEOUT",
    "TELNYX_MAX_CONNECTIONS",
    "TELNYX_MAX_KEEPALIVE",
    "WorkerActivities",
    "main_registrations",
    "telnyx_factory",
    "telnyx_http",
]

TELNYX_HTTP_TIMEOUT: Final = httpx.Timeout(connect=2.0, read=8.0, write=5.0, pool=2.0)
TELNYX_MAX_CONNECTIONS: Final = 200
TELNYX_MAX_KEEPALIVE: Final = 50


def telnyx_http(settings: BaseServiceSettings) -> httpx.AsyncClient:
    """The shared Telnyx pool: HTTP/2, the specs/06 §2 timeouts and limits; the caller closes it."""
    return http_client(
        settings,
        timeout=TELNYX_HTTP_TIMEOUT,
        http2=True,
        max_connections=TELNYX_MAX_CONNECTIONS,
        max_keepalive=TELNYX_MAX_KEEPALIVE,
    )


def telnyx_factory(
    settings: WorkerSettings,
    *,
    http: httpx.AsyncClient,
    redis: Redis,
    resolve_key: KeyResolver,
    clock: Clock,
) -> TelnyxClientFactory:
    """Platform client on ``TELNYX_API_KEY``, BYO clients through ``resolve_key``, one shared rate limiter.

    No Telnyx metrics: they are not in the worker catalog (specs/27 §3) until the worker exports per-activity
    metrics.
    """
    return TelnyxClientFactory(
        base_url=settings.telnyx_api_base,
        http=http,
        limiter=TelnyxRateLimiter(redis, clock=clock),
        platform_api_key=settings.telnyx_api_key.get_secret_value(),
        resolve_key=resolve_key,
        clock=clock,
    )


@dataclass(frozen=True, slots=True)
class WorkerActivities:
    """Every activity instance the main queue polls."""

    context: CallContextActivities
    telephony: TelephonyActivities
    call_end: CallEndActivities
    liveness: CallLivenessActivities

    @classmethod
    def build(
        cls,
        *,
        database: Database,
        redis: Redis,
        telnyx: TelnyxClientFactory,
        dial: DialSettings,
        clock: Clock,
    ) -> WorkerActivities:
        limiter = CallLimiter(redis, clock=clock)
        publisher = EventPublisher(redis, call_events=None)
        return cls(
            context=CallContextActivities(database, CallContextStore(redis), clock),
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
        )


def main_registrations(
    settings: TemporalSettings, activities: WorkerActivities
) -> tuple[TaskQueueRegistration, ...]:
    """``CallWorkflow`` and its activities on ``TEMPORAL_TASK_QUEUE``.

    The post-call queue is not polled until ``PostCallWorkflow`` exists: the abandoned child waits there.
    """
    return (TaskQueueRegistration(settings.temporal_task_queue, CALL_WORKFLOWS, activities.main_queue()),)
