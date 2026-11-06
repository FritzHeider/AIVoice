"""Per-turn dynamic data of calls in the media worker (specs/13 §7 Decision P08-T03b).

:class:`TurnDataSource` holds the per-process pieces (the guarded tool HTTP client shared with tools and
Webhook nodes, the Redis client of the ``dyn:`` cache, which the worker's pre-call fetches fill too, and the
clock) and builds the :class:`~va_tools.turn_data.TurnDynamicData` of a call that has ``timing:
"every_turn"`` entries: the requests carry ``User-Agent`` and ``X-VA-Call-Id`` and are not signed (as the
pre-call fetches), the lines start from the context's ``turn_sections``, and every fetch is reported to the
call's recorder as ``call.dynamic_data``.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from redis.asyncio import Redis

from va_common.time import Clock
from va_domain.call_context import CallContext
from va_tools.dynamic_data import DynamicDataFetcher
from va_tools.http import HttpExecutor
from va_tools.turn_data import DynamicDataEvents, TurnDynamicData, turn_specs

__all__ = ["TurnDataSource"]


@dataclass(frozen=True, slots=True, kw_only=True)
class TurnDataSource:
    """Builds the per-turn dynamic data of calls; one per process.

    ``allow_http`` admits plain ``http://`` URLs (local and test environments only); ``redis`` ``None``
    disables the cache.
    """

    tools_http: httpx.AsyncClient
    allow_http: bool
    redis: Redis | None
    clock: Clock

    def for_call(self, context: CallContext, events: DynamicDataEvents | None) -> TurnDynamicData | None:
        """The per-turn fetches of ``context`` reporting to ``events``; ``None`` when the call has none."""
        specs = turn_specs(context.config.dynamic_data)
        if not specs:
            return None
        http = HttpExecutor(
            self.tools_http, clock=self.clock, allow_http=self.allow_http, call_id=str(context.call_id)
        )
        fetcher = DynamicDataFetcher(http, redis=self.redis, org_id=context.org_id, clock=self.clock)
        return TurnDynamicData(fetcher, specs, initial=context.turn_sections, events=events)
