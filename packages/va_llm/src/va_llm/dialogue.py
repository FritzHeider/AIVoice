"""Streaming dialogue (specs/10-llm-gateway.md §2, §2.1 dialogue row, §8, §11; Decision 2026-11-01 P03-T13b).

:meth:`va_llm.LlmGateway.stream_dialogue` returns a :class:`DialogueStream` at once; a producer task sends the
streaming request, reads the SSE lines itself (:mod:`va_llm.stream_wire`), and hands content deltas to
``text()``. Before the first token every failure of the primary target (any HTTP error, connect error,
2.5 s TTFT deadline, malformed chunk, open breaker) gets one attempt on the platform fallback model; after the
first token a failure ends the stream with the partial text kept and ``error`` set. Speculative streams buffer
their deltas until ``commit()``; ``discard()`` and cancelling the consumer close the HTTP stream.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Final, Literal, Self

import httpx
import openai
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, Tracer
from pydantic import BaseModel, ConfigDict, Field

from va_common.logging import get_logger
from va_common.time import Clock
from va_llm.breaker import BreakerBoard, CircuitBreaker
from va_llm.clients import CALL_ID_HEADER, LlmClients, sdk_timeout
from va_llm.deadline import DeadlineExceeded, within
from va_llm.errors import (
    LlmConnectionError,
    LlmError,
    LlmHttpError,
    LlmTimeoutError,
    LlmUnavailableError,
    OrgProviderError,
    UnavailableReason,
    map_openai_error,
)
from va_llm.metering import Meter, estimate_usage
from va_llm.metrics import LlmMetrics
from va_llm.models import ChatMessage, TokenUsage, ToolCall, ToolDef
from va_llm.providers import LlmProvider, ProviderRegistry
from va_llm.stream_wire import StreamAssembler
from va_llm.targets import Target, platform_fallback, record_failure, sdk_backstop
from va_llm.wire import ChatRequest

__all__ = ["TOTAL_S", "TTFT_S", "DialogueDeps", "DialogueRequest", "DialogueStream", "StreamOutcome"]

TTFT_S: Final = 2.5
TOTAL_S: Final = 20.0
_CALL_TYPE: Final = "dialogue"

type StreamOutcome = Literal["completed", "failed", "truncated", "discarded", "cancelled"]

_log = get_logger(__name__)


class DialogueRequest(BaseModel):
    """One dialogue turn; ``provider`` None resolves the org's provider, ``model`` None its dialogue model."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    org_id: uuid.UUID
    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolDef, ...] = ()
    provider: LlmProvider | None = None
    model: str | None = None
    temperature: float = 0.7
    max_tokens: int = Field(default=250, gt=0)
    speculative: bool = False
    call_id: uuid.UUID | None = None
    turn_index: int | None = None


@dataclass(frozen=True, slots=True)
class DialogueDeps:
    """The gateway collaborators a stream uses."""

    registry: ProviderRegistry
    clients: LlmClients
    clock: Clock
    metrics: LlmMetrics
    breakers: BreakerBoard
    meter: Meter
    tracer: Tracer


class _End:
    """The end of ``text()``."""


_END: Final = _End()


class _PreTokenFailure(Exception):  # noqa: N818 - internal signal: the target failed before its first token
    def __init__(self, reason: UnavailableReason) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class _Attempt:
    """One streaming request to ``target``."""

    target: Target
    breaker: CircuitBreaker
    assembler: StreamAssembler
    sent_at: float
    started: bool = False
    verdict: bool = False
    outcome: StreamOutcome = "completed"
    calls: list[ToolCall] = field(default_factory=list)

    def judge(self, *, success: bool) -> None:
        if not self.verdict:
            self.verdict = True
            self.breaker.record(success=success)


def _where(target: Target) -> dict[str, str]:
    return {"provider_id": target.provider.id, "model": target.model}


async def _next_line(lines: AsyncIterator[str]) -> str | None:
    return await anext(lines, None)


