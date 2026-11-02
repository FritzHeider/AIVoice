"""Build, store, and load :class:`~va_domain.call_context.CallContext` (specs/01-architecture.md §2.1 step 2).

:func:`build_call_context` turns a ``calls`` row into a context. It is the one builder: the worker's
``prefetch_call_context`` activity, the media worker rebuilding a missing context (specs/07 §3), and the edge
for inbound calls all use it. ``calls.config`` also carries keys that are not call configuration (the
dispatch ``pricing`` snapshot, post-call steps); the context keeps only the ``SendCallConfig`` fields, plus
the inbound maintenance message (``maintenance_message``, specs/06 §7 Decision P05-T12c).

:class:`CallContextStore` keeps the JSON in Redis ``call:{<id>}:ctx``. An unreadable stored value is logged
and read as missing so the caller rebuilds it from the row.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime

from pydantic import ValidationError
from redis.asyncio import Redis

from va_common.logging import get_logger
from va_db.models import Call
from va_domain.call_context import CallContext, call_context_key
from va_domain.calls.config import SendCallConfig
from va_domain.numbers import MAINTENANCE_MESSAGE_KEY

__all__ = ["CallContextStore", "build_call_context", "config_of"]

_log = get_logger(__name__)


def config_of(call: Call) -> SendCallConfig:
    """The resolved configuration of ``call``: ``calls.config`` limited to the ``SendCallConfig`` fields."""
    fields = SendCallConfig.model_fields
    return SendCallConfig.model_validate({key: value for key, value in call.config.items() if key in fields})


def build_call_context(
    call: Call, *, now: datetime, trace_context: Mapping[str, str] | None = None
) -> CallContext:
    """The context of ``call`` at ``now``; the row's numbers win, the configuration fills NULL columns."""
    config = config_of(call)
    maintenance = call.config.get(MAINTENANCE_MESSAGE_KEY)
    return CallContext(
        call_id=call.id,
        org_id=call.org_id,
        direction=call.direction,
        to_number=call.to_number or config.phone_number,
        from_number=call.from_number or config.from_number,
        batch_id=call.batch_id,
        contact_id=call.contact_id,
        test_call=call.test_call,
        config=config,
        trace_context=dict(trace_context or {}),
        created_at=now,
        maintenance_message=maintenance if isinstance(maintenance, str) else None,
    )


class CallContextStore:
    """The Redis copy of each call's context, expiring :meth:`CallContext.ttl_s` seconds after it is saved."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def save(self, context: CallContext) -> None:
        await self._redis.set(
            call_context_key(context.call_id), context.model_dump_json(by_alias=True), ex=context.ttl_s()
        )

    async def load(self, call_id: uuid.UUID) -> CallContext | None:
        """The stored context, or ``None`` when absent or unreadable (logged ``call_context.invalid``)."""
        raw = await self._redis.get(call_context_key(call_id))
        if raw is None:
            return None
        try:
            return CallContext.model_validate_json(raw)
        except ValidationError as exc:
            _log.warning(
                "call_context.invalid", call_id=str(call_id), errors=exc.error_count(), error_type=exc.title
            )
            return None

    async def delete(self, call_id: uuid.UUID) -> None:
        await self._redis.delete(call_context_key(call_id))
