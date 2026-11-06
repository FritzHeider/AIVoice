"""Pre-call dynamic data of ``prefetch_call_context`` (specs/13 §7 Decisions P08-T03a and P08-T03b).

:meth:`PreCallDynamicData.fetch` runs every ``dynamic_data`` fetch of the call, whatever its ``timing``
(concurrently, each rendered in the same scope: the call's variables, ``request_data`` and built-ins), within
:data:`~va_tools.dynamic_data.PRE_CALL_BUDGET_S` and returns the context with the variables and context lines
merged in: the merged lines of the ``timing: "pre_call"`` entries in ``context_sections``, the lines of each
``timing: "every_turn"`` entry under its index in ``turn_sections`` (the first value of what the media worker
refreshes at every caller turn). The requests carry ``User-Agent`` and ``X-VA-Call-Id`` and are not signed;
the answers may come from the org's ``dyn:`` cache.

:meth:`PreCallDynamicData.record`, called once the context is saved, writes one ``call.dynamic_data`` event
per fetch (level ``warning`` for a failed one) and merges the variables into ``calls.variables`` in one
transaction, then broadcasts the events. It never raises: dynamic data never fails a call, so a failed
recording is logged (``call_context.dynamic_data_record_failed``) and the call goes on with its context.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from redis.asyncio import Redis

from va_common.logging import get_logger
from va_common.time import Clock
from va_db.repositories import CallRepo
from va_db.session import Database
from va_domain.call_context import CallContext
from va_engine.builtins import call_builtins
from va_engine.templating import TemplateScope
from va_events import EventPublisher, EventType
from va_tools.dynamic_data import (
    PRE_CALL_BUDGET_S,
    PRE_CALL_TIMEOUT_MS,
    DynamicDataFetcher,
    DynamicDataResult,
    merge_results,
)
from va_tools.http import HttpExecutor
from va_workflows.activities.events import broadcast_events, call_event, persist_events

__all__ = ["PreCallData", "PreCallDynamicData"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PreCallData:
    """The context with the pre-call variables and context lines, and the fetches that produced them."""

    context: CallContext
    results: tuple[DynamicDataResult, ...] = ()


class PreCallDynamicData:
    """Runs and records the pre-call fetches of calls; one instance per worker process.

    ``http`` is the guarded tool client (:func:`va_tools.http.tools_client`); ``allow_http`` admits plain
    ``http://`` URLs (local and test environments only).
    """

    def __init__(  # noqa: PLR0913 - the process's shared collaborators, keyword-only
        self,
        *,
        database: Database,
        redis: Redis,
        http: httpx.AsyncClient,
        publisher: EventPublisher,
        clock: Clock,
        allow_http: bool,
    ) -> None:
        self._database = database
        self._redis = redis
        self._http = http
        self._publisher = publisher
        self._clock = clock
        self._allow_http = allow_http

    async def fetch(self, context: CallContext) -> PreCallData:
        """``context`` with the results of its dynamic data fetches merged in (unchanged when it has none)."""
        config = context.config
        specs = list(enumerate(config.dynamic_data))
        if not specs:
            return PreCallData(context)
        scope = TemplateScope(
            variables=context.variables,
            request_data=config.request_data,
            builtins=call_builtins(context, now=self._clock.now()),
            timezone=config.timezone,
        )
        executor = HttpExecutor(
            self._http, clock=self._clock, allow_http=self._allow_http, call_id=str(context.call_id)
        )
        fetcher = DynamicDataFetcher(executor, redis=self._redis, org_id=context.org_id, clock=self._clock)
        results = await fetcher.fetch_all(
            specs, scope, timeout_ms=PRE_CALL_TIMEOUT_MS, budget_s=PRE_CALL_BUDGET_S
        )
        variables, _ = merge_results(results)
        _, lines = merge_results([result for result in results if result.timing == "pre_call"])
        turn_sections = {
            result.index: list(result.sections.values())
            for result in results
            if result.timing == "every_turn" and result.sections
        }
        merged = context.model_copy(
            update={
                "variables": {**context.variables, **variables},
                "context_sections": [*context.context_sections, *lines],
                "turn_sections": {**context.turn_sections, **turn_sections},
            }
        )
        return PreCallData(merged, tuple(results))

    async def record(self, data: PreCallData) -> None:
        """Write the events and the variables of ``data``; never raises."""
        if not data.results:
            return
        try:
            await self._record(data)
        except Exception as exc:  # dynamic data never fails the call: the stored context already has it
            _log.warning("call_context.dynamic_data_record_failed", error_type=type(exc).__name__)

    async def _record(self, data: PreCallData) -> None:
        context = data.context
        events = [
            call_event(
                self._clock,
                context.org_id,
                context.call_id,
                EventType.CALL_DYNAMIC_DATA,
                message=result.message(),
                data=result.event_data(),
                level=result.level,
            )
            for result in data.results
        ]
        variables, _ = merge_results(data.results)
        async with self._database.tenant_session(context.org_id) as session:
            sequenced = await persist_events(session, self._publisher, events)
            if variables:
                await CallRepo(session, context.org_id).merge_variables(context.call_id, variables)
        await broadcast_events(self._publisher, sequenced)
