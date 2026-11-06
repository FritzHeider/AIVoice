"""va_tools test helpers: executors over a plain (respx-patched) client or the guarded production client,
a recording :class:`~va_tools.executor.ToolEvents`, a system tool whose run the test holds, a respx answer
the test holds (:class:`HeldAnswer`), a dynamic data fetcher on a manual clock (:func:`manual_fetcher`) and
the reader thread helpers (:func:`held_reader`, :func:`thread_recorder`, :func:`read_waits`).

An executor gives a read on the reader thread its request's ``timeout_ms`` on its clock. The frozen clock of
:func:`plain_executor` passes any deadline at once, so there such a read is a ``timeout``: tests whose answer
may leave the loop use :func:`manual_executor` (the deadline passes only when the test moves the clock).

The reader thread (:mod:`va_tools.offload`) is one per process: a test may hold it only through
:func:`held_reader`, which releases it when its block ends, so every test ends with the reader idle."""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Final

import httpx
import pytest
from clock_helpers import ManualClock, RecordingClock
from dns_fakes import MappedResolver
from redis.asyncio import Redis
from service_helpers import settings_of

from va_common.settings import WebhookSafetySettings
from va_engine.ports import HttpCall, HttpMethod
from va_llm.models import ToolCall
from va_tools.dynamic_data import DynamicDataFetcher
from va_tools.executor import SystemOutcome
from va_tools.http import MAX_RESPONSE_BYTES, HttpExecutor, Signer, open_http_executor
from va_tools.offload import off_loop

__all__ = [
    "API",
    "ORG",
    "GatedSystem",
    "HeldAnswer",
    "RecordedEvents",
    "call",
    "guarded_executor",
    "held_reader",
    "manual_executor",
    "manual_fetcher",
    "plain_executor",
    "read_waits",
    "thread_recorder",
]

API: Final = "https://api.acme.test"
ORG: Final = uuid.UUID("0193c5d2-7a41-7c3e-9b0d-5f2e8a1c4d71")
HOLD_CAP_S: Final = 10.0
"""A held reader lets go by itself after this long, so a failed test never wedges the process's reader."""


def call(
    path: str = "/lookup",
    *,
    method: HttpMethod = "POST",
    base: str = API,
    **fields: Any,
) -> HttpCall:
    """A rendered request to ``base + path``."""
    return HttpCall(method=method, url=f"{base}{path}", **fields)


@asynccontextmanager
async def plain_executor(
    *,
    clock: RecordingClock | None = None,
    allow_http: bool = False,
    call_id: str | None = "call-7",
    signer: Signer | None = None,
    max_bytes: int | None = None,
) -> AsyncIterator[tuple[HttpExecutor, RecordingClock]]:
    """An executor over a plain client (respx intercepts it) with a recording clock."""
    recording = clock if clock is not None else RecordingClock()
    async with httpx.AsyncClient() as client:
        extra = {} if max_bytes is None else {"max_bytes": max_bytes}
        yield (
            HttpExecutor(
                client, clock=recording, allow_http=allow_http, call_id=call_id, signer=signer, **extra
            ),
            recording,
        )


@asynccontextmanager
async def manual_executor(*, max_bytes: int | None = None) -> AsyncIterator[tuple[HttpExecutor, ManualClock]]:
    """An executor over a plain client (respx intercepts it) on a manual clock: a read on the reader
    thread is waited for, and runs out of time only when the test moves the clock past ``timeout_ms``."""
    clock = ManualClock()
    async with httpx.AsyncClient() as client:
        limit = MAX_RESPONSE_BYTES if max_bytes is None else max_bytes
        yield HttpExecutor(client, clock=clock, allow_http=False, call_id="call-7", max_bytes=limit), clock


class HeldAnswer:
    """A respx side effect that answers ``body`` once ``gate`` is set and records what arrived."""

    def __init__(self, body: dict[str, Any]) -> None:
        self.body = body
        self.gate = asyncio.Event()
        self.arrived = asyncio.Event()
        self.cancelled = False

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        del request
        self.arrived.set()
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return httpx.Response(200, json=self.body)


@asynccontextmanager
async def manual_fetcher(
    *, redis: Redis | None = None, org_id: uuid.UUID = ORG
) -> AsyncIterator[tuple[ManualClock, DynamicDataFetcher]]:
    """A dynamic data fetcher over the real executor (respx intercepts its client) on a manual clock."""
    clock = ManualClock()
    async with httpx.AsyncClient() as client:
        executor = HttpExecutor(client, clock=clock, allow_http=False, call_id="call-7")
        yield clock, DynamicDataFetcher(executor, redis=redis, org_id=org_id, clock=clock)


@asynccontextmanager
async def guarded_executor(resolver: MappedResolver, *, allow_private: bool) -> AsyncIterator[HttpExecutor]:
    """The production executor (:func:`open_http_executor`) with fake DNS."""
    settings = settings_of(WebhookSafetySettings, webhook_allow_private=allow_private)
    async with open_http_executor(
        settings, clock=RecordingClock(), allow_http=True, resolver=resolver
    ) as executor:
        yield executor


@dataclass(slots=True)
class RecordedEvents:
    """What an executor reported, in order."""

    tools: list[dict[str, Any]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def tool(self, data: dict[str, Any]) -> None:
        self.tools.append(data)

    def tool_failed(self, detail: str) -> None:
        self.failures.append(detail)

    def sms_sent(self, data: dict[str, Any]) -> None:
        self.tools.append({"sms_sent": data})


@dataclass(slots=True)
class GatedSystem:
    """A system tool whose run waits for ``gate`` (``outcome`` ``None`` = never finishes; an exception is
    raised)."""

    name: str
    outcome: SystemOutcome | Exception | None
    detaches: bool = True
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    finished: list[str] = field(default_factory=list)
    cancelled: bool = False

    async def run(self, call: ToolCall) -> SystemOutcome:
        self.started.set()
        try:
            await self.gate.wait()
            if self.outcome is None:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.finished.append(call.id)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        assert self.outcome is not None
        return self.outcome


@asynccontextmanager
async def held_reader() -> AsyncIterator[threading.Event]:
    """Occupies the process's reader thread for the block; yields the gate that releases it (still unset
    inside the block unless the test sets it)."""
    loop = asyncio.get_running_loop()
    running = asyncio.Event()
    gate = threading.Event()

    def hold() -> None:
        loop.call_soon_threadsafe(running.set)
        gate.wait(HOLD_CAP_S)

    holding = asyncio.ensure_future(off_loop(hold))
    try:
        async with asyncio.timeout(HOLD_CAP_S):
            await running.wait()
        yield gate
    finally:
        gate.set()
        await holding


def thread_recorder(monkeypatch: pytest.MonkeyPatch, module: ModuleType, name: str) -> list[str]:
    """Wraps ``module.name`` so that every call appends the name of the thread it runs on to the list
    returned (the call itself is unchanged)."""
    threads: list[str] = []
    original: Callable[..., Any] = getattr(module, name)

    def recording(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.current_thread().name)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, recording)
    return threads


def read_waits(monkeypatch: pytest.MonkeyPatch, module: ModuleType) -> asyncio.Event:
    """An event set when ``module`` hands a read to the reader thread (its ``off_loop`` is entered); once
    a test awaited it, the task that entered is waiting for the thread."""
    entered = asyncio.Event()
    original: Callable[[Callable[[], Any]], Any] = module.off_loop

    async def entering(work: Callable[[], Any]) -> Any:
        entered.set()
        return await original(work)

    monkeypatch.setattr(module, "off_loop", entering)
    return entered
