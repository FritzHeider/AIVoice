"""Pathway node and edge schemas (specs/11-pathways-engine.md §3).

Attributes are snake_case; JSON keys are camelCase exactly as Bland stores them (a few keys Bland writes
in snake_case, such as ``modelOptions.block_interruptions`` and the ``forwardingNode`` object, keep their
spelling). Every model keeps unknown keys (``extra="allow"``), and :func:`dump_node` / :func:`dump_edge`
write back only the keys that were present (``exclude_unset``), so a Bland export round-trips without
losing or adding data.

``PathwayNode`` is a discriminated union on ``type``. Raw nodes are normalized before discrimination
(:func:`normalize_node_raw`): import aliases (``"Transfer Node"`` ...) become canonical type names and
``"Global"`` becomes ``"Default"`` with ``data.isGlobal = true`` (specs/11 §3.3; Decision P07-T01 under
specs/11 §11).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Final, Literal, get_args

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, JsonValue, TypeAdapter
from pydantic.alias_generators import to_camel

__all__ = [
    "NODE_ID_PATTERN",
    "NODE_TYPES",
    "NODE_TYPE_ALIASES",
    "WEBHOOK_OPERATORS",
    "CamelModel",
    "ConditionExample",
    "ConversationMessage",
    "CustomCodeConfig",
    "CustomCodeNode",
    "DefaultNode",
    "DialogueExample",
    "EdgeData",
    "EndCallConfig",
    "EndCallNode",
    "ExtractVar",
    "ForwardingTarget",
    "KnowledgeBaseNode",
    "ModelOptions",
    "NodeTag",
    "NodeType",
    "OnTargetNoAnswer",
    "PathwayEdge",
    "PathwayExample",
    "PathwayNode",
    "PathwayNodeData",
    "Position",
    "PressButtonConfig",
    "PressButtonNode",
    "ResponseData",
    "ResponsePathway",
    "RouteOnStatus",
    "SchedulingConfig",
    "SchedulingNode",
    "SmsConfig",
    "SmsNode",
    "TransferCallNode",
    "TransferConfig",
    "TransferPathwayConfig",
    "TransferPathwayNode",
    "WaitForResponseConfig",
    "WaitForResponseNode",
    "WarmTransfer",
    "WebhookConfig",
    "WebhookNode",
    "dump_edge",
    "dump_node",
    "effective_transfer_number",
    "id_list",
    "normalize_node_raw",
    "parse_edge",
    "parse_node",
]

NodeType = Literal[
    "Default",
    "Wait for Response",
    "Webhook",
    "Knowledge Base",
    "Transfer Call",
    "End Call",
    "SMS",
    "Custom Code",
    "Press Button",
    "Transfer Pathway",
    "Scheduling",
]
"""Stored node types; ``Global`` is accepted on input only and normalizes to ``Default`` (§3.3)."""

NODE_TYPES: Final[tuple[str, ...]] = get_args(NodeType)
NODE_TYPE_ALIASES: Final[Mapping[str, str]] = {
    "Transfer Node": "Transfer Call",
    "End Node": "End Call",
    "Knowledge Base Node": "Knowledge Base",
    "Webhook Node": "Webhook",
}
GLOBAL_TYPE: Final = "Global"
NODE_ID_PATTERN: Final = r"^[A-Za-z0-9_-]{1,64}$"

WebhookOperator = Literal[
    "==", "!=", ">", ">=", "<", "<=", "contains", "not_contains", "is_empty", "is_not_empty"
]
WEBHOOK_OPERATORS: Final[tuple[str, ...]] = get_args(WebhookOperator)

Number = int | float
"""Numbers keep the JSON type they arrived with (``320`` stays an int, ``0.5`` a float)."""

PathwayVersionRef = Literal["production", "staging"] | int


class CamelModel(BaseModel):
    """camelCase JSON, snake_case attributes, unknown keys kept."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
        extra="allow",
    )


class _SnakeModel(BaseModel):
    """Objects Bland writes with snake_case keys; unknown keys kept."""

    model_config = ConfigDict(extra="allow")


class ConversationMessage(CamelModel):
    """One message of a fine-tuning example conversation."""

    role: str
    content: str


class ExtractVar(CamelModel):
    """A variable the engine extracts from the conversation at a node (§5.3)."""

    name: str
    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str = ""
    required: bool = False


