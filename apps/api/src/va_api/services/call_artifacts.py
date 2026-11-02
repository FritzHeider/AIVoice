"""Active calls, recording links, transcripts, and event logs of calls (specs/05-calls-api.md §7.4, §7.5,
§7.6, §7.9; Decision P05-T16b).

Every read runs in one ``tenant_session`` of ``ctx.org_id``; an unknown call (or another org's) is
``CALL_NOT_FOUND``.

- Active calls: statuses ``va_db.call_states.ACTIVE_STATUSES``, oldest first; ``current_node_id`` comes from
  the carrier-state hash field ``node_id`` in one Redis pipeline. A Redis failure is logged and leaves every
  node id ``null`` (the list is still returned).
- Recording: ``channel=mixed`` prefers the post-call ``mixed`` variant and falls back to the stored
  dual-channel recording; ``caller``/``agent`` need their variant. The link is presigned for 15 minutes.
  Storage failures are ``503 SERVICE_UNAVAILABLE`` (logged without the key or URL).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, get_args

from botocore.exceptions import BotoCoreError, ClientError
from redis.exceptions import RedisError
from sqlalchemy import select

from va_api.auth.context import RequestContext
from va_api.routers.calls_detail_schemas import ActiveCallItem, CallEventItem, CallEventsQuery
from va_api.services.call_read import ReadDeps, owned_call
from va_common.errors import AppError
from va_common.logging import get_logger
from va_common.storage import PLAYBACK_URL_TTL_S, SignedUrl, recording_variant_key, signed_url
from va_db.call_events_read import load_call_events
from va_db.call_object import load_transcripts
from va_db.call_states import ACTIVE_STATUSES
from va_db.models import Call, CallEventCategory
from va_domain.call_object import Transcript
from va_domain.carrier import carrier_state_key

__all__ = [
    "RECORDING_NOT_FOUND_MESSAGE",
    "RECORDING_UNAVAILABLE_MESSAGE",
    "UNKNOWN_CATEGORY_MESSAGE",
    "EventPage",
    "RecordingChannel",
    "RecordingLink",
    "active_calls",
    "call_events",
    "recording_link",
    "transcript",
]

RECORDING_NOT_FOUND_MESSAGE: Final = "Recording not found."
RECORDING_UNAVAILABLE_MESSAGE: Final = "The recording is temporarily unavailable; retry."
UNKNOWN_CATEGORY_MESSAGE: Final = "Unknown event category {category!r}."
NODE_ID_FIELD: Final = "node_id"
_CATEGORIES: Final = frozenset(get_args(CallEventCategory))
_STORAGE_ERRORS: Final = (ClientError, BotoCoreError, ValueError)
_REDIS_ERRORS: Final = (RedisError, OSError, TimeoutError)

type RecordingChannel = Literal["mixed", "caller", "agent"]

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RecordingLink:
    """A presigned recording link."""

    url: str
    expires_at: datetime
    duration_ms: int | None
    channels: Literal[1, 2]


@dataclass(frozen=True, slots=True)
class EventPage:
    """One page of a call's events; ``next_after_seq`` ``None`` on the last page."""

    events: list[CallEventItem]
    next_after_seq: int | None


def _decoded(value: bytes | str | None) -> str | None:
    text = value.decode() if isinstance(value, bytes) else value
    return text or None


async def _node_ids(deps: ReadDeps, calls: Sequence[Call]) -> list[str | None]:
    if not calls:
        return []
    try:
        async with deps.redis.pipeline(transaction=False) as pipe:
            for call in calls:
                pipe.hget(carrier_state_key(call.id), NODE_ID_FIELD)
            values = await pipe.execute()
    except _REDIS_ERRORS:
        _log.warning("calls.active_node_ids_unavailable", count=len(calls))
        return [None] * len(calls)
    return [_decoded(value) for value in values]


def _duration_seconds(call: Call, now: datetime) -> int:
    if call.answered_at is None:
        return 0
    return max(int((now - call.answered_at).total_seconds()), 0)


