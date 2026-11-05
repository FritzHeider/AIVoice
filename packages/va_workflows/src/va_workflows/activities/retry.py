"""``schedule_retry_call``: create the voicemail retry child of an ended call (specs/05-calls-api.md §3
``retry``; specs/07 §14 Decision P05-T10f).

``CallWorkflow`` runs it once the call's lifecycle is over. The activity reads the parent's recorded
``end_reason`` and decides with :mod:`va_domain.call_retry`; a retry is an ordinary **scheduled** call
(``start_time = now + retry.wait``, ``parent_call_id`` set, the parent's configuration with the retry's
voicemail settings). The child's ``CallWorkflow`` sleeps until then and its ``dial`` admits it like every
scheduled call (compliance, limits, credit hold), so a refusal is recorded on the child as
``failed``/``dispatch_blocked``.

The parent row is locked (``FOR UPDATE``) while the existing child is looked up, so a repeated attempt (an
activity retry after a lost commit, or a workflow replay) returns ``already_scheduled`` with the same child.
"""

from __future__ import annotations

from datetime import timedelta

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio import activity

from va_common.ids import new_id
from va_common.logging import bound_context, get_logger
from va_common.time import Clock
from va_db.call_children import COPIED_COLUMNS, child_call
from va_db.call_context import config_of
from va_db.call_states import is_terminal
from va_db.models import Call
from va_db.session import Database
from va_domain.call_retry import RETRY_ATTEMPT_KEY, child_config, retry_attempt, retry_wanted
from va_domain.calls.config import RetryConfig
from va_events import SCHEDULED_MESSAGE, DomainEvent, EventPublisher, EventType
from va_workflows.activities.errors import call_not_found
from va_workflows.activities.events import broadcast_events, call_event, persist_events
from va_workflows.activities.inputs import CallActivityInput
from va_workflows.activities.retry_inputs import RetryPlan

__all__ = ["COPIED_COLUMNS", "RetryActivities"]

_log = get_logger(__name__)


class RetryActivities:
    """Creates voicemail retry children; one instance per worker process."""

    def __init__(self, *, database: Database, publisher: EventPublisher, clock: Clock) -> None:
        self._database = database
        self._publisher = publisher
        self._clock = clock

    @activity.defn(name="schedule_retry_call")
    async def schedule_retry_call(self, call: CallActivityInput) -> RetryPlan:
        with bound_context(org_id=str(call.org_id), call_id=str(call.call_id)):
            events: list[DomainEvent] = []
            async with self._database.tenant_session(call.org_id) as session:
                parent = await self._lock_parent(session, call)
                plan, retry = await self._decide(session, parent)
                if plan.reason is None and retry is not None:
                    plan, child = self._create_child(parent, retry, retry_attempt(parent.config) + 1)
                    session.add(child)
                    await session.flush()
                    events = await persist_events(
                        session, self._publisher, [self._scheduled_event(child, plan)]
                    )
            await broadcast_events(self._publisher, events)
            if plan.reason is None:
                _log.info("calls.retry_scheduled", attempt=plan.attempt, wait_s=retry.wait if retry else None)
            else:
                _log.info("calls.retry_skipped", reason=plan.reason)
            return plan

    async def _lock_parent(self, session: AsyncSession, call: CallActivityInput) -> Call:
        parent = (
            await session.execute(
                select(Call).where(Call.id == call.call_id, Call.org_id == call.org_id).with_for_update()
            )
        ).scalar_one_or_none()
        if parent is None:
            raise call_not_found()
        return parent

    async def _decide(  # noqa: PLR0911 - one skip reason per rule, in order
        self, session: AsyncSession, parent: Call
    ) -> tuple[RetryPlan, RetryConfig | None]:
        """A skip plan, or an empty plan and the ``retry`` to create the child with."""
        if parent.direction != "outbound":
            return RetryPlan(reason="not_outbound"), None
        if not is_terminal(parent.status):
            return RetryPlan(reason="not_ended"), None
        retry = config_of(parent).retry
        if retry is None:
            return RetryPlan(reason="no_retry"), None
        if not retry_wanted(retry, parent.end_reason):
            return RetryPlan(reason="end_reason"), None
        existing = (
            await session.execute(
                select(Call.id, Call.start_time, Call.config).where(
                    Call.org_id == parent.org_id,
                    Call.parent_call_id == parent.id,
                    Call.config.has_key(RETRY_ATTEMPT_KEY),
                )
            )
        ).first()
        if existing is not None:
            child_id, start_time, stored = existing
            return (
                RetryPlan(
                    child_call_id=child_id,
                    scheduled_for=start_time,
                    attempt=retry_attempt(stored),
                    reason="already_scheduled",
                ),
                None,
            )
        if retry_attempt(parent.config) >= retry.max_attempts:
            return RetryPlan(reason="attempts_exhausted"), None
        return RetryPlan(), retry

    def _create_child(self, parent: Call, retry: RetryConfig, attempt: int) -> tuple[RetryPlan, Call]:
        child_id = new_id()
        scheduled_for = self._clock.now() + timedelta(seconds=retry.wait)
        stored: dict[str, JsonValue] = dict(parent.config)
        child = child_call(
            parent,
            child_id=child_id,
            config=child_config(stored, retry, attempt, scheduled_for),
            start_time=scheduled_for,
        )
        plan = RetryPlan(child_call_id=child_id, scheduled_for=scheduled_for, attempt=attempt)
        return plan, child

    def _scheduled_event(self, child: Call, plan: RetryPlan) -> DomainEvent:
        data: dict[str, JsonValue] = {
            "status": "scheduled",
            "scheduled_for": child.config["start_time"],
            "warnings": [],
            "parent_call_id": str(child.parent_call_id),
            "retry_attempt": plan.attempt,
        }
        return call_event(
            self._clock,
            child.org_id,
            child.id,
            EventType.CALL_SCHEDULED,
            message=SCHEDULED_MESSAGE,
            data=data,
        )
