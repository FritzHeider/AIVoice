"""The attached call's :class:`~va_domain.call_context.CallContext` (specs/07-media-pipeline.md §3 step 3;
specs/28 §6.2; specs/07 §3 Decision P05-T07b).

:class:`DbCallContextLoader` reads ``call:{<id>}:ctx``; the org comes from the context, or, when it is
missing, from the system ``CallLegLookupRepo`` for the ``start`` frame's ``call_control_id`` (which must be
the primary leg of the same call). The ``calls`` row is then read in the org's tenant session: a missing call
refuses the stream with ``call_unknown``, a status other than ``dialing``/``ringing``/``in_progress`` with
``call_status``. A missing context is rebuilt from ``calls.config`` and saved (a save error is logged; the
call proceeds).
"""

from __future__ import annotations

import uuid
from typing import Final, Literal, Protocol

from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from va_common.logging import get_logger
from va_common.time import Clock
from va_db.call_context import CallContextStore, build_call_context
from va_db.repositories import CallLegLookupRepo, CallRepo, NotFoundError
from va_db.session import Database
from va_domain.call_context import CallContext

__all__ = [
    "ACTIVE_STATUSES",
    "CallContextLoader",
    "ContextRefusedError",
    "DbCallContextLoader",
    "RefusalReason",
]

ACTIVE_STATUSES: Final = frozenset({"dialing", "ringing", "in_progress"})

type RefusalReason = Literal["call_unknown", "call_status", "database", "redis"]

_log = get_logger(__name__)


class ContextRefusedError(Exception):
    """The stream must be refused: ``call_unknown``/``call_status`` (4401), ``database``/``redis`` (1011)."""

    def __init__(self, reason: RefusalReason, *, detail: str = "") -> None:
        super().__init__(f"call context refused: {reason}")
        self.reason: RefusalReason = reason
        self.detail = detail or reason


class CallContextLoader(Protocol):
    async def load(self, call_id: uuid.UUID, call_control_id: str) -> CallContext:
        """The call's context; raises :class:`ContextRefusedError`."""
        ...


class DbCallContextLoader:
    """Redis context first, the database for the org (when needed), the status check, and the rebuild."""

    def __init__(self, database: Database, store: CallContextStore, clock: Clock) -> None:
        self._database = database
        self._store = store
        self._clock = clock

    async def load(self, call_id: uuid.UUID, call_control_id: str) -> CallContext:
        try:
            stored = await self._store.load(call_id)
        except (RedisError, OSError) as exc:
            raise ContextRefusedError("redis", detail=type(exc).__name__) from exc
        try:
            org_id = stored.org_id if stored is not None else await self._org_of(call_id, call_control_id)
            current = await self._read(org_id, call_id, rebuild=stored is None)
        except (SQLAlchemyError, OSError, ValidationError) as exc:
            raise ContextRefusedError("database", detail=type(exc).__name__) from exc
        if stored is not None:
            return stored
        assert current is not None  # noqa: S101 - rebuilt whenever nothing was stored
        try:
            await self._store.save(current)
        except (RedisError, OSError) as exc:
            _log.warning("media.context_save_failed", detail=type(exc).__name__)
        _log.info("media.context_rebuilt", org_id=str(org_id))
        return current

    async def _org_of(self, call_id: uuid.UUID, call_control_id: str) -> uuid.UUID:
        async with self._database.system_session() as session:
            leg = await CallLegLookupRepo(session).by_call_control_id(call_control_id)
        if leg is None or leg.call_id != call_id or leg.role != "primary":
            raise ContextRefusedError("call_unknown", detail="leg_mismatch")
        return leg.org_id

    async def _read(self, org_id: uuid.UUID, call_id: uuid.UUID, *, rebuild: bool) -> CallContext | None:
        async with self._database.tenant_session(org_id) as session:
            try:
                call = await CallRepo(session, org_id).get(call_id)
            except NotFoundError:
                raise ContextRefusedError("call_unknown", detail="call_missing") from None
            if call.status not in ACTIVE_STATUSES:
                raise ContextRefusedError("call_status", detail=call.status)
            return build_call_context(call, now=self._clock.now()) if rebuild else None