class DialogueStream:
    """A streaming dialogue completion; always close it (``async with`` or :meth:`aclose`)."""

    def __init__(self, deps: DialogueDeps, request: DialogueRequest) -> None:
        self._deps = deps
        self._request = request
        self._chat = ChatRequest(
            request.messages,
            request.tools,
            "auto" if request.tools else None,
            request.temperature,
            request.max_tokens,
            None,
        )
        self._queue: asyncio.Queue[str | _End] = asyncio.Queue()
        self._held: list[str | _End] | None = [] if request.speculative else None
        self._decided = False
        self._discarded = False
        self._text_taken = False
        self._producing: _Attempt | None = None
        self._last: LlmError | None = None
        self._status: int | None = None
        self._outcome: StreamOutcome | None = None
        self._started = deps.clock.monotonic()
        self._span: Span = deps.tracer.start_span(
            f"llm.{_CALL_TYPE}",
            kind=SpanKind.CLIENT,
            attributes={
                "llm.call_type": _CALL_TYPE,
                "llm.streaming": True,
                "llm.speculative": request.speculative,
            },
        )
        self.ttft_ms: float | None = None
        self.usage: TokenUsage | None = None
        self.finish_reason: str | None = None
        self.error: LlmError | None = None
        self.provider_id: str | None = None
        self.model: str | None = None
        self.fell_back = False
        self.attempts = 0
        self._calls: list[ToolCall] = []
        self._task = asyncio.create_task(self._produce(), name="llm.dialogue")
        self._task.add_done_callback(self._ended)

    # The consumer side.

    def text(self) -> AsyncIterator[str]:
        """The content deltas (after ``commit()`` when speculative); iterate once. Raises
        :class:`LlmUnavailableError` at the end when no target produced a token."""
        if self._text_taken:
            msg = "DialogueStream.text() may be iterated only once"
            raise RuntimeError(msg)
        self._text_taken = True
        return self._iterate()

    async def tool_calls(self) -> list[ToolCall]:
        """The tool calls once the stream ended (only named calls of a cut-off stream)."""
        await asyncio.wait([self._task])
        self._raise_failure()
        return list(self._calls)

    def commit(self) -> None:
        """Speculative: release the buffered deltas and every later one to ``text()``."""
        if not self._request.speculative or self._decided:
            return
        self._decided = True
        self._deps.metrics.speculated("committed")
        held, self._held = self._held or [], None
        for item in held:
            self._queue.put_nowait(item)

    async def discard(self) -> None:
        """Cancel the request and drop undelivered deltas; ``text()`` then ends."""
        if self._request.speculative and not self._decided:
            self._decided = True
            self._deps.metrics.speculated("discarded")
        self._discarded = True
        held, self._held = self._held, None
        await self._stop()
        if held is not None and any(isinstance(item, _End) for item in held):
            # The producer had already ended into the buffer: ``_ended`` will not run again.
            self._queue.put_nowait(_END)

    async def aclose(self) -> None:
        """Cancel the request (when still running) and wait until the HTTP stream is closed; re-raises an
        unexpected producer crash (a bug), never a request failure."""
        await self._stop()
        self._raise_crash()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.aclose()

    async def _iterate(self) -> AsyncIterator[str]:
        finished = False
        try:
            while True:
                item = await self._queue.get()
                if isinstance(item, _End):
                    finished = True
                    break
                yield item
        finally:
            if not finished:  # the consumer stopped early or was cancelled (barge-in)
                await self._stop()
        self._raise_failure()

    async def _stop(self) -> None:
        if not self._task.done():
            self._task.cancel()
        await asyncio.wait([self._task])

    def _raise_crash(self) -> None:
        if not self._task.cancelled() and (crash := self._task.exception()) is not None:
            raise crash

    def _raise_failure(self) -> None:
        self._raise_crash()
        if isinstance(self.error, LlmUnavailableError):
            raise self.error

    def _deliver(self, item: str | _End) -> None:
        if self._held is not None:
            self._held.append(item)
        else:
            self._queue.put_nowait(item)

    def _ended(self, task: asyncio.Task[None]) -> None:
        self._deliver(_END)
        crash = None if task.cancelled() else task.exception()
        self._finish_span(self._outcome or ("cancelled" if task.cancelled() else "failed"), crash)

    # The producer side.

    async def _produce(self) -> None:
        try:
            with trace.use_span(self._span, record_exception=False, set_status_on_exception=False):
                attempt = await self._run(self._started + TOTAL_S)
            self._outcome = attempt.outcome
        except LlmUnavailableError as exc:
            self.error = exc
            self._outcome = "failed"
        except asyncio.CancelledError:
            self._outcome = "discarded" if self._discarded else "cancelled"
            raise
        finally:
            await self._meter()

    async def _run(self, deadline: float) -> _Attempt:
        primary = await self._primary()
        try:
            return await self._on_target(primary, deadline)
        except _PreTokenFailure as failure:
            fallback = self._fallback(primary)
            if fallback is None:
                raise self._unavailable(failure.reason, primary) from self._last
        self.fell_back = True
        try:
            return await self._on_target(fallback, deadline)
        except _PreTokenFailure as failure:
            raise self._unavailable(failure.reason, fallback) from self._last

    async def _primary(self) -> Target:
        request = self._request
        provider = request.provider
        if provider is None:
            try:
                provider = await self._deps.registry.resolve(request.org_id)
            except OrgProviderError as exc:
                _log.warning(
                    "llm.org_provider_unavailable",
                    org_id=str(request.org_id),
                    call_type=_CALL_TYPE,
                    error=exc.cause,
                )
                self.fell_back = True
                return platform_fallback(self._deps.registry)
        return Target(provider, request.model or provider.models["dialogue"])

    def _fallback(self, primary: Target) -> Target | None:
        if self.fell_back:  # already on the platform fallback (the org's provider could not be loaded)
            return None
        target = platform_fallback(self._deps.registry)
        return None if target.same_as(primary) else target

    def _unavailable(self, reason: UnavailableReason, target: Target) -> LlmUnavailableError:
        return LlmUnavailableError(reason, last=self._last, **_where(target))

    async def _on_target(self, target: Target, deadline: float) -> _Attempt:
        deps = self._deps
        self.provider_id, self.model = target.provider.id, target.model
        breaker = deps.breakers.get(target.provider.id, target.model)
        if not breaker.allow():
            deps.metrics.attempt(_CALL_TYPE, target.model, "breaker_open")
            raise _PreTokenFailure("breaker_open")
        self.attempts += 1
        attempt = _Attempt(target, breaker, StreamAssembler(**_where(target)), sent_at=deps.clock.monotonic())
        try:
            await self._attempt(attempt, deadline)
        except asyncio.CancelledError:
            if not attempt.verdict:
                breaker.abandon()
            raise
        return attempt

    async def _attempt(self, attempt: _Attempt, deadline: float) -> None:
        try:
            await self._read(attempt, deadline)
            calls = attempt.assembler.tool_calls(complete=True)
        except LlmError as error:
            self._failed(attempt, error)
            return
        attempt.judge(success=True)
        attempt.calls = calls
        self._producing = attempt
        self._calls = calls
        self.finish_reason = attempt.assembler.finish_reason
        self._deps.metrics.attempt(_CALL_TYPE, attempt.target.model, "ok")

    def _failed(self, attempt: _Attempt, error: LlmError) -> None:
        self._last = error
        if isinstance(error, LlmHttpError):
            self._status = error.status
        record_failure(self._deps.metrics, _CALL_TYPE, attempt.target, error)
        if not attempt.started:
            attempt.judge(success=not error.breaker_failure)
            raise _PreTokenFailure("exhausted")
        attempt.outcome = "truncated"
        self.error = error
        self._calls = attempt.assembler.tool_calls(complete=False)
        _log.warning(
            "llm.dialogue_truncated",
            provider=attempt.target.provider.id,
            model=attempt.target.model,
            kind=error.error_kind,
        )

    async def _read(self, attempt: _Attempt, deadline: float) -> None:
        """Read the stream of ``attempt`` to ``[DONE]``; every failure is an :class:`LlmError`."""
        target = attempt.target
        try:
            await self._consume(attempt, deadline)
        except DeadlineExceeded:
            phase = "a token" if not attempt.started else "the end of the stream"
            message = f"{target.provider.id} {target.model}: no {phase} in time"
            raise LlmTimeoutError(message, **_where(target)) from None
        except openai.OpenAIError as exc:
            raise map_openai_error(exc, now=self._deps.clock.now(), **_where(target)) from exc
        except httpx.TimeoutException as exc:
            message = f"{target.provider.id} {target.model}: stream timed out"
            raise LlmTimeoutError(message, **_where(target)) from exc
        except httpx.HTTPError as exc:
            message = f"{target.provider.id} {target.model}: stream broke: {type(exc).__name__}"
            raise LlmConnectionError(message, **_where(target)) from exc
        if not attempt.assembler.done:
            message = f"{target.provider.id} {target.model}: stream ended without [DONE]"
            raise LlmConnectionError(message, **_where(target))

    async def _consume(self, attempt: _Attempt, deadline: float) -> None:
        clock = self._deps.clock
        ttft_deadline = min(attempt.sent_at + TTFT_S, deadline)
        async with AsyncExitStack() as stack:
            response = await within(
                clock, ttft_deadline - clock.monotonic(), self._open(stack, attempt.target)
            )
            self._status = int(response.status_code)
            lines: Any = response.iter_lines()
            stack.push_async_callback(lines.aclose)
            while not attempt.assembler.done:
                limit = (deadline if attempt.started else ttft_deadline) - clock.monotonic()
                line = await within(clock, limit, _next_line(lines))
                if line is None:
                    return
                self._feed(attempt, line)

    async def _open(self, stack: AsyncExitStack, target: Target) -> Any:
        """Take the provider's concurrency slot (held for the whole stream) and send the request."""
        request = self._request
        provider = target.provider
        await stack.enter_async_context(self._deps.clients.limit(provider))
        headers = {CALL_ID_HEADER: str(request.call_id)} if request.call_id is not None else None
        # The body is built from our own typed models; the SDK's TypedDict overloads cannot see through it.
        create: Any = self._deps.clients.client(provider).chat.completions.with_streaming_response.create
        return await stack.enter_async_context(
            create(
                model=target.model,
                extra_headers=headers,
                timeout=sdk_timeout(sdk_backstop(TOTAL_S)),
                stream=True,
                stream_options={"include_usage": True},
                **self._chat.body(),
            )
        )

    def _feed(self, attempt: _Attempt, line: str) -> None:
        deltas = attempt.assembler.feed(line)
        if deltas.token and not attempt.started:
            attempt.started = True
            attempt.judge(success=True)
            self._producing = attempt
            self.ttft_ms = (self._deps.clock.monotonic() - attempt.sent_at) * 1000.0
            self._deps.metrics.ttft(attempt.target.model, _CALL_TYPE, self.ttft_ms)
        if deltas.content:
            self._deliver(deltas.content)

    async def _meter(self) -> None:
        """Meter the attempt that produced the result (tokens, or a token-less completion) exactly once."""
        attempt = self._producing
        if attempt is None:
            return
        assembler = attempt.assembler
        usage = assembler.usage or estimate_usage(
            self._request.messages, assembler.text, assembler.tool_calls(complete=False)
        )
        self.usage = usage
        await self._deps.meter.record(
            org_id=self._request.org_id,
            call_id=self._request.call_id,
            call_type=_CALL_TYPE,
            provider_id=attempt.target.provider.id,
            model=attempt.target.model,
            usage=usage,
        )

    def _finish_span(self, outcome: StreamOutcome, crash: BaseException | None) -> None:
        span = self._span
        self._deps.metrics.duration(_CALL_TYPE, (self._deps.clock.monotonic() - self._started) * 1000.0)
        attributes: dict[str, Any] = {
            "llm.outcome": outcome,
            "llm.attempts": self.attempts,
            "llm.fallback": self.fell_back,
            "llm.turn_index": self._request.turn_index,
            "llm.ttft_ms": self.ttft_ms,
            "llm.provider": self.provider_id,
            "llm.model": self.model,
            "http.status_code": self._status,
        }
        if self.usage is not None:
            attributes["llm.tokens_in"] = self.usage.prompt_tokens
            attributes["llm.tokens_out"] = self.usage.completion_tokens
        span.set_attributes({key: value for key, value in attributes.items() if value is not None})
        failure = self.error if crash is None else crash
        if outcome in {"failed", "truncated"} and failure is not None:
            span.record_exception(failure)
            span.set_status(Status(StatusCode.ERROR, type(failure).__name__))
        span.end()
