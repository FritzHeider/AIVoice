"""`EngineState`: the serialized conversation state (specs/11-pathways-engine.md §9).

The state is stored with ``model_dump_json()`` after every turn and read back with :func:`load_engine_state`,
which rejects an unknown ``schema_version`` and unknown fields. :meth:`EngineState.add_turn` keeps ``history``
at :data:`HISTORY_CAP` turns: the oldest turns move into ``history_summary`` as ``role: content`` lines, and
the summary keeps only its last :data:`HISTORY_SUMMARY_MAX_CHARS` characters (specs/11 §9 Decision P05-T09a).
"""

from __future__ import annotations

from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

__all__ = [
    "HISTORY_CAP",
    "HISTORY_SUMMARY_MAX_CHARS",
    "EngineState",
    "NodeFrame",
    "TurnRecord",
    "load_engine_state",
]

HISTORY_CAP: Final = 200
HISTORY_SUMMARY_MAX_CHARS: Final = 4000

_STRICT = ConfigDict(extra="forbid")


class NodeFrame(BaseModel):
    """A return address on the transfer-pathway stack."""

    model_config = _STRICT

    pathway_id: UUID | None
    version: int | None
    node_id: str


class TurnRecord(BaseModel):
    """One message of the conversation history."""

    model_config = _STRICT

    role: Literal["user", "assistant", "tool", "system"]
    content: str
    node_id: str | None
    interrupted: bool = False
    tool_call_id: str | None = None
    tool_name: str | None = None
    at_ms: int

    def summary_line(self) -> str:
        """``role: content``: how the turn reads once it is folded into ``history_summary``."""
        return f"{self.role}: {self.content}"


class EngineState(BaseModel):
    """Everything a runner needs to continue a conversation."""

    model_config = _STRICT

    schema_version: Literal[1] = 1
    runner: Literal["task", "pathway"]
    pathway_id: UUID | None
    pathway_version: int | None
    current_node_id: str
    return_stack: list[NodeFrame] = Field(default_factory=list)
    visit_counts: dict[str, int] = Field(default_factory=dict)
    variables: dict[str, JsonValue] = Field(default_factory=dict)
    history: list[TurnRecord] = Field(default_factory=list)
    history_summary: str = ""
    wait_reprompts: int = 0
    turn_index: int = 0
    ended: bool = False
    end_reason: str | None = None
    graph_stack_versions: dict[str, int] = Field(default_factory=dict)

    def add_turn(self, record: TurnRecord) -> None:
        """Append ``record``; past :data:`HISTORY_CAP` turns the oldest fold into ``history_summary``."""
        self.history.append(record)
        overflow = len(self.history) - HISTORY_CAP
        if overflow <= 0:
            return
        dropped = self.history[:overflow]
        del self.history[:overflow]
        lines = [self.history_summary] if self.history_summary else []
        lines.extend(turn.summary_line() for turn in dropped)
        self.history_summary = "\n".join(lines)[-HISTORY_SUMMARY_MAX_CHARS:]


def load_engine_state(data: str | bytes) -> EngineState:
    """Parse a stored state; raises :class:`pydantic.ValidationError` for an unknown schema or field."""
    return EngineState.model_validate_json(data)