class PathwayExample(CamelModel):
    """A routing fine-tuning example."""

    conversation: list[ConversationMessage] = Field(default_factory=list)
    chosen_label: str = ""


class ConditionExample(CamelModel):
    """A loop-condition fine-tuning example."""

    conversation: list[ConversationMessage] = Field(default_factory=list)
    condition_met: bool = False
    explanation: str = ""


class DialogueExample(CamelModel):
    """A dialogue fine-tuning example."""

    conversation: list[ConversationMessage] = Field(default_factory=list)
    assistant_response: str = ""


class ModelOptions(CamelModel):
    """Per-node model options; ``None`` inherits global/call/org/platform values (§13)."""

    temperature: Number | None = Field(default=None, ge=0, le=1)
    interruption_threshold: int | None = Field(default=None, ge=0)
    skip_user_response: bool = False
    block_interruptions: bool = Field(default=False, alias="block_interruptions")
    resumption_speed: Literal[1, 2, 3] = Field(default=2, alias="resumption_speed")
    interruptibility: int | None = Field(default=None, ge=0, le=3)
    model: str | None = None


class NodeTag(CamelModel):
    """Label shown in pathway_logs and the dashboard."""

    name: str
    color: str | None = None


class ForwardingTarget(_SnakeModel):
    """Forwarding into another pathway (``forwardingNode`` object form)."""

    pathway_id: str
    version: PathwayVersionRef = "production"
    node_id: str | None = None


class ResponseData(CamelModel):
    """A JSONPath extraction of a webhook response into a variable."""

    name: str
    data: str
    context: str = ""


class ResponsePathway(CamelModel):
    """Conditional routing on an extracted webhook value (evaluated in order)."""

    variable: str
    operator: WebhookOperator
    value: JsonValue = None
    target_node_id: str


class RouteOnStatus(CamelModel):
    """Node to jump to per webhook outcome; ``None`` = normal routing."""

    status_2xx: str | None = Field(default=None, alias="2xx")
    status_4xx: str | None = Field(default=None, alias="4xx")
    status_5xx: str | None = Field(default=None, alias="5xx")
    timeout: str | None = None

    def targets(self) -> list[str]:
        """Every configured target node id, in key order."""
        values = (self.status_2xx, self.status_4xx, self.status_5xx, self.timeout)
        return [value for value in values if value]


class WebhookConfig(CamelModel):
    """Webhook node config (§3.2.1)."""

    url: str = ""
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    body: JsonValue = None
    timeout_ms: int = Field(default=10_000, ge=100, le=60_000)
    retries: int = Field(default=0, ge=0, le=3)
    speech: str | None = None
    response_data: list[ResponseData] = Field(default_factory=list)
    route_on_status: RouteOnStatus | None = None
    response_pathways: list[ResponsePathway] = Field(default_factory=list)


class OnTargetNoAnswer(CamelModel):
    """What a warm transfer does when the human agent does not answer."""

    action: Literal["return_to_agent", "end_call", "move_to_node"] = "return_to_agent"
    node_id: str | None = None


class WarmTransfer(CamelModel):
    """Warm transfer settings (§3.2.2 ``warm``)."""

    briefing_prompt: str | None = None
    hold_audio: Literal["hold_music", "continue_conversation", "silence"] = "hold_music"
    target_timeout_secs: int = Field(default=30, ge=1, le=600)
    on_target_no_answer: OnTargetNoAnswer = Field(default_factory=OnTargetNoAnswer)


class TransferConfig(CamelModel):
    """Transfer Call node config (§3.2.2)."""

    mode: Literal["cold", "warm"] = "cold"
    number: str | None = None
    speech: str | None = None
    speech_mode: Literal["static", "prompt"] = "static"
    speech_prompt: str | None = None
    caller_id_mode: Literal["agent_number", "original_caller"] = "agent_number"
    warm: WarmTransfer | None = None


class EndCallConfig(CamelModel):
    """End Call node config (§3.2.3)."""

    speech: str | None = None
    speech_mode: Literal["static", "prompt"] = "static"
    speech_prompt: str | None = None


class SmsConfig(CamelModel):
    """SMS node config (§3.2.4); ``from_`` ``None`` = the call's agent number."""

    to: str = "{{from}}"
    from_: str | None = Field(default=None, alias="from")
    message: str = ""
    message_mode: Literal["static", "prompt"] = "static"
    message_prompt: str | None = None
    speech: str | None = None
    continue_on_failure: bool = True


