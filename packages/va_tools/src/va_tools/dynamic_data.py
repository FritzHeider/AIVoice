"""Dynamic data: HTTP fetches whose extracted values become call variables (specs/13 §7).

Rules: specs/13 §7 Decision P08-T03a. :meth:`DynamicDataFetcher.fetch` runs one
:class:`~va_domain.calls.tools.DynamicDataSpec` and never raises:

1. The request is the spec rendered like a tool request (:func:`va_tools.render.render_request`: URL
   expressions percent-encoded, ``query`` merged into the URL, headers as text, a lone body expression keeps
   its value's type). A template that does not parse is the outcome ``render_error``; nothing is sent.
2. With ``cache: true``, a ``cache_ttl_seconds`` above zero and a Redis client, the answer is looked up
   under :func:`cache_key` first. A stored ``{"status": int, "body": JSON}`` whose body is a JSON document
   (rule 4) is a hit: no request, outcome ``ok``, ``cached``. Any other stored value (not that shape, or
   a body that is text, a bare string, nothing or ``null``, which an earlier build could store) and a Redis
   failure are misses. So is a stored value longer than the fetcher's ``max_cached_bytes`` plus the wrapper
   (checked before it is parsed): a fetcher hits exactly what it would store, whoever stored the value.
3. On a miss the request goes through the :class:`~va_engine.ports.HttpPort` (SSRF guard, size cap; no
   retries). Only a ``readable`` 2xx answer (rule 4) whose compact JSON is at most the fetcher's
   ``max_cached_bytes`` (:data:`MAX_CACHED_BYTES` unless the fetcher was built with a smaller limit)
   is stored (``SET … EX cache_ttl_seconds``); a failed write is ignored. Failures are never cached, and
   neither is a 2xx answer that is not readable: it says nothing about the data, so the endpoint is asked
   again by the next fetch, as after a failure. The key covers the org, the method, the rendered URL and
   the body, not the headers.
4. Variables (outcome ``ok`` only): each ``response_data`` item sets ``name`` to the JSONPath value of
   ``data``. The JSONPaths are read only from a ``readable`` answer: a whole JSON document, so not an empty
   body, not a body that is no JSON (a bare JSON string or ``null`` included) and not one cut at the size
   cap; any other 2xx answer is ``ok`` and sets nothing. A null (or missing) value sets nothing, and
   neither does a value the stores behind a call
   cannot hold: containers nested more than :data:`MAX_VARIABLE_DEPTH` deep (the context JSON, the engine
   state and the event dumps stop at about 254 levels and wrap a variable in levels of their own), U+0000
   in any string or object key (Postgres ``jsonb`` refuses it), or compact JSON above
   :data:`MAX_VARIABLE_BYTES`. Such a value is logged by name and skipped; the fetch stays ``ok`` with its
   other variables. Of two items with one name the later one counts.
5. Context: for each variable that was set and whose item has a non-blank ``context``, the template is
   rendered with this fetch's variables in front of the scope's, stripped and cut to :data:`CONTEXT_LIMIT`
   characters. A blank result or a template that does not parse gives no line.
6. Any other exception (a port or a Redis client that raises) is the outcome ``error``.

Every outcome but ``ok`` sets nothing and is a ``warning``. Logs (``tools.dynamic_data``) carry the index,
timing, outcome, status, the cache flag, the duration and the number of variables; never the URL, headers,
bodies or values. :meth:`DynamicDataFetcher.fetch_all` runs several specs concurrently in ONE scope (no fetch
sees another's variables) within a budget on the injected clock; what is not finished by then is cancelled
and reported as ``timeout``.

:meth:`DynamicDataFetcher.unfetched` is the result of a fetch that produced no answer of its own: one that
missed the budget (``timeout``) or one the caller did not start (``paused``, which only the per-turn fetches
of :mod:`va_tools.turn_data` report, specs/13 §7 Decision P08-FIX-23). It sends nothing and reads no cache.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, Literal

from pydantic import JsonValue
from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common.logging import get_logger
from va_common.time import Clock
from va_common.waits import within
from va_domain.calls.tools import DynamicDataSpec
from va_engine.handlers.reports import redacted_url
from va_engine.ports import HttpOutcomeKind, HttpPort
from va_engine.templating import TemplateScope, TemplateSyntaxError, render
from va_tools.jsonpath import extract_all
from va_tools.models import ToolSpec
from va_tools.render import ToolRenderError, excerpt, render_request

__all__ = [
    "CONTEXT_LIMIT",
    "MAX_CACHED_BYTES",
    "MAX_VARIABLE_BYTES",
    "MAX_VARIABLE_DEPTH",
    "PRE_CALL_BUDGET_S",
    "PRE_CALL_TIMEOUT_MS",
    "DynamicDataFetcher",
    "DynamicDataResult",
    "DynamicOutcome",
    "DynamicTiming",
    "cache_key",
    "merge_results",
]

PRE_CALL_TIMEOUT_MS: Final = 5_000
"""Each pre-call request (connect at most 2 s); pre-call requests are not retried."""
PRE_CALL_BUDGET_S: Final = 6.0
"""The whole pre-call phase of a call, on the injected clock."""
CONTEXT_LIMIT: Final = 2_000
"""Characters of one rendered context line."""
MAX_VARIABLE_BYTES: Final = 16_384
"""Compact JSON of one extracted value; a larger one is not set."""
MAX_VARIABLE_DEPTH: Final = 32
"""Arrays and objects nested inside one another in one extracted value (a scalar is 0, ``[1]`` is 1); a
deeper value is not set."""
MAX_CACHED_BYTES: Final = 262_144
"""Compact JSON of a response body; a larger one is not cached (a fetcher may have a smaller limit)."""
_ENVELOPE_BYTES: Final = 32
"""Room for the ``{"status":NNN,"body":}`` wrapper (22 bytes) around a stored body."""

type DynamicOutcome = HttpOutcomeKind | Literal["render_error", "error", "paused"]
type DynamicTiming = Literal["pre_call", "every_turn"]

_log = get_logger(__name__)


def _compact(value: JsonValue) -> str:
    """Compact JSON through the standard library (no 64-bit integer limit)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _unstorable(value: JsonValue) -> Literal["too_deep", "nul_character"] | None:
    """Why the stores behind a call cannot hold ``value`` (``None`` when they can).

    Walks the value without recursion: an answer may nest as deep as its parser allows.
    """
    pending: list[tuple[JsonValue, int]] = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, str):
            if "\x00" in item:
                return "nul_character"
            continue
        if not isinstance(item, dict | list):
            continue
        if depth >= MAX_VARIABLE_DEPTH:
            return "too_deep"
        if isinstance(item, dict):
            if any("\x00" in name for name in item):
                return "nul_character"
            pending.extend((member, depth + 1) for member in item.values())
        else:
            pending.extend((member, depth + 1) for member in item)
    return None


