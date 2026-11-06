"""The shared HTTP executor of webhook nodes, scheduling nodes and tools (specs/13 §5 steps 4-6, §9).

:class:`HttpExecutor` implements :class:`va_engine.ports.HttpPort`: it sends an already rendered
:class:`~va_engine.ports.HttpCall` and never raises.

* Pre-flight: an unparseable URL or one without a host is ``invalid_url``; a scheme other than ``https`` (and
  ``http`` when allowed) is ``blocked``. Production clients come from :func:`open_http_executor`, whose
  :func:`va_common.safe_http.safe_client` vets every connection at connect time (DNS pinned, private ranges
  per ``WEBHOOK_ALLOW_PRIVATE``); a refused connection is ``blocked`` and never retried, a name that does
  not resolve is a (retried) ``connection`` failure.
* Headers: the rendered ones plus ``User-Agent: VoiceAgents-Tools/1.0``, ``X-VA-Call-Id`` and the optional
  signer's headers over the exact body bytes. A JSON body (``va_common.json.dumps``) is sent for every method
  but ``GET``.
* Timeouts: connect ≤ 2 s, every other phase ``timeout_ms``. Retries (up to ``retries``) on connect errors,
  timeouts and 5xx with a ``0.25 s * attempt`` backoff on the injected clock; never on 4xx.
* The body is read up to 1 MiB (``truncated`` beyond), parsed as JSON when it parses, else kept as text; the
  ``extract`` JSONPath pairs apply to JSON bodies only.
* The body is parsed and searched on the event loop only when its paths are cheap for its size
  (:func:`va_tools.jsonpath.inline_read`); otherwise on the process's reader thread
  (:func:`va_tools.offload.off_loop`). The outcome is the same either way. A request cancelled while its
  read waits for the thread or runs there returns at once (specs/13 §9 Decision P08-FIX-25).
* A read on the reader thread gets ``timeout_ms`` on the injected clock, waiting for the thread included.
  When it has not finished by then the outcome is ``timeout`` with no status, body or extracted value (the
  answer is not used at all), and the request is not sent again: it was answered, so the retries do not
  apply. A read still queued at that moment never runs.

Logs (``tools.http_request``) carry the method, host, status, outcome, attempts, duration (the read
included) and ``offloaded`` (whether the read left the loop), never the path, query, headers or bodies. A
read that ran out of time is the one line with ``outcome="timeout"`` and ``offloaded=true``.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

import httpx
import orjson
from pydantic import JsonValue

from va_common.json import dumps
from va_common.logging import get_logger
from va_common.net import HostResolver, UnsafeDestinationError, system_resolver
from va_common.safe_http import safe_client
from va_common.settings import WebhookSafetySettings
from va_common.time import Clock
from va_common.waits import within
from va_engine.ports import HttpCall, HttpOutcome, HttpOutcomeKind
from va_tools.jsonpath import extract_all, inline_read
from va_tools.offload import off_loop

__all__ = [
    "BACKOFF_S",
    "CONNECT_TIMEOUT_S",
    "MAX_RESPONSE_BYTES",
    "USER_AGENT",
    "HttpExecutor",
    "Signer",
    "open_http_executor",
    "tools_client",
]

MAX_RESPONSE_BYTES: Final = 1_048_576
CONNECT_TIMEOUT_S: Final = 2.0
BACKOFF_S: Final = 0.25
USER_AGENT: Final = "VoiceAgents-Tools/1.0"
DEFAULT_TIMEOUT_S: Final = 10.0
_SERVER_ERROR: Final = 500
_SUCCESS: Final = range(200, 300)

type Signer = Callable[[bytes], Mapping[str, str]]
"""Signature headers over the exact request body bytes (``X-VA-Signature``, specs/19 §8)."""

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class _Attempt:
    kind: HttpOutcomeKind
    status: int | None = None
    raw: bytes = b""
    truncated: bool = False

    @property
    def retryable(self) -> bool:
        if self.kind in {"timeout", "connection"}:
            return True
        return self.status is not None and self.status >= _SERVER_ERROR


def _preflight(url: str, *, allow_http: bool) -> HttpOutcomeKind | None:
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError:
        return "invalid_url"
    if not parts.scheme or not host:
        return "invalid_url"
    schemes = {"https", "http"} if allow_http else {"https"}
    return None if parts.scheme.lower() in schemes else "blocked"


def _host_of(url: str) -> str | None:
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _parsed(raw: bytes) -> JsonValue:
    try:
        value: JsonValue = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return raw.decode("utf-8", errors="replace")
    return value


def _read(raw: bytes, extract: tuple[tuple[str, str], ...]) -> tuple[JsonValue, Mapping[str, JsonValue]]:
    """The parsed body of ``raw`` and what ``extract`` selects in it (JSON bodies only). Data in, data
    out: it runs on the loop or on the reader thread."""
    body = _parsed(raw)
    json_body = body if not isinstance(body, str) else None
    return body, (extract_all(json_body, extract) if json_body is not None else {})


async def _read_capped(response: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        room = max_bytes - size
        if len(chunk) > room:
            chunks.append(chunk[:room])
            return b"".join(chunks), True
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks), False


class HttpExecutor:
    """Sends rendered requests over ``client`` (see the module docstring for the rules)."""

    def __init__(  # noqa: PLR0913 - one keyword per executor knob keeps call sites explicit
        self,
        client: httpx.AsyncClient,
        *,
        clock: Clock,
        allow_http: bool,
        call_id: str | None = None,
        signer: Signer | None = None,
        max_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self._client = client
        self._clock = clock
        self._allow_http = allow_http
        self._call_id = call_id
        self._signer = signer
        self._max_bytes = max_bytes

    async def execute(self, call: HttpCall) -> HttpOutcome:
        """Send ``call`` (with retries) and describe what happened."""
        started = self._clock.monotonic()
        url = call.url.strip()
        refused = _preflight(url, allow_http=self._allow_http)
        if refused is not None:
            return await self._finish(call, url, _Attempt(refused), attempts=0, started=started)
        content = None if call.body is None or call.method == "GET" else dumps(call.body)
        headers = self._headers(call, content)
        timeout = httpx.Timeout(
            call.timeout_ms / 1000, connect=min(CONNECT_TIMEOUT_S, call.timeout_ms / 1000)
        )
        attempts = 0
        while True:
            attempts += 1
            attempt = await self._attempt(call, url, headers, content, timeout)
            if not attempt.retryable or attempts > call.retries:
                return await self._finish(call, url, attempt, attempts=attempts, started=started)
            await self._clock.sleep(BACKOFF_S * attempts)

    def _headers(self, call: HttpCall, content: bytes | None) -> httpx.Headers:
        headers = httpx.Headers(dict(call.headers))
        headers["User-Agent"] = USER_AGENT
        if self._call_id is not None:
            headers["X-VA-Call-Id"] = self._call_id
        if content is not None:
            headers["Content-Type"] = "application/json"
        if self._signer is not None:
            headers.update(self._signer(content or b""))
        return headers

    async def _attempt(
        self,
        call: HttpCall,
        url: str,
        headers: httpx.Headers,
        content: bytes | None,
        limits: httpx.Timeout,
    ) -> _Attempt:
        try:
            async with self._client.stream(
                call.method, url, headers=headers, content=content, timeout=limits
            ) as response:
                raw, truncated = await _read_capped(response, self._max_bytes)
        except UnsafeDestinationError as exc:
            return _Attempt("connection" if exc.reason == "resolution" else "blocked")
        except httpx.TimeoutException:
            return _Attempt("timeout")
        except httpx.InvalidURL:
            return _Attempt("invalid_url")
        except httpx.TransportError:
            return _Attempt("connection")
        status = response.status_code
        kind: HttpOutcomeKind = "ok" if status in _SUCCESS else "http_error"
        return _Attempt(kind, status, raw, truncated)

    async def _finish(
        self, call: HttpCall, url: str, attempt: _Attempt, *, attempts: int, started: float
    ) -> HttpOutcome:
        body: JsonValue = None
        extracted: Mapping[str, JsonValue] = {}
        offloaded = False
        if attempt.status is not None and attempt.raw:
            work = functools.partial(_read, attempt.raw, call.extract)
            offloaded = not inline_read(len(attempt.raw), (path for _, path in call.extract))
            if not offloaded:
                body, extracted = work()
            else:
                reading = asyncio.ensure_future(off_loop(work))
                if await within(reading, self._clock, call.timeout_ms / 1000):
                    body, extracted = reading.result()
                else:
                    attempt = _Attempt("timeout")
        if call.extract and not extracted:
            extracted = dict.fromkeys((name for name, _ in call.extract), None)
        duration_ms = max(round((self._clock.monotonic() - started) * 1000), 0)
        _log.info(
            "tools.http_request",
            method=call.method,
            host=_host_of(url),
            status=attempt.status,
            outcome=attempt.kind,
            attempts=attempts,
            duration_ms=duration_ms,
            truncated=attempt.truncated,
            offloaded=offloaded,
        )
        return HttpOutcome(
            kind=attempt.kind,
            status=attempt.status,
            body=body,
            truncated=attempt.truncated,
            extracted=extracted,
            attempts=attempts,
            duration_ms=duration_ms,
        )


def tools_client(
    settings: WebhookSafetySettings, *, resolver: HostResolver = system_resolver
) -> httpx.AsyncClient:
    """The guarded client of :class:`HttpExecutor` instances (connect-time SSRF vetting per
    ``WEBHOOK_ALLOW_PRIVATE``); a process may share one between many executors and must close it."""
    return safe_client(
        settings,
        timeout=httpx.Timeout(DEFAULT_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        allow_private=settings.webhook_allow_private,
        resolver=resolver,
    )


@asynccontextmanager
async def open_http_executor(  # noqa: PLR0913 - one keyword per executor knob keeps call sites explicit
    settings: WebhookSafetySettings,
    *,
    clock: Clock,
    allow_http: bool,
    resolver: HostResolver = system_resolver,
    call_id: str | None = None,
    signer: Signer | None = None,
) -> AsyncIterator[HttpExecutor]:
    """An executor over a guarded :func:`tools_client` (closed on exit)."""
    client = tools_client(settings, resolver=resolver)
    try:
        yield HttpExecutor(client, clock=clock, allow_http=allow_http, call_id=call_id, signer=signer)
    finally:
        await client.aclose()
