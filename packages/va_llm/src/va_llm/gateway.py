"""The LLM gateway: non-streaming completions per call type (specs/10-llm-gateway.md §1, §2.1, §2.2,
§11; Decision 2026-11-01 P03-T13a rules 5-11).

``complete`` resolves the org's provider, sends the request to the primary target ``(provider, model)``
under the call type's policy (deadline on the injected clock, retries on retryable errors, ``Retry-After``
≤ 1 s honoured), skips targets whose breaker is open, and for dialogue/analysis falls back once to the
platform fallback model. When the org's own provider cannot be loaded, dialogue/analysis go straight to that
fallback and the other call types raise ``LlmUnavailableError("provider_unavailable")`` without a request.
Every attempt is counted in ``va_llm_requests_total``; the whole call is one ``llm.<call_type>`` span and one
``va_llm_request_duration_ms`` observation; every completion is metered.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx
import openai
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, TracerProvider

from va_common.logging import get_logger
from va_common.time import Clock
from va_llm.breaker import BreakerBoard, CircuitBreaker
from va_llm.clients import CALL_ID_HEADER, LlmClients, sdk_timeout
from va_llm.deadline import DeadlineExceeded, within
from va_llm.dialogue import DialogueDeps, DialogueRequest, DialogueStream
from va_llm.errors import (
    LlmConnectionError,
    LlmError,
    LlmHttpError,
    LlmProtocolError,
    LlmRateLimitedError,
    LlmTimeoutError,
    LlmUnavailableError,
    OrgProviderError,
    UnavailableReason,
    map_openai_error,
)
from va_llm.metering import Meter, UsageRecorder, estimate_usage
from va_llm.metrics import LlmMetrics
from va_llm.models import ChatMessage, Completion, ToolChoice, ToolDef
from va_llm.policy import POLICIES, CallPolicy, CallType
from va_llm.pricing import CostTable
from va_llm.providers import ProviderRegistry
from va_llm.targets import RETRYABLE_STATUSES, Target, platform_fallback, record_failure, sdk_backstop
from va_llm.wire import ChatRequest, ParsedCompletion, parse_completion

__all__ = ["RETRYABLE_STATUSES", "LlmGateway"]

_MAX_RETRY_AFTER_S: Final = 1.0

_log = get_logger(__name__)


@dataclass(slots=True)
class _Run:
    call_type: CallType
    org_id: uuid.UUID
    call_id: uuid.UUID | None
    request: ChatRequest
    target: Target
    attempts: int = 0
    last: LlmError | None = None
    status: int | None = None
    fell_back: bool = False


class _Budget:
    """The deadline of the attempts on one target: a total budget, or ``timeout_s`` per attempt."""

    def __init__(self, clock: Clock, policy: CallPolicy) -> None:
        self._clock = clock
        self._policy = policy
        self._deadline = clock.monotonic() + policy.timeout_s

    def limit(self) -> float:
        if self._policy.per_attempt:
            return self._policy.timeout_s
        return self._deadline - self._clock.monotonic()

    def allows(self, wait_s: float) -> bool:
        return self._policy.per_attempt or self._clock.monotonic() + wait_s < self._deadline


def _rate_limited_wait(retry_after_s: float | None, delay: float) -> float | None:
    """A ``429`` is retried after its ``Retry-After`` (absent = 0) only when that is at most 1 s."""
    after_s = retry_after_s or 0.0
    return max(delay, after_s) if after_s <= _MAX_RETRY_AFTER_S else None


def _error_wait(error: LlmError, policy: CallPolicy, delay: float) -> float | None:
    match error:
        case LlmTimeoutError():
            return delay if policy.per_attempt else None
        case LlmRateLimitedError(retry_after_s=after):
            return _rate_limited_wait(after, delay)
        case LlmHttpError(status=status):
            return delay if status in RETRYABLE_STATUSES else None
        case LlmConnectionError():
            return delay
        case _:
            return None


def _retry_wait(error: LlmError, policy: CallPolicy, delay: float | None, budget: _Budget) -> float | None:
    """Seconds to wait before retrying after ``error`` (``None``: do not retry)."""
    wait = None if delay is None else _error_wait(error, policy, delay)
    return wait if wait is not None and budget.allows(wait) else None


def _annotate(span: Span, run: _Run | None) -> None:
    if run is None:  # the org's provider could not be loaded: nothing was attempted
        return
    span.set_attribute("llm.provider", run.target.provider.id)
    span.set_attribute("llm.model", run.target.model)
    span.set_attribute("llm.attempts", run.attempts)
    span.set_attribute("llm.fallback", run.fell_back)
    if run.status is not None:
        span.set_attribute("http.status_code", run.status)


class LlmGateway:
    """OpenAI-compatible completions with the specs/10 §2.1 policies; owns nothing it must close (the caller
    owns ``http``)."""

    def __init__(  # noqa: PLR0913 - collaborators are injected (clock, metrics, recorder, costs, tracing)
        self,
        registry: ProviderRegistry,
        http: httpx.AsyncClient,
        *,
        clock: Clock,
        metrics: LlmMetrics,
        recorder: UsageRecorder | None = None,
        costs: CostTable | None = None,
        tracer_provider: TracerProvider | None = None,
    ) -> None:
        self.registry = registry
        self.clients = LlmClients(http)
        self._clock = clock
        self._metrics = metrics
        self._meter_usage = Meter(metrics, costs if costs is not None else CostTable(), recorder)
        self._breakers = BreakerBoard(clock, metrics.breaker_state)
        self._tracer = trace.get_tracer("va_llm", tracer_provider=tracer_provider)
        self._dialogue = DialogueDeps(
            self.registry, self.clients, clock, metrics, self._breakers, self._meter_usage, self._tracer
        )

    def breaker(self, provider_id: str, model: str) -> CircuitBreaker:
        """The breaker of ``(provider_id, model)``."""
        return self._breakers.get(provider_id, model)

    async def prewarm(self) -> bool:
        """Open a connection to the platform provider (media-worker startup)."""
        return await self.clients.prewarm(self.registry.platform)

    async def stream_dialogue(self, request: DialogueRequest) -> DialogueStream:
        """Start one streaming dialogue turn (specs/10 §2); returns before the first token."""
        return DialogueStream(self._dialogue, request)

    async def complete(  # noqa: PLR0913 - mirrors the chat-completions parameters
        self,
        call_type: CallType,
        *,
        org_id: uuid.UUID,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolDef] = (),
        tool_choice: ToolChoice | None = None,
        temperature: float = 0.0,
        max_tokens: int = 512,
        model: str | None = None,
        call_id: uuid.UUID | None = None,
        response_format: Mapping[str, Any] | None = None,
    ) -> Completion:
        """One completion under ``call_type``'s policy; :class:`LlmUnavailableError` when it is exhausted."""
        policy = POLICIES[call_type]
        request = ChatRequest(
            tuple(messages), tuple(tools), tool_choice, temperature, max_tokens, response_format
        )
        started = self._clock.monotonic()
        run: _Run | None = None
        with self._tracer.start_as_current_span(
            f"llm.{call_type}",
            kind=SpanKind.CLIENT,
            attributes={"llm.call_type": call_type},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                run = await self._open(
                    policy, call_type=call_type, org_id=org_id, call_id=call_id, request=request, model=model
                )
                completion = await self._run(run, policy)
            except LlmError as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                if run is None:
                    span.set_attribute("llm.provider", exc.provider_id)
                raise
            finally:
                self._metrics.duration(call_type, (self._clock.monotonic() - started) * 1000.0)
                _annotate(span, run)
            span.set_attribute("llm.tokens_in", completion.usage.prompt_tokens)
            span.set_attribute("llm.tokens_out", completion.usage.completion_tokens)
        await self._meter(run, completion)
        return completion

    async def _open(  # noqa: PLR0913 - the parts of one call
        self,
        policy: CallPolicy,
        *,
        call_type: CallType,
        org_id: uuid.UUID,
        call_id: uuid.UUID | None,
        request: ChatRequest,
        model: str | None,
    ) -> _Run:
        """The run of one call on its primary target; when the org's provider cannot be loaded, a run already
        on the platform fallback (dialogue/analysis) or ``LlmUnavailableError("provider_unavailable")``."""
        try:
            provider = await self.registry.resolve(org_id)
        except OrgProviderError as exc:
            _log.warning(
                "llm.org_provider_unavailable", org_id=str(org_id), call_type=call_type, error=exc.cause
            )
            if not policy.falls_back:
                raise LlmUnavailableError(
                    "provider_unavailable", provider_id=exc.provider_id, model=model or "", last=None
                ) from exc
            return _Run(call_type, org_id, call_id, request, platform_fallback(self.registry), fell_back=True)
        return _Run(
            call_type, org_id, call_id, request, Target(provider, model or provider.models[policy.role])
        )

    def _fallback(self, policy: CallPolicy, primary: Target) -> Target | None:
        if not policy.falls_back:
            return None
        target = platform_fallback(self.registry)
        return None if target.same_as(primary) else target

    async def _run(self, run: _Run, policy: CallPolicy) -> Completion:
        primary = run.target
        if run.fell_back:  # the org's provider could not be loaded: one attempt on the platform fallback
            return await self._on_target(run, policy, primary, iter(()))
        try:
            return await self._on_target(run, policy, primary, iter(policy.retry_delays_s))
        except LlmUnavailableError:
            fallback = self._fallback(policy, primary)
            if fallback is None:
                raise
        run.fell_back = True
        return await self._on_target(run, policy, fallback, iter(()))

    async def _on_target(
        self, run: _Run, policy: CallPolicy, target: Target, delays: Iterator[float]
    ) -> Completion:
        run.target = target
        breaker = self._breakers.get(target.provider.id, target.model)
        budget = _Budget(self._clock, policy)
        while True:
            if not breaker.allow():
                self._metrics.attempt(run.call_type, target.model, "breaker_open")
                raise self._unavailable("breaker_open", run)
            try:
                parsed = await self._attempt(run, breaker, budget.limit())
            except LlmError as error:
                wait = _retry_wait(error, policy, next(delays, None), budget)
                if wait is None:
                    raise self._unavailable("exhausted", run) from error
                await self._clock.sleep(wait)
            else:
                usage = parsed.usage or estimate_usage(
                    run.request.messages, parsed.content, parsed.tool_calls
                )
                return Completion(
                    content=parsed.content,
                    tool_calls=parsed.tool_calls,
                    finish_reason=parsed.finish_reason,
                    usage=usage,
                    provider_id=target.provider.id,
                    model=target.model,
                    attempts=run.attempts,
                    fell_back=run.fell_back,
                )

    def _unavailable(self, reason: UnavailableReason, run: _Run) -> LlmUnavailableError:
        return LlmUnavailableError(
            reason, provider_id=run.target.provider.id, model=run.target.model, last=run.last
        )

    async def _attempt(self, run: _Run, breaker: CircuitBreaker, limit_s: float) -> ParsedCompletion:
        target = run.target
        where = {"provider_id": target.provider.id, "model": target.model}
        run.attempts += 1
        try:
            status, body = await within(self._clock, limit_s, self._send(run, limit_s))
        except DeadlineExceeded:
            error: LlmError = LlmTimeoutError(
                f"{target.provider.id} {target.model}: no answer within {limit_s:g} s", **where
            )
        except openai.OpenAIError as exc:
            error = map_openai_error(exc, now=self._clock.now(), **where)
        except asyncio.CancelledError:
            breaker.abandon()
            raise
        else:
            run.status = status
            breaker.record(success=True)
            try:
                parsed = parse_completion(body, **where)
            except LlmProtocolError as exc:
                self._failed(run, exc)
                raise
            self._metrics.attempt(run.call_type, target.model, "ok")
            return parsed
        breaker.record(success=not error.breaker_failure)
        self._failed(run, error)
        raise error

    async def _send(self, run: _Run, limit_s: float) -> tuple[int, bytes]:
        provider = run.target.provider
        headers = {CALL_ID_HEADER: str(run.call_id)} if run.call_id is not None else None
        # The body is built from our own typed models; the SDK's TypedDict overloads cannot see through it.
        create: Any = self.clients.client(provider).chat.completions.with_raw_response.create
        async with self.clients.limit(provider):
            raw = await create(
                model=run.target.model,
                extra_headers=headers,
                timeout=sdk_timeout(sdk_backstop(limit_s)),
                **run.request.body(),
            )
        return int(raw.status_code), bytes(raw.content)

    def _failed(self, run: _Run, error: LlmError) -> None:
        run.last = error
        if isinstance(error, LlmHttpError):
            run.status = error.status
        record_failure(self._metrics, run.call_type, run.target, error)

    async def _meter(self, run: _Run, completion: Completion) -> None:
        await self._meter_usage.record(
            org_id=run.org_id,
            call_id=run.call_id,
            call_type=run.call_type,
            provider_id=completion.provider_id,
            model=completion.model,
            usage=completion.usage,
        )
