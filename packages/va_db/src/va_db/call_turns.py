"""Transcript turn rows written by the media worker (specs/03-data-model.md ``call_turns``; specs/07 §8).

:class:`CallTurnRow` is one ``call_turns`` row as the media worker knows it at the end of a turn;
:class:`LatencyMerge` adds latency values to a stored row and :class:`TurnDelete` removes a superseded user
turn. :data:`TurnOp` is the union the write-behind writer queues, in the order the pipeline produced them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from va_db.models import CallTurnRole

__all__ = ["CallTurnRow", "LatencyMerge", "TurnDelete", "TurnOp"]


@dataclass(frozen=True, slots=True)
class CallTurnRow:
    """One turn; ``start_ms``/``end_ms`` are relative to the call's answer time."""

    org_id: uuid.UUID
    call_id: uuid.UUID
    turn_index: int
    role: CallTurnRole
    text: str
    start_ms: int
    end_ms: int
    text_spoken: str | None = None
    interrupted: bool = False
    node_id: str | None = None
    node_name: str | None = None
    language_code: str | None = None
    latency: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class LatencyMerge:
    """Merge ``latency`` into the stored row's ``latency`` (keys of ``latency`` win)."""

    org_id: uuid.UUID
    call_id: uuid.UUID
    turn_index: int
    role: CallTurnRole
    latency: dict[str, Any]


@dataclass(frozen=True, slots=True)
class TurnDelete:
    """Delete one row (a user turn superseded by a merged turn)."""

    org_id: uuid.UUID
    call_id: uuid.UUID
    turn_index: int
    role: CallTurnRole


type TurnOp = CallTurnRow | LatencyMerge | TurnDelete
