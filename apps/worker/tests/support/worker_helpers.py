"""Worker test doubles: a Temporal client source and controllable fake workers; settings that can dial."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any, cast

from service_helpers import settings_of
from temporalio.client import Client

from va_worker.runner import WorkerLike
from va_worker.settings import WorkerSettings
from va_worker.task_queues import TaskQueueRegistration


class FakeClientSource:
    def __init__(self) -> None:
        self.connects = 0

    async def client(self) -> Client:
        self.connects += 1
        return cast("Client", object())


class FakeWorker:
    def __init__(self, error: Exception | None = None) -> None:
        self.started = asyncio.Event()
        self._stop = asyncio.Event()
        self._running = False
        self.error = error
        self.shutdowns = 0

    @property
    def is_running(self) -> bool:
        return self._running

    async def run(self) -> None:
        self._running = True
        self.started.set()
        try:
            if self.error is not None:
                raise self.error
            await self._stop.wait()
        finally:
            self._running = False

    async def shutdown(self) -> None:
        self.shutdowns += 1
        self._stop.set()


class Factory:
    def __init__(self, *workers: FakeWorker) -> None:
        self.workers = list(workers)
        self.built: list[tuple[Any, str]] = []

    def __call__(self, client: Client, registration: TaskQueueRegistration) -> WorkerLike:
        self.built.append((client, registration.task_queue))
        return self.workers[len(self.built) - 1]


def dialing_settings(**overrides: Any) -> WorkerSettings:
    """Worker settings the container accepts: random stream secret and local master key, an app id."""
    values: dict[str, Any] = {
        "stream_token_secret": secrets.token_hex(32),
        "local_master_key": secrets.token_hex(32),
        "telnyx_call_control_app_id": "app-test",
        **overrides,
    }
    return settings_of(WorkerSettings, **values)