class PressButtonConfig(CamelModel):
    """Press Button node config (§3.2.5): ``w`` = 0.5 s pause, ``W`` = 1 s."""

    digits: str = Field(pattern=r"^[0-9*#ABCDwW]+$")
    speech: str | None = None
    duration_millis: int = Field(default=250, ge=50, le=5_000)


class TransferPathwayConfig(CamelModel):
    """Transfer Pathway node config (§3.2.6); ``node_id`` ``None`` = the target's start node."""

    pathway_id: str = ""
    version: PathwayVersionRef = "production"
    node_id: str | None = None
    carry_variables: bool = True
    return_on_end: bool = False


class SchedulingConfig(CamelModel):
    """Scheduling node config (§3.2.7)."""

    calendar_provider: Literal["webhook"] = "webhook"
    availability_url: str = ""
    booking_url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    timezone: str | None = None
    slot_duration_minutes: int = Field(default=30, ge=5, le=480)
    lookahead_days: int = Field(default=14, ge=1, le=90)
    speech: str | None = None
    confirmation_prompt: str | None = None
    on_failure_node_id: str | None = None


class CustomCodeConfig(CamelModel):
    """Custom Code node config (§3.2.8; runtime specs/13 §8, timeout default 1 000 ms, max 5 000)."""

    language: Literal["typescript", "javascript"] = "typescript"
    code: str = ""
    timeout_ms: int = Field(default=1_000, ge=1, le=5_000)
    network_allowlist: list[str] = Field(default_factory=list)
    speech: str | None = None


class WaitForResponseConfig(CamelModel):
    """Wait for Response node config (§3.2.9)."""

    wait_seconds: int = Field(default=30, ge=1, le=3_600)
    reprompt_text: str | None = None
    max_reprompts: int = Field(default=2, ge=0, le=10)
    on_timeout: Literal["end_call", "move_to_node", "repeat"] = "end_call"
    timeout_node_id: str | None = None


class PathwayNodeData(CamelModel):
    """Every ``data`` field of §3.1 with its default."""

    name: str = Field(min_length=1, max_length=120)
    is_start: bool = False
    is_global: bool = False
    global_label: str | None = None
    enable_global_auto_return: bool = True
    prompt: str | None = None
    text: str | None = None
    condition: str | None = None
    condition_threshold: int = Field(default=6, ge=0, le=10)
    extract_vars: list[ExtractVar] = Field(default_factory=list)
    extract_vars_mode: Literal["before_response", "background"] = "before_response"
    pathway_examples: list[PathwayExample] = Field(default_factory=list)
    condition_examples: list[ConditionExample] = Field(default_factory=list)
    dialogue_examples: list[DialogueExample] = Field(default_factory=list)
    model_options: ModelOptions = Field(default_factory=ModelOptions)
    enable_forwarding: bool = False
    forwarding_node: str | ForwardingTarget | None = None
    kb: str | list[str] | None = None
    kb_top_k: int = Field(default=5, ge=1, le=50)
    tools: list[str | dict[str, JsonValue]] = Field(default_factory=list)
    webhook: WebhookConfig | None = None
    transfer: TransferConfig | None = None
    transfer_number: str | None = None
    end_call: EndCallConfig | None = None
    sms: SmsConfig | None = None
    press_button: PressButtonConfig | None = None
    transfer_pathway: TransferPathwayConfig | None = None
    scheduling: SchedulingConfig | None = None
    custom_code: CustomCodeConfig | None = None
    wait_for_response: WaitForResponseConfig | None = None
    max_visits: int = Field(default=8, ge=1, le=100)
    tag: NodeTag | None = None

    def kb_ids(self) -> list[str]:
        """``kb`` as a list (a single id or an array)."""
        return id_list(self.kb)

    def tool_ids(self) -> list[str]:
        """The tool ids in ``tools`` (inline tool objects excluded)."""
        return [tool for tool in self.tools if isinstance(tool, str)]


