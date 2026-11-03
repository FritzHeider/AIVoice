"""Redis keys of the event bus (specs/03-data-model.md §11; specs/19-events-and-webhooks.md §1).

Per-call keys use the hash-tag form ``call:{<uuid>}:...`` so every key of one call maps to the same cluster
slot. The streams are one key each (no tag).
"""

from __future__ import annotations

import uuid
from typing import Final

from va_events.catalog import Stream

__all__ = [
    "CALL_SEQ_TTL_S",
    "DEAD_STREAM_KEY",
    "DEAD_STREAM_MAXLEN",
    "STREAM_MAXLEN",
    "call_live_channel",
    "call_seq_key",
    "stream_key",
]

CALL_SEQ_TTL_S: Final = 172_800
STREAM_MAXLEN: Final = 1_000_000
DEAD_STREAM_KEY: Final = "events:dead"
"""Entries a consumer could not decode, map, or insert (stream, entry id, reason, raw payload)."""
DEAD_STREAM_MAXLEN: Final = 100_000


def call_seq_key(call_id: uuid.UUID) -> str:
    """``call:{<id>}:seq``: the per-call event sequence counter (48 h)."""
    return f"call:{{{call_id}}}:seq"


def call_live_channel(call_id: uuid.UUID) -> str:
    """``call:{<id>}:live``: the pub/sub channel of the call's live events."""
    return f"call:{{{call_id}}}:live"


def stream_key(stream: Stream) -> str:
    """``events:<stream>``: one of the five event bus streams."""
    return f"events:{stream}"