async def active_calls(deps: ReadDeps, ctx: RequestContext) -> list[ActiveCallItem]:
    """The org's dialing, ringing, in-progress, and transferring calls, oldest first."""
    async with deps.database.tenant_session(ctx.org_id) as session:
        rows = await session.scalars(
            select(Call)
            .where(Call.org_id == ctx.org_id, Call.status.in_(ACTIVE_STATUSES))
            .order_by(Call.created_at, Call.id)
        )
        calls = list(rows)
    nodes = await _node_ids(deps, calls)
    now = deps.clock.now()
    return [
        ActiveCallItem.model_validate(
            {
                "call_id": call.id,
                "to": call.to_number or "",
                "from": call.from_number or "",
                "status": call.status,
                "started_at": call.answered_at or call.started_at or call.dial_started_at,
                "direction": call.direction,
                "runner_type": call.runner_type,
                "pathway_id": call.pathway_id,
                "current_node_id": node,
                "duration_seconds": _duration_seconds(call, now),
            }
        )
        for call, node in zip(calls, nodes, strict=True)
    ]


def _unavailable(call_id: uuid.UUID, exc: Exception) -> AppError:
    _log.warning("calls.recording_link_failed", call_id=str(call_id), error_type=type(exc).__name__)
    return AppError("SERVICE_UNAVAILABLE", RECORDING_UNAVAILABLE_MESSAGE)


async def _recording_source(
    deps: ReadDeps, call: Call, channel: RecordingChannel
) -> tuple[str, Literal[1, 2]]:
    """The object key to link and its channel count."""
    variant = recording_variant_key(call.org_id, call.id, call.created_at, channel)
    if await deps.object_store.exists(deps.bucket, variant):
        return variant, 1
    if channel == "mixed" and call.recording_s3_key is not None:
        return call.recording_s3_key, 2
    raise AppError("NOT_FOUND", RECORDING_NOT_FOUND_MESSAGE)


async def recording_link(
    deps: ReadDeps, ctx: RequestContext, call_id: uuid.UUID, channel: RecordingChannel
) -> RecordingLink:
    """A 15-minute link to the call's recording in ``channel``; ``NOT_FOUND`` when there is none."""
    async with deps.database.tenant_session(ctx.org_id) as session:
        call = await owned_call(session, ctx, call_id)
    if call.recording_s3_key is None:
        raise AppError("NOT_FOUND", RECORDING_NOT_FOUND_MESSAGE)
    try:
        key, channels = await _recording_source(deps, call, channel)
        link: SignedUrl = await signed_url(
            deps.object_store, deps.bucket, key, now=deps.clock.now(), ttl_s=PLAYBACK_URL_TTL_S
        )
    except _STORAGE_ERRORS as exc:
        raise _unavailable(call.id, exc) from exc
    return RecordingLink(
        url=link.url, expires_at=link.expires_at, duration_ms=call.recording_duration_ms, channels=channels
    )


async def transcript(deps: ReadDeps, ctx: RequestContext, call_id: uuid.UUID) -> list[Transcript]:
    """The call's spoken turns so far (any status), as in the Call Object."""
    async with deps.database.tenant_session(ctx.org_id) as session:
        call = await owned_call(session, ctx, call_id)
        return await load_transcripts(session, call)


def _categories(value: str | None) -> list[str] | None:
    if value is None:
        return None
    names: list[str] = []
    for raw in value.split(","):
        name = raw.strip()
        if not name or name in names:
            continue
        if name not in _CATEGORIES:
            raise AppError("INVALID_INPUT", UNKNOWN_CATEGORY_MESSAGE.format(category=name), field="category")
        names.append(name)
    return names or None


async def call_events(
    deps: ReadDeps, ctx: RequestContext, call_id: uuid.UUID, query: CallEventsQuery
) -> EventPage:
    """A page of the call's events ordered by ``seq`` (only ``query.category`` categories when given)."""
    categories = _categories(query.category)
    async with deps.database.tenant_session(ctx.org_id) as session:
        call = await owned_call(session, ctx, call_id)
        rows, next_after_seq = await load_call_events(
            session, ctx.org_id, call.id, after_seq=query.after_seq, limit=query.limit, categories=categories
        )
        events = [
            CallEventItem(
                seq=row.seq,
                category=row.category,
                level=row.level,
                message=row.message,
                data=row.data,
                created_at=row.created_at,
            )
            for row in rows
        ]
    return EventPage(events=events, next_after_seq=next_after_seq)
