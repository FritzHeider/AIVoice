"""Reading calls: ``GET /v1/calls/{call_id}`` and ``GET /v1/calls`` (specs/05-calls-api.md §6, §7.1;
specs/19 §4; Decision P05-T16a).

Rows are read in one ``tenant_session`` and serialized by :mod:`va_db.call_object`. ``recording_url`` is a
fresh 7-day presigned URL for calls whose recording was copied to S3; a presign failure is logged and the URL
is ``null`` (the call is still returned). The org timezone for ``start_date``/``end_date`` is read before the
tenant session opens (the settings reader uses its own session).
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final, get_args
from zoneinfo import ZoneInfo

from fastapi import Request
from redis.asyncio import Redis
from sqlalchemy import ColumnElement, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from va_api.auth.context import RequestContext
from va_api.routers.calls_read_schemas import CallListQuery
from va_api.services.call_stop import CALL_NOT_FOUND_MESSAGE
from va_common.errors import AppError
from va_common.storage import ObjectStore, SignedUrl, signed_recording_url
from va_common.time import Clock
from va_db.call_object import load_call_list_items, load_call_objects
from va_db.models import Call, CallStatus
from va_db.org_settings import OrgSettingsReader
from va_db.pagination import decode_cursor, encode_cursor
from va_db.session import Database
from va_domain.call_object import BLAND_STATUS_FILTER, CallListItem, CallObject

__all__ = [
    "ALIAS_CONFLICT_MESSAGE",
    "UNKNOWN_STATUS_MESSAGE",
    "CallList",
    "ReadDeps",
    "get_call",
    "list_calls",
    "org_call",
    "owned_call",
    "read_deps_of",
    "recording_urls",
]

ALIAS_CONFLICT_MESSAGE: Final = "{alias} and {name} must not differ."
UNKNOWN_STATUS_MESSAGE: Final = "Unknown call status {status!r}."
_SECONDS_PER_MINUTE: Final = 60
_STATUS_NAMES: Final[Mapping[str, tuple[str, ...]]] = {
    **{status: (status,) for status in get_args(CallStatus)},
    **BLAND_STATUS_FILTER,
}
"""Accepted ``status`` names: internal statuses, then Bland names (``completed`` covers ``transferred``)."""


@dataclass(frozen=True, slots=True)
class ReadDeps:
    """What reading calls needs from the container."""

    database: Database
    object_store: ObjectStore
    bucket: str
    org_settings: OrgSettingsReader
    clock: Clock
    redis: Redis


@dataclass(frozen=True, slots=True)
class CallList:
    """One page: ``total_count`` only without a cursor; ``next_cursor`` ``None`` on the last page."""

    total_count: int | None
    calls: list[CallListItem]
    next_cursor: str | None


def read_deps_of(request: Request) -> ReadDeps:
    """The read dependencies of the running app."""
    container = request.app.state.container
    return ReadDeps(
        database=container.database,
        object_store=container.object_store,
        bucket=request.app.state.settings.s3_bucket_recordings,
        org_settings=container.org_settings,
        clock=container.clock,
        redis=container.redis,
    )


async def recording_urls(
    store: ObjectStore, bucket: str, calls: Sequence[Call], now: datetime
) -> dict[uuid.UUID, SignedUrl | None]:
    """A 7-day presigned URL per call with a stored recording; ``None`` when presigning failed."""
    urls: dict[uuid.UUID, SignedUrl | None] = {}
    for call in calls:
        if call.recording_s3_key is None:
            continue
        urls[call.id] = await signed_recording_url(
            store,
            bucket,
            call.recording_s3_key,
            now=now,
            failure_event="calls.recording_url_failed",
            call_id=str(call.id),
        )
    return urls


async def org_call(session: AsyncSession, org_id: uuid.UUID, call_id: uuid.UUID) -> Call:
    """The call ``call_id`` of ``org_id``; ``CALL_NOT_FOUND`` when absent (or another org's)."""
    call = await session.scalar(select(Call).where(Call.org_id == org_id, Call.id == call_id))
    if call is None:
        raise AppError("CALL_NOT_FOUND", CALL_NOT_FOUND_MESSAGE)
    return call


async def owned_call(session: AsyncSession, ctx: RequestContext, call_id: uuid.UUID) -> Call:
    """The call ``call_id`` of ``ctx.org_id``; ``CALL_NOT_FOUND`` when absent (or another org's)."""
    return await org_call(session, ctx.org_id, call_id)


async def get_call(deps: ReadDeps, ctx: RequestContext, call_id: uuid.UUID) -> CallObject:
    """The Call Object of one call of ``ctx.org_id``; ``CALL_NOT_FOUND`` when absent (or another org's)."""
    async with deps.database.tenant_session(ctx.org_id) as session:
        call = await owned_call(session, ctx, call_id)
        urls = await recording_urls(deps.object_store, deps.bucket, [call], deps.clock.now())
        (found,) = await load_call_objects(session, [call], recording_urls=urls)
    return found


def _alias(query: CallListQuery, name: str, alias: str, alias_field: str | None = None) -> Any:
    value = getattr(query, name)
    other = getattr(query, alias_field or alias)
    if value is not None and other is not None and value != other:
        raise AppError("INVALID_INPUT", ALIAS_CONFLICT_MESSAGE.format(alias=alias, name=name), field=alias)
    return value if value is not None else other


def _statuses(value: str) -> list[str]:
    statuses: list[str] = []
    for raw in value.split(","):
        name = raw.strip()
        if not name:
            continue
        mapped = _STATUS_NAMES.get(name)
        if mapped is None:
            raise AppError("INVALID_INPUT", UNKNOWN_STATUS_MESSAGE.format(status=name), field="status")
        statuses.extend(mapped)
    return statuses


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _local_midnight(day: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(day, time.min, tzinfo=zone)


def _equality_filters(query: CallListQuery) -> list[ColumnElement[bool]]:
    pairs: tuple[tuple[Any, Any], ...] = (
        (Call.to_number, _alias(query, "to_number", "to")),
        (Call.from_number, _alias(query, "from_number", "from", "from_")),
        (Call.batch_id, _alias(query, "batch_id", "campaign_id")),
        (Call.pathway_id, query.pathway_id),
        (Call.persona_id, query.persona_id),
        (Call.agent_id, query.agent_id),
        (Call.disposition_tag, query.disposition_tag),
        (Call.external_id, query.external_id),
        (Call.answered_by, None if query.answered_by is None else query.answered_by.replace("-", "_")),
    )
    return [column == value for column, value in pairs if value is not None]


def _state_filters(query: CallListQuery) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if query.completed is not None:
        complete = Call.queue_status == "complete"
        conditions.append(complete if query.completed else Call.queue_status.is_distinct_from("complete"))
    if query.inbound is not None:
        conditions.append(Call.direction == "inbound" if query.inbound else Call.direction != "inbound")
    if query.status is not None:
        conditions.append(Call.status.in_(_statuses(query.status)))
    if query.duration_gt is not None:
        conditions.append(Call.connected_seconds > query.duration_gt * _SECONDS_PER_MINUTE)
    if query.duration_lt is not None:
        conditions.append(Call.connected_seconds < query.duration_lt * _SECONDS_PER_MINUTE)
    return conditions


def _time_filters(query: CallListQuery, zone: ZoneInfo | None) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if query.created_at_gte is not None:
        conditions.append(Call.created_at >= _aware(query.created_at_gte))
    if query.created_at_lte is not None:
        conditions.append(Call.created_at <= _aware(query.created_at_lte))
    if zone is not None and query.start_date is not None:
        conditions.append(Call.created_at >= _local_midnight(query.start_date, zone))
    if zone is not None and query.end_date is not None:
        conditions.append(Call.created_at < _local_midnight(query.end_date + timedelta(days=1), zone))
    return conditions


def _filters(query: CallListQuery, zone: ZoneInfo | None) -> list[ColumnElement[bool]]:
    return [*_equality_filters(query), *_state_filters(query), *_time_filters(query, zone)]


async def _org_zone(deps: ReadDeps, ctx: RequestContext, query: CallListQuery) -> ZoneInfo | None:
    if query.start_date is None and query.end_date is None:
        return None
    settings = await deps.org_settings.get_lenient(ctx.org_id)
    return ZoneInfo("UTC" if settings is None else settings.timezone)


async def _page(
    session: AsyncSession, ctx: RequestContext, query: CallListQuery, conditions: list[ColumnElement[bool]]
) -> tuple[int | None, list[Call]]:
    scoped = [Call.org_id == ctx.org_id, *conditions]
    total = None
    if query.cursor is None:
        total = await session.scalar(select(func.count()).select_from(Call).where(*scoped))
    statement = select(Call).where(*scoped)
    key = tuple_(Call.created_at, Call.id)
    if query.cursor is not None:
        cursor = decode_cursor(query.cursor)
        bound = tuple_(cursor.created_at, cursor.id)
        statement = statement.where(key > bound if query.ascending else key < bound)
    order = (
        (Call.created_at.asc(), Call.id.asc())
        if query.ascending
        else (Call.created_at.desc(), Call.id.desc())
    )
    rows = await session.scalars(statement.order_by(*order).limit(query.limit + 1))
    return total, list(rows)


async def list_calls(deps: ReadDeps, ctx: RequestContext, query: CallListQuery) -> CallList:
    """A page of ``ctx.org_id``'s calls matching ``query`` (newest first unless ``ascending``)."""
    zone = await _org_zone(deps, ctx, query)
    conditions = _filters(query, zone)
    async with deps.database.tenant_session(ctx.org_id) as session:
        total, rows = await _page(session, ctx, query, conditions)
        page = rows[: query.limit]
        urls = await recording_urls(deps.object_store, deps.bucket, page, deps.clock.now())
        items = await load_call_list_items(session, page, recording_urls=urls)
    more = len(rows) > query.limit
    next_cursor = encode_cursor(page[-1].created_at, page[-1].id) if more else None
    return CallList(total_count=total, calls=items, next_cursor=next_cursor)
