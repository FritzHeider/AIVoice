"""``prefetch_call_context``: store the call's :class:`~va_domain.call_context.CallContext` in Redis before
dialling (specs/01-architecture.md §2.1 step 2).

The activity reads the ``calls`` row in the call's tenant session, builds the context with
:func:`va_db.call_context.build_call_context` (carrying the current trace context so the media worker can
continue the trace), and saves it under ``call:{<id>}:ctx``. A call that already reached a terminal status
(stopped before dialling) gets no context. An unknown call (or another org's) fails without retries.

With an :class:`~va_workflows.activities.prerender.OpenerPrerender` the context carries the rendered opener
(``opener_text``) and, after the context is saved, the opener and the standard fillers are rendered into the
phrase cache (specs/07 §11 step 5, Decision P05-T10d); a failed render never fails the activity.
"""

from __future__ import annotations

from opentelemetry import propagate
from temporalio import activity

from va_common.logging import bound_context, get_logger
from va_common.time import Clock
from va_db.call_context import CallContextStore, build_call_context
from va_db.call_states import is_terminal
from va_db.models import CallStatus
from va_db.session import Database
from va_domain.call_context import CallContext
from va_workflows.activities.context_inputs import PREFETCH_RETRY, PREFETCH_TIMEOUT, PrefetchResult
from va_workflows.activities.errors import CALL_NOT_FOUND
from va_workflows.activities.inputs import CallActivityInput
from va_workflows.activities.lookup import load_call
from va_workflows.activities.prerender import OpenerPrerender

__all__ = [
    "CALL_NOT_FOUND",
    "PREFETCH_RETRY",
    "PREFETCH_TIMEOUT",
    "CallContextActivities",
    "PrefetchResult",
]

_log = get_logger(__name__)


class CallContextActivities:
    """Activities that prepare the per-call context; one instance per worker process."""

    def __init__(
        self,
        database: Database,
        store: CallContextStore,
        clock: Clock,
        *,
        prerender: OpenerPrerender | None = None,
    ) -> None:
        self._database = database
        self._store = store
        self._clock = clock
        self._prerender = prerender

    @property
    def prerender(self) -> OpenerPrerender | None:
        """The opener pre-render (``None``: contexts carry no ``opener_text``, nothing is cached)."""
        return self._prerender

    @activity.defn(name="prefetch_call_context")
    async def prefetch_call_context(self, request: CallActivityInput) -> PrefetchResult:
        with bound_context(org_id=str(request.org_id), call_id=str(request.call_id)):
            carrier: dict[str, str] = {}
            propagate.inject(carrier)
            status, context = await self._build(request, carrier)
            if context is None:
                _log.info("call_context.skipped", status=status)
                return PrefetchResult(stored=False, status=status)
            prerender = self._prerender
            if prerender is not None:
                opener = prerender.opener_for(context, self._clock.now())
                context = context.model_copy(update={"opener_text": opener})
            await self._store.save(context)
            ttl_s = context.ttl_s()
            _log.info("call_context.stored", status=status, ttl_s=ttl_s)
            opener_cached = False
            if prerender is not None:
                opener_cached = (await prerender.render(context)).opener
            return PrefetchResult(stored=True, status=status, ttl_s=ttl_s, opener_cached=opener_cached)

    async def _build(
        self, request: CallActivityInput, carrier: dict[str, str]
    ) -> tuple[CallStatus, CallContext | None]:
        """The call's status and its context (``None`` when the call is terminal and needs none)."""
        async with self._database.tenant_session(request.org_id) as session:
            call = await load_call(session, request)
            if is_terminal(call.status):
                return call.status, None
            return call.status, build_call_context(call, now=self._clock.now(), trace_context=carrier)
