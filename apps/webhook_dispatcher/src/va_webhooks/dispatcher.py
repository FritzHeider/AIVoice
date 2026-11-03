"""The outbox poll loop (specs/19-events-and-webhooks.md §8; rules: specs/19 §8 Decision P06-T03).

Each pod claims due rows with ``FOR UPDATE SKIP LOCKED`` under a 5 min lease (``OutboxRepo.claim_due``), so
several pods never deliver the same row concurrently, and a crashed pod's rows become due again when the
lease runs out. Deliveries run concurrently: at most ``concurrency`` per pod and ``per_host`` per URL host.
Each attempt's outcome, the 410 endpoint disable, and the call log row commit in one system transaction; the
live broadcast of the call log event and the admin email follow the commit. A row cancelled while its attempt
ran (its endpoint was deleted; specs/19 §10 Decision P06-T04b) keeps its state: nothing is recorded, logged
as ``webhook.delivery_cancelled``.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Protocol

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.logging import get_logger
from va_common.metrics import ServiceMetrics
from va_common.time import Clock
from va_common.waits import wait_until_set, within
from va_db.repositories import DeliveryCancelledError, OutboxRepo
from va_db.session import Database
from va_events import DomainEvent, EventPublisher, EventPublishError, insert_rows, row_for
from va_events.webhook_delivery import (
    ENDPOINT_DISABLED,
    GONE,
    SECRET_UNAVAILABLE,
    Deliverer,
    Outcome,
    Outgoing,
    SendResult,
    blocked,
    decide,
)
from va_webhooks.call_log import attempt_event
from va_webhooks.destinations import DestinationPolicy
from va_webhooks.signing_secrets import SecretUnavailableError, SigningSecrets

__all__ = ["DispatchLimits", "Dispatcher", "EndpointNotifier", "HostLimiter"]

_log = get_logger(__name__)

_METRIC_RESULT: Final = {"succeeded": "succeeded", "retry": "failed", "dead": "dead"}


@dataclass(frozen=True, slots=True)
class DispatchLimits:
    """Pod-wide concurrency, per-host concurrency, claim batch, idle poll interval, and drain budget."""

    concurrency: int = 200
    per_host: int = 20
    claim_batch: int = 200
    poll_interval_s: float = 1.0
    drain_timeout_s: float = 20.0


class EndpointNotifier(Protocol):
    async def notify(self, org_id: uuid.UUID, url: str) -> int: ...


class HostLimiter:
    """At most ``per_host`` concurrent slots per host; idle hosts are forgotten."""

    def __init__(self, per_host: int) -> None:
        self._per_host = per_host
        self._hosts: dict[str, tuple[asyncio.Semaphore, int]] = {}

    @contextlib.asynccontextmanager
    async def slot(self, host: str) -> AsyncIterator[None]:
        semaphore, users = self._hosts.get(host, (asyncio.Semaphore(self._per_host), 0))
        self._hosts[host] = (semaphore, users + 1)
        try:
            async with semaphore:
                yield
        finally:
            semaphore, users = self._hosts[host]
            if users == 1:
                del self._hosts[host]
            else:
                self._hosts[host] = (semaphore, users - 1)

    def tracked(self) -> int:
        """How many hosts currently have a waiting or running delivery."""
        return len(self._hosts)


class Dispatcher:
    """Claims due outbox rows and delivers them."""

    def __init__(  # noqa: PLR0913 - keyword-only collaborators
        self,
        *,
        database: Database,
        deliverer: Deliverer,
        secrets: SigningSecrets,
        destinations: DestinationPolicy,
        publisher: EventPublisher,
        notifier: EndpointNotifier,
        clock: Clock,
        metrics: ServiceMetrics,
        rng: random.Random | None = None,
        limits: DispatchLimits | None = None,
    ) -> None:
        self._database = database
        self._deliverer = deliverer
        self._secrets = secrets
        self._destinations = destinations
        self._publisher = publisher
        self._notifier = notifier
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()  # noqa: S311 - retry jitter, not security
        self._limits = limits if limits is not None else DispatchLimits()
        self._hosts = HostLimiter(self._limits.per_host)
        self._deliveries = metrics.counter("va_webhook_deliveries_total")
        self._delivery_ms = metrics.histogram("va_webhook_delivery_ms")
        self._backlog = metrics.gauge("va_webhook_backlog")
        self._oldest = metrics.gauge("va_webhook_oldest_pending_seconds")
        self._backlog_at: float | None = None

    @property
    def hosts(self) -> HostLimiter:
        return self._hosts

    async def run_once(self) -> int:
        """Claim one batch, deliver every row of it, and return how many rows were claimed."""
        claimed = await self._claim(self._limits.claim_batch)
        await asyncio.gather(*(self._deliver(item) for item in claimed))
        return len(claimed)

    async def run(self, stop: asyncio.Event) -> None:
        """Poll until ``stop`` is set, then wait up to ``drain_timeout_s`` for in-flight deliveries."""
        tasks: set[asyncio.Task[None]] = set()
        try:
            while not stop.is_set():
                await self._poll(stop, tasks)
            if tasks:
                await within(asyncio.gather(*tasks), self._clock, self._limits.drain_timeout_s)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _poll(self, stop: asyncio.Event, tasks: set[asyncio.Task[None]]) -> None:
        await self._refresh_backlog_if_due()
        free = self._limits.concurrency - len(tasks)
        if free <= 0:
            await self._wait_for_slot(stop, tasks)
            return
        wanted = min(free, self._limits.claim_batch)
        try:
            claimed = await self._claim(wanted)
        except (SQLAlchemyError, OSError) as exc:
            _log.warning("webhook.claim_failed", error=type(exc).__name__)
            claimed = []
        for item in claimed:
            task = asyncio.create_task(self._deliver(item), name=f"webhook:{item.id}")
            tasks.add(task)
            task.add_done_callback(tasks.discard)
        if len(claimed) < wanted:
            await wait_until_set(stop, self._clock, self._limits.poll_interval_s)

    async def _wait_for_slot(self, stop: asyncio.Event, tasks: set[asyncio.Task[None]]) -> None:
        stopped = asyncio.ensure_future(stop.wait())
        try:
            await asyncio.wait({*tasks, stopped}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopped.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopped

    async def _claim(self, limit: int) -> list[Outgoing]:
        async with self._database.system_session() as session:
            repo = OutboxRepo(session)
            rows = await repo.claim_due(self._clock.now(), limit=limit)
            enabled = await repo.endpoint_enabled({row.endpoint_id for row in rows if row.endpoint_id})
            return [Outgoing.of(row, enabled) for row in rows]

    async def refresh_backlog(self) -> None:
        """Set the backlog gauges from the outbox (rows due now and the age of the oldest)."""
        now = self._clock.now()
        async with self._database.system_session() as session:
            backlog = await OutboxRepo(session).backlog(now)
        self._backlog.set(backlog.due)
        oldest = 0.0 if backlog.oldest_due_at is None else (now - backlog.oldest_due_at).total_seconds()
        self._oldest.set(max(0.0, oldest))

    async def _refresh_backlog_if_due(self) -> None:
        now = self._clock.monotonic()
        if self._backlog_at is not None and now - self._backlog_at < self._limits.poll_interval_s:
            return
        self._backlog_at = now
        try:
            await self.refresh_backlog()
        except (SQLAlchemyError, OSError) as exc:
            _log.warning("webhook.backlog_failed", error=type(exc).__name__)

    async def _deliver(self, item: Outgoing) -> None:
        """Attempt and record one delivery; a failure is logged and the row recovers through its lease."""
        try:
            async with self._hosts.slot(item.host):
                result = await self._attempt(item)
            await self._record(item, result)
        except Exception as exc:  # one row must never stop the loop
            _log.error("webhook.delivery_failed", delivery_id=str(item.id), error=type(exc).__name__)

    async def _attempt(self, item: Outgoing) -> SendResult:
        if not item.endpoint_enabled:
            return SendResult(None, ENDPOINT_DISABLED, None)
        refusal = await self._destinations.check(item.org_id, item.url)
        if refusal is not None:
            return SendResult(None, blocked(refusal), None)
        try:
            secrets = await self._secrets.get(item.org_id)
        except SecretUnavailableError as exc:
            _log.warning("webhook.secret_unavailable", org_id=str(item.org_id), error=str(exc))
            return SendResult(None, SECRET_UNAVAILABLE, None)
        return await self._deliverer.send(item, secrets)

    async def _record(self, item: Outgoing, result: SendResult) -> None:
        now = self._clock.now()
        outcome = decide(item.attempt, result, now, self._rng)
        event = await self._sequenced(attempt_event(item, result, outcome, clock=self._clock))
        try:
            async with self._database.system_session() as session:
                repo = OutboxRepo(session)
                await self._apply(repo, item, result, outcome, now)
                disabled = (
                    result.error == GONE
                    and item.endpoint_id is not None
                    and await repo.disable_endpoint(item.endpoint_id)
                )
                event = await self._insert_call_event(session, event)
        except DeliveryCancelledError:
            _log.info("webhook.delivery_cancelled", delivery_id=str(item.id))
            return
        if event is not None:
            await self._broadcast(event)
        if disabled:
            await self._notifier.notify(item.org_id, item.url)
        self._deliveries.labels(result=_METRIC_RESULT[outcome.status], format=item.format).inc()
        if result.response_ms is not None:
            self._delivery_ms.observe(result.response_ms)

    @staticmethod
    async def _apply(
        repo: OutboxRepo, item: Outgoing, result: SendResult, outcome: Outcome, now: datetime
    ) -> None:
        if outcome.status == "succeeded" and result.status_code is not None:
            await repo.mark_succeeded(
                item.id, now=now, status_code=result.status_code, response_ms=result.response_ms or 0
            )
            return
        error = result.error or ""
        if outcome.status == "retry" and outcome.next_attempt_at is not None:
            await repo.schedule_retry(
                item.id,
                next_attempt_at=outcome.next_attempt_at,
                status_code=result.status_code,
                error=error,
                response_ms=result.response_ms,
            )
            return
        await repo.mark_dead(
            item.id, status_code=result.status_code, error=error, response_ms=result.response_ms
        )

    async def _sequenced(self, event: DomainEvent | None) -> DomainEvent | None:
        if event is None:
            return None
        try:
            return await self._publisher.sequence(event)
        except EventPublishError as exc:
            _log.warning("webhook.call_event_failed", call_id=str(event.call_id), step=exc.step)
            return None

    @staticmethod
    async def _insert_call_event(session: AsyncSession, event: DomainEvent | None) -> DomainEvent | None:
        row = None if event is None else row_for(event)
        if event is None or row is None:
            return None
        try:
            async with session.begin_nested():
                await insert_rows(session, [row])
        except SQLAlchemyError as exc:
            _log.warning("webhook.call_event_failed", call_id=str(event.call_id), error=type(exc).__name__)
            return None
        return event

    async def _broadcast(self, event: DomainEvent) -> None:
        try:
            await self._publisher.broadcast(event)
        except EventPublishError as exc:
            _log.warning("webhook.call_event_broadcast_failed", call_id=str(event.call_id), step=exc.step)
