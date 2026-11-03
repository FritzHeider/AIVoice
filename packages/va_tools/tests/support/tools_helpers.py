"""va_tools test helpers: executors over a plain (respx-patched) client or the guarded production client."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

import httpx
from clock_helpers import RecordingClock
from dns_fakes import MappedResolver
from service_helpers import settings_of

from va_common.settings import WebhookSafetySettings
from va_engine.ports import HttpCall, HttpMethod
from va_tools.http import HttpExecutor, Signer, open_http_executor

__all__ = ["API", "call", "guarded_executor", "plain_executor"]

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