def id_list(value: str | list[str] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


class Position(CamelModel):
    """Editor position of a node."""

    x: Number
    y: Number


class _NodeBase(CamelModel):
    """Fields shared by every node type."""

    id: str = Field(pattern=NODE_ID_PATTERN)
    type: str
    position: Position | None = None
    width: Number | None = None
    height: Number | None = None
    data: PathwayNodeData


class DefaultNode(_NodeBase):
    """A Default node (also every global node)."""

    type: Literal["Default"]


class WaitForResponseNode(_NodeBase):
    """A Wait for Response node."""

    type: Literal["Wait for Response"]


class WebhookNode(_NodeBase):
    """A Webhook node."""

    type: Literal["Webhook"]


class KnowledgeBaseNode(_NodeBase):
    """A Knowledge Base node."""

    type: Literal["Knowledge Base"]


class TransferCallNode(_NodeBase):
    """A Transfer Call node."""

    type: Literal["Transfer Call"]


class EndCallNode(_NodeBase):
    """An End Call node."""

    type: Literal["End Call"]


class SmsNode(_NodeBase):
    """An SMS node."""

    type: Literal["SMS"]


class CustomCodeNode(_NodeBase):
    """A Custom Code node."""

    type: Literal["Custom Code"]


class PressButtonNode(_NodeBase):
    """A Press Button node."""

    type: Literal["Press Button"]


class TransferPathwayNode(_NodeBase):
    """A Transfer Pathway node."""

    type: Literal["Transfer Pathway"]


class SchedulingNode(_NodeBase):
    """A Scheduling node."""

    type: Literal["Scheduling"]


def normalize_node_raw(raw: Any) -> Any:
    """Map import aliases to canonical types and ``Global`` to ``Default`` + ``isGlobal`` (§3.3).

    Anything that is not a mapping with a string ``type`` is returned unchanged for pydantic to reject.
    """
    if not isinstance(raw, Mapping):
        return raw
    node_type = raw.get("type")
    if not isinstance(node_type, str):
        return raw
    if node_type == GLOBAL_TYPE:
        normalized = {**raw, "type": "Default"}
        data = raw.get("data")
        if isinstance(data, Mapping):
            normalized["data"] = {**data, "isGlobal": True}
        return normalized
    canonical = NODE_TYPE_ALIASES.get(node_type)
    if canonical is None:
        return raw
    return {**raw, "type": canonical}


PathwayNode = Annotated[
    Annotated[
        DefaultNode
        | WaitForResponseNode
        | WebhookNode
        | KnowledgeBaseNode
        | TransferCallNode
        | EndCallNode
        | SmsNode
        | CustomCodeNode
        | PressButtonNode
        | TransferPathwayNode
        | SchedulingNode,
        Field(discriminator="type"),
    ],
    BeforeValidator(normalize_node_raw),
]
"""Any pathway node, discriminated on ``type`` after :func:`normalize_node_raw`."""


class EdgeData(CamelModel):
    """Edge routing data (§3.2)."""

    label: str = ""
    description: str = ""
    is_fallback: bool = False
    always_pick: bool = False
    priority: int = 0


class PathwayEdge(CamelModel):
    """A directed edge ``source → target``; ``type`` is editor-only."""

    id: str = Field(min_length=1, max_length=256)
    source: str
    target: str
    type: str | None = None
    data: EdgeData = Field(default_factory=EdgeData)


_NODE_ADAPTER: Final[TypeAdapter[PathwayNode]] = TypeAdapter(PathwayNode)


def parse_node(raw: object) -> PathwayNode:
    """Validate one raw node (raises ``pydantic.ValidationError``)."""
    return _NODE_ADAPTER.validate_python(raw)


def parse_edge(raw: object) -> PathwayEdge:
    """Validate one raw edge (raises ``pydantic.ValidationError``)."""
    return PathwayEdge.model_validate(raw)


def dump_node(node: PathwayNode) -> dict[str, Any]:
    """The node as stored JSON: only keys that were present (or set), camelCase, extras kept."""
    return node.model_dump(mode="json", by_alias=True, exclude_unset=True)


def dump_edge(edge: PathwayEdge) -> dict[str, Any]:
    """The edge as stored JSON (same rules as :func:`dump_node`)."""
    return edge.model_dump(mode="json", by_alias=True, exclude_unset=True)


def effective_transfer_number(data: PathwayNodeData) -> str | None:
    """``transfer.number``, falling back to the legacy ``transferNumber``; blank values count as absent."""
    for candidate in (data.transfer.number if data.transfer else None, data.transfer_number):
        if candidate and candidate.strip():
            return candidate
    return None
