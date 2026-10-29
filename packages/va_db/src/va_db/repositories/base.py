"""Generic tenant repository and the system-scope marker (specs/03-data-model.md §1, §2).

Pagination and error codes follow specs/31-error-catalog.md §2, §3. :class:`TenantRepository` gives every
org-owned table get/list/create/update/soft-delete with keyset pagination. It runs on a ``tenant_session``
(RLS) and still filters ``org_id`` explicitly, so platform rows with a NULL ``org_id`` (``voices``,
``dnc_entries``) never appear through it. Rows of another org and soft-deleted rows are reported as
:class:`NotFoundError`, which never reveals whether the row exists.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any, ClassVar, Final

from sqlalchemy import ColumnElement, Select, Table, inspect, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.errors import AppError
from va_common.time import Clock, SystemClock
from va_db.base import Base
from va_db.pagination import DEFAULT_PAGE_LIMIT, Page, decode_cursor, encode_cursor, validate_limit

__all__ = [
    "IMMUTABLE_COLUMNS",
    "NotFoundError",
    "SystemRepository",
    "TenantRepository",
    "conflict_savepoint",
    "is_unique_violation",
    "require_positive",
]

IMMUTABLE_COLUMNS: Final = frozenset({"id", "org_id", "created_at", "updated_at", "deleted_at"})
"""Columns that ``TenantRepository.update`` never takes (``create`` accepts only ``id`` among them)."""

_REQUIRED_COLUMNS: Final = ("id", "org_id", "created_at")
_CREATED_AT: Final = "created_at"
_DELETED_AT: Final = "deleted_at"
_ID: Final = "id"
_UNIQUE_VIOLATION: Final = "23505"


class NotFoundError(AppError):
    """``NOT_FOUND`` for a row that is absent, soft-deleted, or owned by another org (never says which)."""

    def __init__(self, resource: str) -> None:
        super().__init__("NOT_FOUND", f"{resource} not found.")
        self.resource = resource


def is_unique_violation(exc: IntegrityError) -> bool:
    """Whether ``exc`` is a Postgres unique violation (SQLSTATE 23505)."""
    return getattr(exc.orig, "sqlstate", None) == _UNIQUE_VIOLATION


@asynccontextmanager
async def conflict_savepoint(
    session: AsyncSession, message: str, *, field: str | None = None
) -> AsyncIterator[None]:
    """Apply the block's changes and flush them in a savepoint; a unique violation becomes ``CONFLICT``.

    Changes must be made inside the block: ``begin_nested()`` first flushes pending changes into the
    enclosing transaction, where a failure would abort the caller's whole transaction. After a conflict the
    caller's transaction stays usable.
    """
    try:
        async with session.begin_nested():
            yield
            await session.flush()
    except IntegrityError as exc:
        if is_unique_violation(exc):
            raise AppError("CONFLICT", message, field=field) from exc
        raise


def require_positive(name: str, value: int) -> int:
    """``value`` when it is at least 1, else ``ValueError`` (a programming error, not user input)."""
    if value < 1:
        msg = f"{name} must be at least 1, got {value}"
        raise ValueError(msg)
    return value


def _attribute(row: Base, name: str) -> Any:
    return inspect(row).attrs[name].value


class TenantRepository[M: Base]:
    """Org-scoped data access for one model; use it inside ``tenant_session(org_id)`` with the same org."""

    def __init__(
        self,
        model: type[M],
        session: AsyncSession,
        org_id: uuid.UUID,
        *,
        clock: Clock | None = None,
    ) -> None:
        table = model.__table__
        if not isinstance(table, Table) or not all(name in table.c for name in _REQUIRED_COLUMNS):
            msg = f"{model.__tablename__} is not a tenant resource table (needs id, org_id, created_at)"
            raise TypeError(msg)
        self.model = model
        self.session = session
        self.org_id = org_id
        self._table = table
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._attributes = frozenset(inspect(model).attrs.keys())
        self._soft = _DELETED_AT in table.c

    @property
    def resource(self) -> str:
        """The table name used in error messages."""
        return self._table.name

    def _select(self, *, include_deleted: bool) -> Select[M]:
        statement = select(self.model).where(self._table.c.org_id == self.org_id)
        if self._soft and not include_deleted:
            statement = statement.where(self._table.c.deleted_at.is_(None))
        return statement

    def _validate_keys(self, values: Mapping[str, Any], forbidden: Collection[str]) -> None:
        rejected = sorted(key for key in values if key in forbidden or key not in self._attributes)
        if rejected:
            msg = f"{self.resource} does not accept {', '.join(rejected)}"
            raise ValueError(msg)

    def _savepoint(self) -> AbstractAsyncContextManager[None]:
        """:func:`conflict_savepoint` with the message "<table> already exists."."""
        return conflict_savepoint(self.session, f"{self.resource} already exists.")

    async def get(self, row_id: uuid.UUID, *, include_deleted: bool = False) -> M:
        """The org's row ``row_id``; :class:`NotFoundError` when absent, deleted, or another org's."""
        statement = self._select(include_deleted=include_deleted).where(self._table.c.id == row_id)
        row = await self.session.scalar(statement)
        if row is None:
            raise NotFoundError(self.resource)
        return row

    async def list(
        self,
        *,
        limit: int = DEFAULT_PAGE_LIMIT,
        cursor: str | None = None,
        where: Sequence[ColumnElement[bool]] = (),
        include_deleted: bool = False,
    ) -> Page[M]:
        """One page, newest first (``created_at DESC, id DESC``), after ``cursor`` when given."""
        validate_limit(limit)
        created_at, row_id = self._table.c.created_at, self._table.c.id
        statement = self._select(include_deleted=include_deleted).where(*where)
        if cursor is not None:
            key = decode_cursor(cursor)
            statement = statement.where(tuple_(created_at, row_id) < tuple_(key.created_at, key.id))
        statement = statement.order_by(created_at.desc(), row_id.desc()).limit(limit + 1)
        rows = tuple((await self.session.scalars(statement)).all())
        items, has_more = rows[:limit], len(rows) > limit
        next_cursor = None
        if has_more:
            last = items[-1]
            next_cursor = encode_cursor(_attribute(last, _CREATED_AT), _attribute(last, _ID))
        return Page(items=items, next_cursor=next_cursor, has_more=has_more)

    async def create(self, values: Mapping[str, Any]) -> M:
        """Insert a row owned by the org; ``CONFLICT`` on a unique violation (the session stays usable)."""
        self._validate_keys(values, IMMUTABLE_COLUMNS - {_ID})
        row = self.model(**values, org_id=self.org_id)
        async with self._savepoint():
            self.session.add(row)
        await self.session.refresh(row)
        return row

    async def update(self, row_id: uuid.UUID, values: Mapping[str, Any]) -> M:
        """Change columns of the org's live row ``row_id``; ``NOT_FOUND`` / ``CONFLICT`` as for get/create."""
        self._validate_keys(values, IMMUTABLE_COLUMNS)
        row = await self.get(row_id)
        async with self._savepoint():
            for key, value in values.items():
                setattr(row, key, value)
        await self.session.refresh(row)
        return row

    async def soft_delete(self, row_id: uuid.UUID) -> M:
        """Set ``deleted_at`` on the org's live row ``row_id``; a deleted row is ``NOT_FOUND``."""
        if not self._soft:
            msg = f"{self.resource} has no deleted_at column"
            raise TypeError(msg)
        row = await self.get(row_id)
        async with self._savepoint():
            setattr(row, _DELETED_AT, self._clock.now())
        await self.session.refresh(row)
        return row


class SystemRepository:
    """Marker base of the ``va_system`` (``BYPASSRLS``) repositories listed in specs/03-data-model.md §2.

    Only the repositories named there may subclass it, and they take a session from
    ``Database.system_session()``; a test keeps the subclasses equal to that list.
    """

    system_scope: ClassVar[bool] = True

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
