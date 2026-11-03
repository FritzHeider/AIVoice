"""Pure checks of webhook bodies the e2e receiver got (specs/30 §5 Decision P06-T08; specs/19 §5, §7, §8).

Each check returns one message per failure (empty = all held), so a flow reports every problem at once.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping
from datetime import datetime
from typing import Any, Final

__all__ = [
    "ENVELOPE_KEYS",
    "LATENCY_MESSAGE",
    "LIVE_BODY_KEYS",
    "PRESIGNED_FIELDS",
    "call_object_mismatches",
    "envelope_failures",
    "iso_z_ms",
    "live_body_failures",
    "retry_gap_ok",
]

# specs/19 §5 (bland-live body of a non-final call event).
LIVE_BODY_KEYS: Final = frozenset(
    {"call_id", "category", "level", "message", "data", "seq", "created_at", "metadata"}
)
# specs/19 §7 (envelope of org endpoint deliveries).
ENVELOPE_KEYS: Final = frozenset(
    {"id", "type", "created_at", "org_id", "call_id", "batch_id", "conversation_id", "data"}
)
# The per-stage latency lines of a turn (specs/07 §20 Decision P04-T10).
LATENCY_MESSAGE: Final = re.compile(r"^(E2E|STT|LLM|TTS): \d+ms$")
# Call Object fields computed per read: each read presigns the recording (specs/06 §11 Decision P05-T14).
PRESIGNED_FIELDS: Final = ("recording_url", "recording_expiration")
_ISO_Z_MS: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
# First retry of a failed delivery: 10 s with ±10 % jitter (specs/19 §8), plus dispatcher poll and send time.
RETRY_BASE_S: Final = 10.0
RETRY_JITTER: Final = 0.1
RETRY_SLACK_S: Final = 15.0


def iso_z_ms(value: object) -> bool:
    """Whether ``value`` is an ISO-8601 UTC timestamp with milliseconds and a ``Z`` suffix."""
    if not isinstance(value, str) or _ISO_Z_MS.match(value) is None:
        return False
    datetime.fromisoformat(value)
    return True


def live_body_failures(
    body: Mapping[str, Any],
    *,
    call_id: str,
    categories: Collection[str],
    metadata: Mapping[str, Any],
) -> list[str]:
    """Why ``body`` is not a §5 live body of ``call_id`` in one of ``categories`` carrying ``metadata``."""
    failures: list[str] = []
    if set(body) != LIVE_BODY_KEYS:
        failures.append(f"keys {sorted(body)} != {sorted(LIVE_BODY_KEYS)}")
    if body.get("call_id") != call_id:
        failures.append(f"call_id {body.get('call_id')!r} != {call_id!r}")
    if body.get("category") not in categories:
        failures.append(f"category {body.get('category')!r} not in {sorted(categories)}")
    if body.get("metadata") != dict(metadata):
        failures.append(f"metadata {body.get('metadata')!r} != {dict(metadata)!r}")
    if not iso_z_ms(body.get("created_at")):
        failures.append(f"created_at {body.get('created_at')!r} is not ISO-8601 Z with milliseconds")
    if not isinstance(body.get("seq"), int):
        failures.append(f"seq {body.get('seq')!r} is not an integer")
    return failures


def envelope_failures(
    body: Mapping[str, Any], *, headers: Mapping[str, str], org_id: str, call_id: str
) -> list[str]:
    """Why ``body`` is not a §7 envelope about ``call_id`` of ``org_id`` matching the request ``headers``."""
    failures: list[str] = []
    if set(body) != ENVELOPE_KEYS:
        failures.append(f"keys {sorted(body)} != {sorted(ENVELOPE_KEYS)}")
    if body.get("id") != headers.get("x-va-event-id"):
        failures.append(f"id {body.get('id')!r} != header {headers.get('x-va-event-id')!r}")
    if body.get("type") != headers.get("x-va-event-type"):
        failures.append(f"type {body.get('type')!r} != header {headers.get('x-va-event-type')!r}")
    if body.get("org_id") != org_id:
        failures.append(f"org_id {body.get('org_id')!r} != {org_id!r}")
    if body.get("call_id") != call_id:
        failures.append(f"call_id {body.get('call_id')!r} != {call_id!r}")
    if not iso_z_ms(body.get("created_at")):
        failures.append(f"created_at {body.get('created_at')!r} is not ISO-8601 Z with milliseconds")
    return failures


def retry_gap_ok(
    first_at: float,
    second_at: float,
    *,
    base_s: float = RETRY_BASE_S,
    jitter: float = RETRY_JITTER,
    slack_s: float = RETRY_SLACK_S,
) -> bool:
    """Whether the gap between two attempts fits the first retry delay (jitter floor, delay + ``slack_s``)."""
    gap = second_at - first_at
    return base_s * (1 - jitter) <= gap <= base_s + slack_s + 1e-9


def call_object_mismatches(
    webhook: Mapping[str, Any], api: Mapping[str, Any], *, ignore: Iterable[str] = PRESIGNED_FIELDS
) -> list[str]:
    """Keys whose values differ between a webhook Call Object and ``GET /v1/calls/{id}``, minus ``ignore``."""
    skipped = set(ignore)
    keys = (set(webhook) | set(api)) - skipped
    return sorted(key for key in keys if webhook.get(key, _MISSING) != api.get(key, _MISSING))


_MISSING: Final = object()