def cache_key(org_id: uuid.UUID, method: str, url: str, body: JsonValue) -> str:
    """``dyn:{org_id}:{sha256}`` over the method, the rendered URL (query merged) and the rendered body
    (compact JSON with sorted keys; nothing for no body)."""
    text = "" if body is None else json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(f"{method.upper()}\n{url}\n{text}".encode()).hexdigest()
    return f"dyn:{org_id}:{digest}"


@dataclass(frozen=True, slots=True, kw_only=True)
class DynamicDataResult:
    """What one dynamic data fetch produced.

    ``index`` is the spec's position in the call's ``dynamic_data``; ``url`` has no user info, query or
    fragment (empty when the URL is unknown or unusable). ``readable`` tells whether the answer was a whole
    JSON document the JSONPaths could be read from (never for a failure, an empty body, a body that is not
    JSON, a bare JSON string or ``null``, or a body cut at the size cap). ``variables`` are the variables
    set, ``sections`` the context line of each variable that has one; both are empty unless the answer was
    ``readable``.
    """

    index: int
    timing: DynamicTiming
    outcome: DynamicOutcome
    url: str
    method: str
    status_code: int | None = None
    duration_ms: int = 0
    cached: bool = False
    readable: bool = False
    variables: dict[str, JsonValue] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        """Whether no usable answer arrived."""
        return self.outcome != "ok"

    @property
    def level(self) -> Literal["info", "warning"]:
        """The ``call.dynamic_data`` event level."""
        return "warning" if self.failed else "info"

    def event_data(self) -> dict[str, JsonValue]:
        """The ``call.dynamic_data`` event data (specs/19 §2): no query string, no header, no value."""
        return {
            "url": self.url,
            "method": self.method,
            "status_code": self.status_code,
            "duration_ms": self.duration_ms,
            "variables_set": list(self.variables),
            "cached": self.cached,
            "outcome": self.outcome,
            "timing": self.timing,
            "index": self.index,
        }

    def message(self) -> str:
        """The event message."""
        if self.outcome == "paused":
            return "Dynamic data paused: endpoint not answering"
        if self.failed:
            status = f" ({self.status_code})" if self.status_code is not None else ""
            return f"Dynamic data failed: {self.outcome}{status}"
        count = len(self.variables)
        text = f"Dynamic data: {count} variable{'' if count == 1 else 's'} set"
        return f"{text} (cached)" if self.cached else text


