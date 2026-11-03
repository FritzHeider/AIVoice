"""Webhook bodies (specs/19-events-and-webhooks.md §4-§7) and the HIPAA transcript strip (specs/23 §7).

Pure functions over a :class:`~va_events.model.DomainEvent`: the ``bland`` post-call body is the Call Object
the post-call workflow put into ``call.completed`` ``data`` (§4); :func:`live_body` is the ``bland-live`` body
(§5); :func:`batch_body` the batch status body (§6); :func:`envelope_body` wraps any of them (§7).
Timestamps written here are UTC ISO 8601 with milliseconds and ``Z``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from pydantic import JsonValue

from va_events.catalog import DEFAULT_CATEGORY, EventType
from va_events.model import DomainEvent

__all__ = [
    "BatchFacts",
    "batch_body",
    "content_body",
    "envelope_body",
    "iso_ms",
    "live_body",
    "strip_phi",
]

Body = dict[str, JsonValue]

_BATCH_PREFIX: Final = "batch."
_TERMINAL_SUCCESS: Final = frozenset({EventType.BATCH_COMPLETED, EventType.BATCH_STOPPED})
_DEFAULT_FAILURE_CODE: Final = "batch_failed"
_TEXT_LIVE_TYPES: Final = frozenset({EventType.CALL_TURN, EventType.CALL_POST_TRANSFER_TRANSCRIPT})
_PATHWAY_LOG_TEXT_KEYS: Final = ("text", "user_text")


def iso_ms(moment: datetime) -> str:
    """``moment`` in UTC as ISO 8601 with milliseconds and ``Z`` (``2026-10-24T18:22:31.412Z``)."""
    utc = moment.astimezone(UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _uuid_or_none(value: uuid.UUID | None) -> str | None:
    return None if value is None else str(value)


def live_body(event: DomainEvent, metadata: Mapping[str, JsonValue]) -> Body:
    """The §5 ``bland-live`` body of a call event; ``metadata`` is the call's ``metadata``."""
    category = event.category if event.category is not None else DEFAULT_CATEGORY[event.type]
    return {
        "call_id": _uuid_or_none(event.call_id),
        "category": category,
        "level": event.level,
        "message": event.message,
        "data": dict(event.data),
        "seq": event.seq,
        "created_at": iso_ms(event.occurred_at),
        "metadata": dict(metadata),
    }


@dataclass(frozen=True, slots=True)
class BatchFacts:
    """The ``batches`` row values a batch status body reports."""

    batch_id: uuid.UUID
    calls_total: int
    calls_dispatched: int
    calls_completed: int
    calls_successful: int
    calls_failed: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
    failure_code: str | None = None
    failure_reason: str | None = None


def _data_text(event: DomainEvent, key: str) -> str | None:
    value = event.data.get(key)
    return value if isinstance(value, str) else None


def batch_body(event: DomainEvent, batch: BatchFacts) -> Body:
    """The §6 batch status body of a ``batch.*`` event (counts and timestamps from the row)."""
    body: Body = {
        "batch_id": str(batch.batch_id),
        "status": event.type.value.removeprefix(_BATCH_PREFIX),
        "phase": event.type.value,
        "calls_total": batch.calls_total,
        "calls_dispatched": batch.calls_dispatched,
        "calls_completed": batch.calls_completed,
        "calls_successful": batch.calls_successful,
        "calls_failed": batch.calls_failed,
        "created_at": iso_ms(batch.created_at),
        "updated_at": iso_ms(batch.updated_at),
    }
    if event.type in _TERMINAL_SUCCESS:
        body["completed_at"] = iso_ms(batch.completed_at or event.occurred_at)
    elif event.type is EventType.BATCH_FAILED:
        body["code"] = batch.failure_code or _data_text(event, "code") or _DEFAULT_FAILURE_CODE
        body["reason"] = batch.failure_reason or _data_text(event, "reason") or ""
    return body


def content_body(
    event: DomainEvent, *, metadata: Mapping[str, JsonValue] | None, batch: BatchFacts | None
) -> Body:
    """What a webhook reports about ``event`` before any envelope.

    The Call Object for ``call.completed``, the §5 body for other call events, the §6 body for ``batch.*``
    events whose batch row is known, else the event's ``data``.
    """
    if event.type is EventType.CALL_COMPLETED:
        return dict(event.data)
    if event.call_id is not None:
        return live_body(event, metadata or {})
    if batch is not None and event.type.value.startswith(_BATCH_PREFIX):
        return batch_body(event, batch)
    return dict(event.data)


def envelope_body(event: DomainEvent, data: Body) -> Body:
    """The §7 envelope around ``data``."""
    return {
        "id": event.id,
        "type": event.type.value,
        "created_at": iso_ms(event.occurred_at),
        "org_id": str(event.org_id),
        "call_id": _uuid_or_none(event.call_id),
        "batch_id": _uuid_or_none(event.batch_id),
        "conversation_id": _uuid_or_none(event.conversation_id),
        "data": data,
    }


def _strip_pathway_log(entry: JsonValue) -> JsonValue:
    if not isinstance(entry, dict):
        return entry
    return {key: value for key, value in entry.items() if key not in _PATHWAY_LOG_TEXT_KEYS}


def strip_phi(event_type: EventType, content: Body) -> Body:
    """``content`` (a :func:`content_body` result) without transcript text; the input is not modified.

    Call Object: ``transcripts`` → ``[]``, ``concatenated_transcript`` → ``""``, ``pathway_logs`` entries lose
    ``text`` and ``user_text``. ``call.turn`` / ``call.post_transfer_transcript`` live bodies lose
    ``data.text``. Other bodies are returned as they are.
    """
    if event_type is EventType.CALL_COMPLETED:
        stripped = dict(content)
        if "transcripts" in stripped:
            stripped["transcripts"] = []
        if "concatenated_transcript" in stripped:
            stripped["concatenated_transcript"] = ""
        logs = stripped.get("pathway_logs")
        if isinstance(logs, list):
            stripped["pathway_logs"] = [_strip_pathway_log(entry) for entry in logs]
        return stripped
    if event_type in _TEXT_LIVE_TYPES:
        data = content.get("data")
        if isinstance(data, dict) and "text" in data:
            return {**content, "data": {key: value for key, value in data.items() if key != "text"}}
    return content
