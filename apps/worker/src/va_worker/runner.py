"""Temporal worker lifecycle: one worker per registered task queue (specs/27-observability.md §9 liveness)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Final, Protocol

from temporalio.client import Client
from temporalio.worker import Worker

from va_common.logging import get_logger
from va_worker.task_queues import TaskQueueRegistration

__all__ = [
    "STOPPED_REASON",
    "ClientSource",
    "TemporalWorkers",
    "WorkerFactory",
    "WorkerLike",
    "temporal_worker",
]

STOPPED_REASON: Final = "temporal_worker_stopped"

_log = get_logger(__name__)


class WorkerLike(Protocol):
    @property
    def is_running(self) -> bool: ...

    async def run(self) -> None: ...

    async def shutdown(self) -> None: ...


class ClientSource(Protocol):
    async def client(self) -> Client: ...


type WorkerFactory = Callable[[Client, TaskQueueRegistration], WorkerLike]


def temporal_worker(client: Client, registration: TaskQueueRegistration) -> WorkerLike:
    return Worker(
        client,
        task_queue=registration.task_queue,
        workflows=list(registration.workflows),
        activities=list(registration.activities),
    )


class TemporalWorkers:
    """Runs a worker per registration until shutdown; liveness fails when a started worker stopped."""

    def __init__(
        self,
        registrations: Sequence[TaskQueueRegistration],
        *,
        worker_factory: WorkerFactory = temporal_worker,
    ) -> None:
        self._registrations = tuple(registrations)
        self._factory = worker_factory
        self._workers: list[WorkerLike] = []

    async def run(self, source: ClientSource) -> None:
        """Connect, then poll every registered queue; returns at once when nothing is registered."""
        if not self._registrations:
            _log.info("worker.no_task_queues")
            return
        client = await source.client()
        self._workers = [self._factory(client, registration) for registration in self._registrations]
        _log.info("worker.polling", task_queues=[r.task_queue for r in self._registrations])
        async with asyncio.TaskGroup() as group:
            for worker in self._workers:
                group.create_task(worker.run())

    async def shutdown(self) -> None:
        """Gracefully stop every running worker."""
        running = [worker for worker in self._workers if worker.is_running]
        await asyncio.gather(*(worker.shutdown() for worker in running))

    def liveness(self) -> str | None:
        """``temporal_worker_stopped`` when any started worker is no longer running."""
        if any(not worker.is_running for worker in self._workers):
            return STOPPED_REASON
        return None
