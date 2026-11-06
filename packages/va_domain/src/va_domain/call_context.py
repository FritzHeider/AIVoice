"""`CallContext`: the per-call JSON the media worker runs a call from (specs/01-architecture.md §2.1 step 2).

``prefetch_call_context`` stores it in Redis ``call:{<id>}:ctx`` (hash-tag form, like every per-call key in
:mod:`va_domain.carrier`) with a TTL of the maximum call duration plus one hour. ``config`` is the resolved
:class:`~va_domain.calls.config.SendCallConfig`. ``variables``, ``context_sections`` and ``turn_sections`` are
filled by the pre-call dynamic data of ``prefetch_call_context`` (specs/13 §7 Decisions P08-T03a and
P08-T03b: the variables set, and one rendered context line per variable that has one; the lines of the
``timing: "pre_call"`` entries are ``context_sections``, those of each ``timing: "every_turn"`` entry sit
under its ``dynamic_data`` index in ``turn_sections``, where the call's per-turn refresh replaces them);
``memory`` is filled by the contact's memory (specs/20 §2.3) and stays empty until its producer exists.
``opener_text`` is the templated ``first_sentence`` rendered once at prefetch (specs/07 §11 step 5, Decision
P05-T10d): the media worker speaks it verbatim, so it matches the phrase cache entry rendered for it.

A reader ignores top-level keys it does not know (specs/13 §7 Decision P08-T03b item 1): the worker that
writes a context and the media worker that reads it are not swapped at the same instant in a rolling update,
and a context refused for a field the reader does not use yet would be rebuilt from the ``calls`` row without
its pre-call data. The nested models and ``config`` stay strict, and a change of meaning bumps ``version``.
"""

from __future__ import annotations

import math
from typing import Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue

from va_domain.calls.config import SendCallConfig

__all__ = [
    "CALL_CONTEXT_EXTRA_TTL_S",
    "CALL_CONTEXT_VERSION",
    "MEMORY_RECENT_MESSAGES",
    "CallContext",
    "CallMemory",
    "MemoryFact",
    "MemoryMessage",
    "MemoryOpenItem",
    "call_context_key",
    "call_context_ttl_s",
]

CALL_CONTEXT_VERSION: Final = 1
CALL_CONTEXT_EXTRA_TTL_S: Final = 3600
MEMORY_RECENT_MESSAGES: Final = 10

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_TOLERANT = ConfigDict(frozen=True, extra="ignore")
"""The top-level context: fields a newer writer added are skipped, never a reason to refuse it."""


class MemoryFact(BaseModel):
    """One remembered fact (``Known facts: - {key}: {value}`` in the specs/20 §2.4 block)."""

    model_config = _FROZEN

    key: str
    value: JsonValue


class MemoryOpenItem(BaseModel):
    """An unresolved item from earlier conversations."""

    model_config = _FROZEN

    id: str
    text: str
    opened_at: AwareDatetime


class MemoryMessage(BaseModel):
    """One message of the contact's recent interactions across channels."""

    model_config = _FROZEN

    role: Literal["user", "assistant"]
    channel: Literal["voice", "sms", "widget"]
    text: str
    at: AwareDatetime


class CallMemory(BaseModel):
    """Memory loaded at call start: summary, facts, open items, and the last 10 messages (specs/20 §2.3)."""

    model_config = _FROZEN

    memory_id: UUID
    summary: str | None = None
    facts: list[MemoryFact] = Field(default_factory=list)
    open_items: list[MemoryOpenItem] = Field(default_factory=list)
    recent_messages: list[MemoryMessage] = Field(default_factory=list, max_length=MEMORY_RECENT_MESSAGES)


class CallContext(BaseModel):
    """Everything the media worker needs to run the call without reading Postgres on the hot path."""

    model_config = _TOLERANT

    version: Literal[1] = CALL_CONTEXT_VERSION
    call_id: UUID
    org_id: UUID
    direction: Literal["outbound", "inbound", "web"]
    to_number: str
    from_number: str
    batch_id: UUID | None = None
    contact_id: UUID | None = None
    test_call: bool = False
    config: SendCallConfig
    variables: dict[str, JsonValue] = Field(default_factory=dict)
    context_sections: list[str] = Field(default_factory=list)
    turn_sections: dict[int, list[str]] = Field(default_factory=dict)
    """The context lines of each ``timing: "every_turn"`` dynamic data entry, by ``dynamic_data`` index."""
    memory: CallMemory | None = None
    trace_context: dict[str, str] = Field(default_factory=dict)
    created_at: AwareDatetime
    opener_text: str | None = None
    maintenance_message: str | None = None
    """Set for an inbound call accepted during a maintenance window: the message to play before hanging up."""

    def turn_lines(self) -> list[str]:
        """Every line of :attr:`turn_sections`, the entries in ``dynamic_data`` order."""
        return [line for index in sorted(self.turn_sections) for line in self.turn_sections[index]]

    def ttl_s(self) -> int:
        """Seconds the stored context lives: see :func:`call_context_ttl_s`."""
        return call_context_ttl_s(self.config.max_duration_min)


def call_context_key(call_id: UUID) -> str:
    """``call:{<id>}:ctx``: the Redis key of the stored :class:`CallContext`."""
    return f"call:{{{call_id}}}:ctx"


def call_context_ttl_s(max_duration_min: float) -> int:
    """``ceil(max_duration_min * 60) + 3600``: the call's longest life plus one hour."""
    return math.ceil(max_duration_min * 60) + CALL_CONTEXT_EXTRA_TTL_S
