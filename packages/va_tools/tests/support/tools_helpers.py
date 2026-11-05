"""va_tools test helpers: executors over a plain (respx-patched) client or the guarded production client,
a recording :class:`~va_tools.executor.ToolEvents` and a system tool whose run the test holds."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

import httpx
from clock_helpers import RecordingClock
from dns_fakes import MappedResolver
from service_helpers import settings_of

from va_common.settings import WebhookSafetySettings
from va_engine.ports import HttpCall, HttpMethod
from va_llm.models import ToolCall
from va_tools.executor import SystemOutcome
from va_tools.http import HttpExecutor, Signer, open_http_executor

__all__ = ["API", "GatedSystem", "RecordedEvents", "call", "guarded_executor", "plain_executor"]

API: Final = "https://api.acme.test"


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
