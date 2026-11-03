"""`EngineState`: the serialized conversation state (specs/11-pathways-engine.md §9).

The state is written only with :func:`dump_engine_state` after every turn and read back with
:func:`load_engine_state`. The JSON carries a ``schema_version``: the reader upgrades older documents step by
step (one upgrader per version in ``_UPGRADERS``) and rejects a missing, non-integer, too old, or too new
version with :class:`EngineStateError`; unknown fields still raise :class:`pydantic.ValidationError`. Adding a
field means bumping :data:`CURRENT_SCHEMA_VERSION`, adding one upgrader, and committing a frozen
``packages/va_engine/tests/fixtures/engine_state/vN.json`` that must keep loading forever (specs/11 §9
Decision P07-T03).

:meth:`EngineState.add_turn` keeps ``history`` at :data:`HISTORY_CAP` turns: the oldest turns move into
``history_summary`` as ``role: content`` lines, and the summary keeps only its last
:data:`HISTORY_SUMMARY_MAX_CHARS` characters (specs/11 §9 Decision P05-T09a). :meth:`EngineState.take_pending`
hands the executor the pending actions that are due.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated, Final, Literal
from uuid import UUID

import orjson
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, JsonValue

__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "HISTORY_CAP",
    "HISTORY_SUMMARY_MAX_CHARS",
    "TERMINAL_ACTION_KINDS",
    "EngineState",
    "EngineStateError",
    "NodeFrame",
    "PendingAction",
    "PendingDtmf",
    "PendingEndCall",
    "PendingHumanTakeover",
    "PendingSay",
    "PendingSetVariables",
    "PendingSms",
    "PendingSwitchGraph",
    "PendingTransfer",
    "TurnRecord",
    "dump_engine_state",
    "load_engine_state",
]

CURRENT_SCHEMA_VERSION: Final = 2
HISTORY_CAP: Final = 200
HISTORY_SUMMARY_MAX_CHARS: Final = 4000
TERMINAL_ACTION_KINDS: Final = frozenset({"transfer", "end_call"})

_STRICT = ConfigDict(extra="forbid")
_ACTION = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


StateProblem = Literal["invalid_json", "not_object", "no_version", "unsupported_version", "newer_field"]


class EngineStateError(ValueError):
    """A stored state that is not an object or carries an unsupported ``schema_version``."""

    def __init__(self, problem: StateProblem, *, version: object = None, field: str | None = None) -> None:
        messages: dict[StateProblem, str] = {
            "invalid_json": "engine state is not valid JSON",
            "not_object": "engine state is not a JSON object",
            "no_version": "engine state has no schema_version",
            "unsupported_version": f"unsupported engine state schema_version {version!r}",
            "newer_field": f"v{version} engine state carries the newer field {field!r}",
        }
        super().__init__(messages[problem])
        self.problem = problem
        self.version = version


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


class PendingSay(BaseModel):
    """A line to speak (or send) that was decided but not yet delivered."""

    model_config = _ACTION

    kind: Literal["say"] = "say"
    after_mark: str | None = None
    text: str
    mark: str
    interruptible: bool | None = None


class PendingDtmf(BaseModel):
    """Keypad digits to send on the call."""

    model_config = _ACTION

    kind: Literal["send_dtmf"] = "send_dtmf"
    after_mark: str | None = None
    digits: str = Field(min_length=1)
    duration_ms: int = Field(default=250, ge=1)


class PendingTransfer(BaseModel):
    """A call transfer to run once the farewell played."""

    model_config = _ACTION

    kind: Literal["transfer"] = "transfer"
    after_mark: str | None = None
    mode: Literal["cold", "warm"] = "cold"
    target: str
    caller_id_mode: Literal["agent_number", "original_caller"] = "agent_number"


class PendingEndCall(BaseModel):
    """A hang-up to run once the final line played."""

    model_config = _ACTION

    kind: Literal["end_call"] = "end_call"
    after_mark: str | None = None
    reason: str
    ended_by: str = "agent"


class PendingSms(BaseModel):
    """A text message to send; serialized with the key ``from``."""

    model_config = _ACTION

    kind: Literal["send_sms"] = "send_sms"
    after_mark: str | None = None
    to: str
    from_: str = Field(serialization_alias="from", validation_alias=AliasChoices("from", "from_"))
    body: str


class PendingSetVariables(BaseModel):
    """Variables to merge into the state once the action runs."""

    model_config = _ACTION

    kind: Literal["set_variables"] = "set_variables"
    after_mark: str | None = None
    variables: dict[str, JsonValue]


class PendingSwitchGraph(BaseModel):
    """A switch to another pathway (Transfer Pathway node)."""

    model_config = _ACTION

    kind: Literal["switch_graph"] = "switch_graph"
    after_mark: str | None = None
    pathway_id: UUID
    version: int | None = None
    node_id: str | None = None


class PendingHumanTakeover(BaseModel):
    """Hand the conversation to a human operator."""

    model_config = _ACTION

    kind: Literal["human_takeover"] = "human_takeover"
    after_mark: str | None = None


PendingAction = Annotated[
    PendingSay
    | PendingDtmf
    | PendingTransfer
    | PendingEndCall
    | PendingSms
    | PendingSetVariables
    | PendingSwitchGraph
    | PendingHumanTakeover,
    Field(discriminator="kind"),
]


class EngineState(BaseModel):
    """Everything a runner needs to continue a conversation."""

    model_config = _STRICT

    schema_version: Literal[2] = 2
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
    pending_actions: list[PendingAction] = Field(default_factory=list)
    wait_armed_at_ms: int | None = None

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

    def take_pending(self, mark: str | None = None) -> list[PendingAction]:
        """Remove and return, in order, the actions with no ``after_mark`` or ``after_mark == mark``."""
        due: list[PendingAction] = []
        kept: list[PendingAction] = []
        for action in self.pending_actions:
            (due if action.after_mark is None or action.after_mark == mark else kept).append(action)
        self.pending_actions = kept
        return due


_V2_FIELDS: Final = ("pending_actions", "wait_armed_at_ms")


def _upgrade_v1(raw: dict[str, object]) -> dict[str, object]:
    present = [name for name in _V2_FIELDS if name in raw]
    if present:
        raise EngineStateError("newer_field", version=1, field=present[0])
    return {**raw, "schema_version": 2, "pending_actions": [], "wait_armed_at_ms": None}


_UPGRADERS: Final[Mapping[int, Callable[[dict[str, object]], dict[str, object]]]] = {1: _upgrade_v1}


def dump_engine_state(state: EngineState) -> str:
    """The canonical JSON of ``state``.

    The only writer for ``conversations.engine_state`` and ``call:{id}:engine``.
    """
    return state.model_dump_json(by_alias=True)


def _schema_version(raw: Mapping[str, object]) -> int:
    if "schema_version" not in raw:
        raise EngineStateError("no_version")
    version = raw["schema_version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or not 1 <= version <= CURRENT_SCHEMA_VERSION
    ):
        raise EngineStateError("unsupported_version", version=version)
    return version


def load_engine_state(data: str | bytes | Mapping[str, object]) -> EngineState:
    """Parse a stored state (JSON text or a decoded object), upgrading older schema versions.

    Raises :class:`EngineStateError` for invalid JSON, a non-object, or an unsupported ``schema_version``, and
    :class:`pydantic.ValidationError` for unknown or invalid fields. A mapping argument is never mutated.
    """
    if isinstance(data, Mapping):
        decoded: object = data
    else:
        try:
            decoded = orjson.loads(data)
        except orjson.JSONDecodeError as error:
            raise EngineStateError("invalid_json") from error
    if not isinstance(decoded, Mapping):
        raise EngineStateError("not_object")
    raw: dict[str, object] = dict(decoded)
    for version in range(_schema_version(raw), CURRENT_SCHEMA_VERSION):
        raw = _UPGRADERS[version](raw)
    return EngineState.model_validate(raw)