def merge_results(results: Sequence[DynamicDataResult]) -> tuple[dict[str, JsonValue], list[str]]:
    """The variables and context lines of ``results`` taken in index order.

    A later result wins a variable name, and its line (or its having none) replaces the earlier line of
    that name.
    """
    variables: dict[str, JsonValue] = {}
    lines: dict[str, str] = {}
    for result in sorted(results, key=lambda item: item.index):
        for name, value in result.variables.items():
            variables[name] = value
            lines.pop(name, None)
            line = result.sections.get(name)
            if line is not None:
                lines[name] = line
    return variables, list(lines.values())


def _request_spec(spec: DynamicDataSpec, index: int, timeout_ms: int) -> ToolSpec:
    """``spec`` as the tool request the renderer understands (the names map to their JSONPaths)."""
    return ToolSpec(
        tool_id=None,
        name=f"dynamic_data_{index}",
        description="",
        speech=None,
        url=spec.url,
        method=spec.method,
        headers=dict(spec.headers),
        query=dict(spec.query),
        body=spec.body,
        response_map={item.name: item.data for item in spec.response_data},
        timeout_ms=timeout_ms,
        retries=0,
    )


def _json_document(body: JsonValue, *, truncated: bool) -> bool:
    """Whether a response body is a whole parsed JSON document the JSONPaths can be read from: not one cut
    at the size cap, not text (which a bare JSON string also reads as), not nothing or ``null``."""
    return not truncated and body is not None and not isinstance(body, str)


def _stored_bytes(raw: object) -> int:
    """The size of a stored cache value in bytes (0 for a value that is no text)."""
    if isinstance(raw, str):
        return len(raw.encode())
    return len(raw) if isinstance(raw, bytes | bytearray) else 0


def _cached_answer(raw: object) -> tuple[int, JsonValue] | None:
    """The ``(status, body)`` of a stored answer; ``None`` when the value is not one or its body is not a
    JSON document (only readable answers are stored, so such a value says nothing about the data)."""
    if not isinstance(raw, str | bytes | bytearray):
        return None
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict) or "body" not in value:
        return None
    status = value.get("status")
    if isinstance(status, bool) or not isinstance(status, int):
        return None
    body: JsonValue = value["body"]
    if not _json_document(body, truncated=False):
        return None
    return status, body


