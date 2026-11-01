"""``prefetch_call_context``: store the call's :class:`~va_domain.call_context.CallContext` in Redis before
dialling (specs/01-architecture.md §2.1 step 2).

The activity reads the ``calls`` row in the call's tenant session, builds the context with
:func:`va_db.call_context.build_call_context` (carrying the current trace context so the media worker can
continue the trace), and saves it under ``call:{<id>}:ctx``. A call that already reached a terminal status
(stopped before dialling) gets no context. An unknown call (or another org's) fails without retries.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from opentelemetry import propagate
from pydantic import BaseModel, ConfigDict
from temporalio import activity
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from va_common.logging import bound_context, get_logger
from va_common.time import Clock
from va_db.call_context import CallContextStore, build_call_context
from va_db.call_states import is_terminal
from va_db.models import CallStatus
from va_db.repositories import CallRepo, NotFoundError
from va_db.session import Database
from va_domain.call_context import CallContext
from va_workflows.activities.inputs import CallActivityInput

__all__ = [
    "CALL_NOT_FOUND",
    "PREFETCH_RETRY",
    "PREFETCH_TIMEOUT",
    "CallContextActivities",
    "PrefetchResult",
]

CALL_NOT_FOUND: Final = "CallNotFound"
PREFETCH_TIMEOUT: Final = timedelta(seconds=10)
PREFETCH_RETRY: Final = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=5),
    maximum_attempts=3,
    non_retryable_error_types=[CALL_NOT_FOUND],
)

_log = get_logger(__name__)


def _call_not_found() -> ApplicationError:
    return ApplicationError("call not found", type=CALL_NOT_FOUND, non_retryable=True)


class PrefetchResult(BaseModel):
    """``stored`` tells whether a context was saved, ``status`` is the call's status when it was read, and
    ``ttl_s`` the context's Redis TTL when stored."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stored: bool
    status: str
    ttl_s: int | None = None


class CallContextActivities:
    """Activities that prepare the per-call context; one instance per worker process."""

    def __init__(self, database: Database, store: CallContextStore, clock: Clock) -> None:
        self._database = database
        self._store = store
        self._clock = clock

    @activity.defn(name="prefetch_call_context")
    async def prefetch_call_context(self, request: CallActivityInput) -> PrefetchResult:
        with bound_context(org_id=str(request.org_id), call_id=str(request.call_id)):
            carrier: dict[str, str] = {}
            propagate.inject(carrier)
            status, context = await self._build(request, carrier)
            if context is None:
                _log.info("call_context.skipped", status=status)
                return PrefetchResult(stored=False, status=status)
            await self._store.save(context)
            ttl_s = context.ttl_s()
            _log.info("call_context.stored", status=status, ttl_s=ttl_s)
            return PrefetchResult(stored=True, status=status, ttl_s=ttl_s)

    async def _build(
        self, request: CallActivityInput, carrier: dict[str, str]
    ) -> tuple[CallStatus, CallContext | None]:
        """The call's status and its context (``None`` when the call is terminal and needs none)."""
        async with self._database.tenant_session(request.org_id) as session:
            try:
                call = await CallRepo(session, request.org_id).get(request.call_id)
            except NotFoundError:
                raise _call_not_found() from None
            if is_terminal(call.status):
                return call.status, None
            return call.status, build_call_context(call, now=self._clock.now(), trace_context=carrier)
