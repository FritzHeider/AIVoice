"""The embeddings client (specs/10-llm-gateway.md §10, §11; Decision 2026-11-01 P03-T14b).

:class:`Embeddings` embeds texts on the platform provider: batches of 64 inputs, at most ``concurrency`` (4)
batches in flight per instance, ``dimensions`` (1024) requested and validated, every vector L2-normalised.
Each attempt has a 30 s deadline on the injected clock; 429, 5xx, connect errors, and timeouts are retried
after 1 s, 4 s, and 10 s (a ``Retry-After`` longer than the wait is honoured up to 30 s). Any other failure,
or the last retry failing, raises :class:`LlmUnavailableError` (``reason="exhausted"``). Query embeddings are
cached in Redis for 24 h. Every successful batch is metered as an :class:`EmbeddingUsageRecord`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any, Final

import httpx
import openai
from opentelemetry import trace
from opentelemetry.trace import Span, SpanKind, Status, StatusCode, TracerProvider
from redis.asyncio import Redis

from va_common.logging import get_logger
from va_common.time import Clock
from va_llm.clients import CLIENT_TIMEOUT, openai_client, sdk_timeout
from va_llm.deadline import DeadlineExceeded, within
from va_llm.embedding_cache import QueryCache
from va_llm.embedding_wire import ParsedEmbeddings, parse_embeddings, truncate_input
from va_llm.errors import (
    LlmConnectionError,
    LlmError,
    LlmHttpError,
    LlmRateLimitedError,
    LlmTimeoutError,
    LlmUnavailableError,
    map_openai_error,
)
from va_llm.metering import EmbeddingUsageRecord, EmbeddingUsageRecorder, estimate_tokens
from va_llm.metrics import EmbeddingPurpose, LlmMetrics
from va_llm.pricing import CostTable
from va_llm.providers import LlmProvider
from va_llm.targets import RETRYABLE_STATUSES, attempt_outcome, sdk_backstop

__all__ = [
    "ATTEMPT_TIMEOUT_S",
    "DEFAULT_CONCURRENCY",
    "EMBED_BATCH_SIZE",
    "MAX_RETRY_AFTER_S",
    "RETRY_DELAYS_S",
    "Embeddings",
]

EMBED_BATCH_SIZE: Final = 64
DEFAULT_CONCURRENCY: Final = 4
RETRY_DELAYS_S: Final = (1.0, 4.0, 10.0)
ATTEMPT_TIMEOUT_S: Final = 30.0
MAX_RETRY_AFTER_S: Final = 30.0

_log = get_logger(__name__)


@dataclass(slots=True)
class _Batch:
    purpose: EmbeddingPurpose
    org_id: uuid.UUID
    document_id: uuid.UUID | None
    inputs: tuple[str, ...]
    attempts: int = 0
    status: int | None = None


def _retry_wait(error: LlmError, delay: float | None) -> float | None:
    """Seconds to wait before retrying after ``error`` (``None``: do not retry)."""
    if delay is None:
        return None
    match error:
        case LlmRateLimitedError(retry_after_s=after):
            return min(max(delay, after or 0.0), MAX_RETRY_AFTER_S)
        case LlmHttpError(status=status):
            return delay if status in RETRYABLE_STATUSES else None
        case LlmTimeoutError() | LlmConnectionError():
            return delay
        case _:
            return None


def _first_error(group: BaseExceptionGroup[BaseException]) -> BaseException:
    """The first failure of a batch group (the batches it cancelled are not in it)."""
    first = group.exceptions[0]
    return _first_error(first) if isinstance(first, BaseExceptionGroup) else first


class Embeddings:
    """Embeddings on one provider (the platform's); one instance per process (the in-flight limit is per
    instance). The caller owns ``http`` and ``redis``."""

    def __init__(  # noqa: PLR0913 - collaborators are injected (cache, clock, metrics, metering, tracing)
        self,
        provider: LlmProvider,
        http: httpx.AsyncClient,
        *,
        redis: Redis,
        clock: Clock,
        metrics: LlmMetrics,
        model: str,
        dimensions: int = 1024,
        recorder: EmbeddingUsageRecorder | None = None,
        costs: CostTable | None = None,
        tracer_provider: TracerProvider | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self.provider = provider
        self.model = model
        self.dimensions = dimensions
        self._client = openai_client(provider, http, timeout=CLIENT_TIMEOUT)
        self._clock = clock
        self._metrics = metrics
        self._cache = QueryCache(redis, metrics, model=model, dimensions=dimensions)
        self._recorder = recorder
        self._costs = costs if costs is not None else CostTable()
        self._tracer = trace.get_tracer("va_llm", tracer_provider=tracer_provider)
        self._in_flight = asyncio.Semaphore(concurrency)

    async def embed(
        self, texts: Sequence[str], *, org_id: uuid.UUID, document_id: uuid.UUID | None = None
    ) -> list[list[float]]:
        """One unit vector per text, in order (knowledge-base ingestion). ``ValueError`` for an empty text;
        :class:`LlmUnavailableError` when a batch fails (the other batches are cancelled)."""
        inputs = _checked(texts)
        if not inputs:
            return []
        batches = [
            _Batch("ingest", org_id, document_id, inputs[start : start + EMBED_BATCH_SIZE])
            for start in range(0, len(inputs), EMBED_BATCH_SIZE)
        ]
        started = self._clock.monotonic()
        try:
            results = await self._run_all(batches)
        finally:
            self._metrics.embedding_duration("ingest", (self._clock.monotonic() - started) * 1000.0)
        return [vector for result in results for vector in result]

    async def embed_query(self, text: str, *, org_id: uuid.UUID) -> list[float]:
        """The unit vector of a retrieval query, from the 24 h Redis cache when present."""
        (sendable,) = _checked((text,))
        cached = await self._cache.get(text)
        if cached is not None:
            return cached
        started = self._clock.monotonic()
        try:
            (vector,) = await self._embed_batch(_Batch("query", org_id, None, (sendable,)))
        finally:
            self._metrics.embedding_duration("query", (self._clock.monotonic() - started) * 1000.0)
        await self._cache.put(text, vector)
        return vector

    async def _run_all(self, batches: list[_Batch]) -> list[list[list[float]]]:
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [group.create_task(self._embed_batch(batch)) for batch in batches]
        except BaseExceptionGroup as failed:
            raise _first_error(failed) from None
        return [task.result() for task in tasks]

    async def _embed_batch(self, batch: _Batch) -> list[list[float]]:
        async with self._in_flight:
            parsed, tokens, estimated = await self._traced(batch)
        await self._meter(batch, tokens, estimated=estimated)
        return parsed.vectors

    async def _traced(self, batch: _Batch) -> tuple[ParsedEmbeddings, int, bool]:
        with self._tracer.start_as_current_span(
            "llm.embeddings",
            kind=SpanKind.CLIENT,
            attributes={
                "llm.provider": self.provider.id,
                "llm.model": self.model,
                "llm.purpose": batch.purpose,
                "llm.inputs": len(batch.inputs),
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                parsed = await self._attempts(batch, iter(RETRY_DELAYS_S))
            except LlmError as exc:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
                raise
            finally:
                _annotate(span, batch)
            estimated = parsed.prompt_tokens is None
            tokens = (
                sum(estimate_tokens(text) for text in batch.inputs)
                if parsed.prompt_tokens is None
                else parsed.prompt_tokens
            )
            span.set_attribute("llm.tokens_in", tokens)
        return parsed, tokens, estimated

    async def _attempts(self, batch: _Batch, delays: Iterator[float]) -> ParsedEmbeddings:
        while True:
            try:
                return await self._attempt(batch)
            except LlmError as error:
                wait = _retry_wait(error, next(delays, None))
                if wait is None:
                    raise LlmUnavailableError(
                        "exhausted", provider_id=self.provider.id, model=self.model, last=error
                    ) from error
                await self._clock.sleep(wait)

    async def _attempt(self, batch: _Batch) -> ParsedEmbeddings:
        where = {"provider_id": self.provider.id, "model": self.model}
        batch.attempts += 1
        try:
            status, body = await within(self._clock, ATTEMPT_TIMEOUT_S, self._send(batch.inputs))
        except DeadlineExceeded:
            error: LlmError = LlmTimeoutError(
                f"{self.provider.id} {self.model}: no embeddings within {ATTEMPT_TIMEOUT_S:g} s", **where
            )
        except openai.OpenAIError as exc:
            error = map_openai_error(exc, now=self._clock.now(), **where)
        else:
            batch.status = status
            try:
                parsed = parse_embeddings(
                    body, expected=len(batch.inputs), dimensions=self.dimensions, **where
                )
            except LlmError as exc:
                self._failed(batch, exc)
                raise
            self._metrics.embedding_attempt(self.model, "ok")
            return parsed
        self._failed(batch, error)
        raise error

    async def _send(self, inputs: tuple[str, ...]) -> tuple[int, bytes]:
        # The SDK's TypedDict overloads are bypassed: the body is decoded by parse_embeddings, not the SDK.
        create: Any = self._client.embeddings.with_raw_response.create
        raw = await create(
            model=self.model,
            input=list(inputs),
            dimensions=self.dimensions,
            encoding_format="float",
            timeout=sdk_timeout(sdk_backstop(ATTEMPT_TIMEOUT_S)),
        )
        return int(raw.status_code), bytes(raw.content)

    def _failed(self, batch: _Batch, error: LlmError) -> None:
        if isinstance(error, LlmHttpError):
            batch.status = error.status
        self._metrics.embedding_attempt(self.model, attempt_outcome(error))
        self._metrics.error(self.model, error.error_kind)
        _log.warning(
            "llm.embedding_attempt_failed",
            provider=self.provider.id,
            model=self.model,
            purpose=batch.purpose,
            kind=error.error_kind,
            status=error.status if isinstance(error, LlmHttpError) else None,
            attempt=batch.attempts,
        )

    async def _meter(self, batch: _Batch, tokens: int, *, estimated: bool) -> None:
        self._metrics.tokens(self.model, tokens, 0)
        if self._recorder is None:
            return
        cost, _ = self._costs.cost_micros(self.model, tokens, 0)
        record = EmbeddingUsageRecord(
            org_id=batch.org_id,
            document_id=batch.document_id,
            purpose=batch.purpose,
            provider_id=self.provider.id,
            model=self.model,
            prompt_tokens=tokens,
            estimated=estimated,
            cost_micros=cost,
        )
        try:
            await self._recorder.record(record)
        except Exception as exc:  # metering must never fail an embedding
            _log.warning("llm.usage_record_failed", model=self.model, error=type(exc).__name__)


def _checked(texts: Sequence[str]) -> tuple[str, ...]:
    """``texts`` truncated to the input limit; ``ValueError`` for an empty text (providers reject it)."""
    if any(not text for text in texts):
        msg = "embedding inputs must be non-empty strings"
        raise ValueError(msg)
    results = [truncate_input(text) for text in texts]
    truncated = sum(1 for _, cut in results if cut)
    if truncated:
        _log.info("llm.embedding_truncated", inputs=len(texts), truncated=truncated)
    return tuple(text for text, _ in results)


def _annotate(span: Span, batch: _Batch) -> None:
    span.set_attribute("llm.attempts", batch.attempts)
    if batch.status is not None:
        span.set_attribute("http.status_code", batch.status)