class DynamicDataFetcher:
    """Runs the dynamic data specs of one org's conversation over ``http`` (see the module docstring).

    ``redis`` ``None`` disables the cache. ``max_cached_bytes`` bounds the compact JSON of a body this
    fetcher stores and, plus the wrapper, the stored value it reads (a longer one is a miss).
    """

    def __init__(
        self,
        http: HttpPort,
        *,
        redis: Redis | None,
        org_id: uuid.UUID,
        clock: Clock,
        max_cached_bytes: int = MAX_CACHED_BYTES,
    ) -> None:
        self._http = http
        self._redis = redis
        self._org_id = org_id
        self._clock = clock
        self._max_cached_bytes = max_cached_bytes

    @property
    def clock(self) -> Clock:
        """The clock the fetches are timed and bounded on."""
        return self._clock

    async def fetch(
        self, spec: DynamicDataSpec, index: int, scope: TemplateScope, *, timeout_ms: int
    ) -> DynamicDataResult:
        """Fetch ``spec`` (position ``index``) rendered in ``scope``; never raises."""
        started = self._clock.monotonic()
        try:
            result = await self._fetch(spec, index, scope, timeout_ms)
        except Exception as exc:  # rule 6: a port or a Redis client that raises never fails the conversation
            _log.warning("tools.dynamic_data_failed", index=index, error_type=type(exc).__name__)
            result = DynamicDataResult(
                index=index, timing=spec.timing, outcome="error", url="", method=spec.method
            )
        duration_ms = max(round((self._clock.monotonic() - started) * 1000), 0)
        return self._logged(dataclasses.replace(result, duration_ms=duration_ms))

    async def fetch_all(
        self,
        specs: Sequence[tuple[int, DynamicDataSpec]],
        scope: TemplateScope,
        *,
        timeout_ms: int,
        budget_s: float,
    ) -> list[DynamicDataResult]:
        """Fetch every ``(index, spec)`` concurrently in ``scope``; results in the order of ``specs``.

        A fetch not finished after ``budget_s`` seconds of the clock is cancelled and reported as ``timeout``.
        """
        if not specs:
            return []
        tasks = [
            asyncio.ensure_future(self.fetch(spec, index, scope, timeout_ms=timeout_ms))
            for index, spec in specs
        ]
        await within(asyncio.gather(*tasks), self._clock, budget_s)
        results: list[DynamicDataResult] = []
        for (index, spec), task in zip(specs, tasks, strict=True):
            if task.cancelled():
                duration_ms = round(budget_s * 1000)
                results.append(self.unfetched(spec, index, scope, "timeout", duration_ms=duration_ms))
            else:
                results.append(task.result())
        return results

    def unfetched(
        self,
        spec: DynamicDataSpec,
        index: int,
        scope: TemplateScope,
        outcome: Literal["timeout", "paused"],
        *,
        duration_ms: int = 0,
    ) -> DynamicDataResult:
        """The result of a fetch of ``spec`` that produced no answer of its own: ``timeout`` for one that
        missed its budget, ``paused`` for one the caller did not start. Nothing is sent and no cache is
        read; the URL is the rendered one without user info, query or fragment (empty when it does not
        render)."""
        try:
            url = redacted_url(render_request(_request_spec(spec, index, 0), scope).call.url)
        except ToolRenderError:
            url = ""
        return self._logged(
            DynamicDataResult(
                index=index,
                timing=spec.timing,
                outcome=outcome,
                url=url,
                method=spec.method,
                duration_ms=duration_ms,
            )
        )

    # One fetch.

    async def _fetch(
        self, spec: DynamicDataSpec, index: int, scope: TemplateScope, timeout_ms: int
    ) -> DynamicDataResult:
        try:
            call = render_request(_request_spec(spec, index, timeout_ms), scope).call
        except ToolRenderError as exc:
            _log.warning("tools.dynamic_data_render_failed", index=index, field=exc.field)
            return DynamicDataResult(
                index=index, timing=spec.timing, outcome="render_error", url="", method=spec.method
            )
        url = redacted_url(call.url)
        redis = self._redis if spec.cache and spec.cache_ttl_seconds > 0 else None
        key = cache_key(self._org_id, call.method, call.url, call.body)
        hit = None if redis is None else await self._lookup(redis, key, index)
        if hit is not None:
            status, body = hit
            readable = True
            extracted = extract_all(body, call.extract)
        else:
            outcome = await self._http.execute(call)
            if not outcome.succeeded:
                return DynamicDataResult(
                    index=index,
                    timing=spec.timing,
                    outcome=outcome.kind,
                    url=url,
                    method=call.method,
                    status_code=outcome.status,
                )
            status = outcome.status or 0
            readable = _json_document(outcome.body, truncated=outcome.truncated)
            extracted = outcome.extracted if readable else {}
            if redis is not None and readable:
                await self._store(redis, key, spec.cache_ttl_seconds, (status, outcome.body), index)
        variables = self._variables(spec, extracted, index)
        return DynamicDataResult(
            index=index,
            timing=spec.timing,
            outcome="ok",
            url=url,
            method=call.method,
            status_code=status,
            cached=hit is not None,
            readable=readable,
            variables=variables,
            sections=self._sections(spec, variables, scope, index),
        )

    def _variables(
        self, spec: DynamicDataSpec, extracted: Mapping[str, JsonValue], index: int
    ) -> dict[str, JsonValue]:
        variables: dict[str, JsonValue] = {}
        for name in {item.name: item for item in spec.response_data}:
            value = extracted.get(name)
            if value is None:
                continue
            reason = _unstorable(value)
            if reason is not None:
                _log.warning("tools.dynamic_data_value_unstorable", index=index, name=name, reason=reason)
                continue
            if len(_compact(value).encode()) > MAX_VARIABLE_BYTES:
                _log.warning("tools.dynamic_data_value_too_large", index=index, name=name)
                continue
            variables[name] = value
        return variables

    def _sections(
        self, spec: DynamicDataSpec, variables: Mapping[str, JsonValue], scope: TemplateScope, index: int
    ) -> dict[str, str]:
        sections: dict[str, str] = {}
        own = dataclasses.replace(scope, variables={**scope.variables, **variables})
        for name, item in {item.name: item for item in spec.response_data}.items():
            if name not in variables or not (item.context or "").strip():
                continue
            try:
                text = render(item.context or "", own).strip()
            except TemplateSyntaxError:
                _log.warning("tools.dynamic_data_context_invalid", index=index, name=name)
                continue
            line = excerpt(text, CONTEXT_LIMIT)
            if line:
                sections[name] = line
        return sections

    # The cache.

    async def _lookup(self, redis: Redis, key: str, index: int) -> tuple[int, JsonValue] | None:
        try:
            raw = await redis.get(key)
        except (RedisError, OSError) as exc:
            _log.warning("tools.dynamic_data_cache_unavailable", index=index, error_type=type(exc).__name__)
            return None
        if raw is None:
            return None
        if _stored_bytes(raw) > self._max_cached_bytes + _ENVELOPE_BYTES:
            _log.info("tools.dynamic_data_cache_oversized", index=index)
            return None
        answer = _cached_answer(raw)
        if answer is None:
            _log.warning("tools.dynamic_data_cache_invalid", index=index)
        return answer

    async def _store(
        self, redis: Redis, key: str, ttl_s: int, answer: tuple[int, JsonValue], index: int
    ) -> None:
        status, body = answer
        try:
            if len(_compact(body).encode()) > self._max_cached_bytes:
                return
            await redis.set(key, _compact({"status": status, "body": body}), ex=ttl_s)
        except (RedisError, OSError, ValueError, TypeError, RecursionError) as exc:
            _log.warning("tools.dynamic_data_cache_write_failed", index=index, error_type=type(exc).__name__)

    # Reporting.

    @staticmethod
    def _logged(result: DynamicDataResult) -> DynamicDataResult:
        _log.info(
            "tools.dynamic_data",
            index=result.index,
            timing=result.timing,
            outcome=result.outcome,
            status=result.status_code,
            cached=result.cached,
            duration_ms=result.duration_ms,
            variables=len(result.variables),
        )
        return result
