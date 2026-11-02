"""Built-in system tools for the dialogue LLM (specs/10 §7, specs/13 §4/§6; Decision 2026-11-02 P05-T09b).

:func:`system_tools` turns :class:`SystemToolFacts` (what the call allows) into the tool definitions of a
turn; :func:`parse_system_call` validates a tool call the model made against the exact schema it was offered
and returns typed arguments, or an :class:`InvalidToolCall` whose :meth:`~InvalidToolCall.tool_message` goes
back to the model.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import orjson
from jsonschema import Draft202012Validator

from va_domain.call_context import CallContext
from va_domain.org import OrgSettings
from va_llm.models import ChatMessage, ToolCall, ToolDef

__all__ = [
    "DEFAULT_TRANSFER_LABEL",
    "RESERVED_TOOL_NAMES",
    "SYSTEM_TOOL_ORDER",
    "UNKNOWN_TOOL",
    "EndCallArgs",
    "InvalidToolCall",
    "LookupKnowledgeArgs",
    "PressButtonArgs",
    "ScheduleCallbackArgs",
    "SendSmsArgs",
    "SystemToolCall",
    "SystemToolFacts",
    "TransferCallArgs",
    "facts_for_call",
    "is_reserved",
    "parse_system_call",
    "resolve_transfer",
    "system_tools",
    "transfer_labels",
]

RESERVED_TOOL_NAMES = frozenset(
    {
        "end_call",
        "transfer_call",
        "press_button",
        "send_sms",
        "schedule_callback",
        "lookup_knowledge",
        "book_slot",
        "list_slots",
    }
)
SYSTEM_TOOL_ORDER = (
    "end_call",
    "transfer_call",
    "press_button",
    "send_sms",
    "schedule_callback",
    "lookup_knowledge",
)
DEFAULT_TRANSFER_LABEL = "default"
UNKNOWN_TOOL = "unknown_tool"
DIGITS_PATTERN = "^[0-9*#ABCDwW]{1,32}$"
SMS_MAX_LENGTH = 1600
_NOT_JSON = "arguments must be a JSON object"

type Runner = Literal["task", "pathway"]
type Channel = Literal["voice", "sms", "web_chat", "web_voice"]
type JsonSchema = dict[str, Any]


@dataclass(frozen=True, slots=True)
class SystemToolFacts:
    """What a call allows; ``sms_capable`` folds the agent number's SMS capability and messaging readiness."""

    runner: Runner
    channel: Channel = "voice"
    transfer_phone_number: str | None = None
    transfer_list: Mapping[str, str] = field(default_factory=dict)
    sms_capable: bool = False
    allow_send_sms: bool = True
    allow_schedule_callback: bool = False
    has_knowledge_bases: bool = False
    allow_end_call_tool: bool = True


@dataclass(frozen=True, slots=True)
class EndCallArgs:
    reason: str
    final_message: str | None = None


@dataclass(frozen=True, slots=True)
class TransferCallArgs:
    label: str | None = None
    message: str | None = None


@dataclass(frozen=True, slots=True)
class PressButtonArgs:
    digits: str


@dataclass(frozen=True, slots=True)
class SendSmsArgs:
    message: str


@dataclass(frozen=True, slots=True)
class ScheduleCallbackArgs:
    datetime_iso: str
    note: str | None = None


@dataclass(frozen=True, slots=True)
class LookupKnowledgeArgs:
    query: str


type SystemToolCall = (
    EndCallArgs
    | TransferCallArgs
    | PressButtonArgs
    | SendSmsArgs
    | ScheduleCallbackArgs
    | LookupKnowledgeArgs
)


@dataclass(frozen=True, slots=True)
class InvalidToolCall:
    """A system tool call the model must retry.

    ``detail`` is ``"unknown_tool"`` or a list of schema errors.
    """

    name: str
    detail: str | list[dict[str, str]]

    def tool_message(self, tool_call_id: str) -> ChatMessage:
        """The ``tool`` message ``{"error":"invalid_arguments","detail":…}`` answering the call."""
        body = orjson.dumps({"error": "invalid_arguments", "detail": self.detail}).decode()
        return ChatMessage(role="tool", content=body, tool_call_id=tool_call_id)


def is_reserved(name: str) -> bool:
    """Whether customer tools may not use ``name`` (specs/13 §4)."""
    return name in RESERVED_TOOL_NAMES


def facts_for_call(
    context: CallContext,
    settings: OrgSettings,
    *,
    sms_capable: bool,
    channel: Channel = "voice",
    allow_end_call_tool: bool = True,
) -> SystemToolFacts:
    """The facts of a call from its context and the org settings (pathway runners never get transfers)."""
    config = context.config
    return SystemToolFacts(
        runner="pathway" if config.runner_type == "pathway" else "task",
        channel=channel,
        transfer_phone_number=config.transfer_phone_number,
        transfer_list=dict(config.transfer_list),
        sms_capable=sms_capable,
        allow_send_sms=settings.tools.allow_send_sms,
        allow_schedule_callback=settings.tools.allow_schedule_callback or settings.engine.allow_callbacks,
        has_knowledge_bases=bool(config.knowledge_base_ids),
        allow_end_call_tool=allow_end_call_tool,
    )


def _string(description: str | None = None, **constraints: Any) -> JsonSchema:
    schema: JsonSchema = {"type": "string", **constraints}
    if description is not None:
        schema["description"] = description
    return schema


def _object(properties: Mapping[str, JsonSchema], *required: str) -> JsonSchema:
    return {"type": "object", "properties": dict(properties), "required": list(required)}


def transfer_labels(facts: SystemToolFacts) -> list[str]:
    """The ``label`` enum: sorted transfer list keys, plus ``default`` when a transfer number is set too."""
    labels = sorted(facts.transfer_list)
    if facts.transfer_phone_number and DEFAULT_TRANSFER_LABEL not in facts.transfer_list:
        labels.append(DEFAULT_TRANSFER_LABEL)
    return labels


def _transfer_schema(facts: SystemToolFacts) -> JsonSchema:
    message = _string("Short line to say before transferring")
    if not facts.transfer_list:
        return _object({"message": message})
    return _object({"label": _string(enum=transfer_labels(facts)), "message": message}, "label")


def _end_call() -> ToolDef:
    return ToolDef(
        name="end_call",
        description=(
            "End the call. Use it when the conversation is finished or the person asks to hang up. "
            "Put the last sentence to say in final_message."
        ),
        parameters=_object({"reason": _string(), "final_message": _string()}, "reason"),
    )


def _transfer_call(facts: SystemToolFacts) -> ToolDef:
    return ToolDef(
        name="transfer_call",
        description=(
            "Transfer the caller to a person or department. Use it when the caller asks for someone else or "
            "when your instructions say to transfer."
        ),
        parameters=_transfer_schema(facts),
    )


def _press_button() -> ToolDef:
    return ToolDef(
        name="press_button",
        description=(
            "Press keypad buttons to navigate a phone menu. Send the digits to press; w waits half a second, "
            "W waits one second."
        ),
        parameters=_object({"digits": _string(pattern=DIGITS_PATTERN)}, "digits"),
    )


def _send_sms() -> ToolDef:
    return ToolDef(
        name="send_sms",
        description="Send a text message to the person you are speaking with.",
        parameters=_object({"message": _string(maxLength=SMS_MAX_LENGTH)}, "message"),
    )


def _schedule_callback() -> ToolDef:
    return ToolDef(
        name="schedule_callback",
        description=(
            "Schedule a call back to this person. datetime_iso is the agreed time in ISO 8601, in the "
            "person's local time."
        ),
        parameters=_object({"datetime_iso": _string(), "note": _string()}, "datetime_iso"),
    )


def _lookup_knowledge() -> ToolDef:
    return ToolDef(
        name="lookup_knowledge",
        description="Search the reference information for facts that answer the person's question.",
        parameters=_object({"query": _string()}, "query"),
    )


def _end_call_allowed(facts: SystemToolFacts) -> bool:
    return facts.runner == "task" or facts.allow_end_call_tool


def _transfer_allowed(facts: SystemToolFacts) -> bool:
    has_target = bool(facts.transfer_phone_number or facts.transfer_list)
    return facts.runner == "task" and facts.channel == "voice" and has_target


_AVAILABILITY: tuple[tuple[Callable[[SystemToolFacts], bool], Callable[[SystemToolFacts], ToolDef]], ...] = (
    (_end_call_allowed, lambda _: _end_call()),
    (_transfer_allowed, _transfer_call),
    (lambda facts: facts.channel == "voice", lambda _: _press_button()),
    (lambda facts: facts.sms_capable and facts.allow_send_sms, lambda _: _send_sms()),
    (lambda facts: facts.allow_schedule_callback, lambda _: _schedule_callback()),
    (lambda facts: facts.has_knowledge_bases, lambda _: _lookup_knowledge()),
)


def _offered(facts: SystemToolFacts) -> dict[str, ToolDef]:
    tools = (build(facts) for allowed, build in _AVAILABILITY if allowed(facts))
    return {tool.name: tool for tool in tools}


def system_tools(facts: SystemToolFacts) -> tuple[ToolDef, ...]:
    """The system tools a turn offers, in :data:`SYSTEM_TOOL_ORDER`."""
    return tuple(_offered(facts).values())


def _schema_errors(schema: JsonSchema, arguments: object) -> list[dict[str, str]]:
    errors = [
        {"path": ".".join(str(part) for part in error.absolute_path), "message": error.message}
        for error in Draft202012Validator(schema).iter_errors(arguments)
    ]
    return sorted(errors, key=lambda item: (item["path"], item["message"]))


def _typed(name: str, arguments: dict[str, Any]) -> SystemToolCall:
    match name:
        case "end_call":
            return EndCallArgs(reason=arguments["reason"], final_message=arguments.get("final_message"))
        case "transfer_call":
            return TransferCallArgs(label=arguments.get("label"), message=arguments.get("message"))
        case "press_button":
            return PressButtonArgs(digits=arguments["digits"])
        case "send_sms":
            return SendSmsArgs(message=arguments["message"])
        case "schedule_callback":
            return ScheduleCallbackArgs(datetime_iso=arguments["datetime_iso"], note=arguments.get("note"))
        case _:
            return LookupKnowledgeArgs(query=arguments["query"])


def parse_system_call(call: ToolCall, facts: SystemToolFacts) -> SystemToolCall | InvalidToolCall | None:
    """Typed arguments of a system tool call; ``None`` when ``call.name`` is not a reserved name.

    A reserved name the turn did not offer answers ``unknown_tool``; bad JSON or a schema violation answers
    the sorted list of errors. Empty arguments count as ``{}``.
    """
    if not is_reserved(call.name):
        return None
    tool = _offered(facts).get(call.name)
    if tool is None:
        return InvalidToolCall(call.name, UNKNOWN_TOOL)
    try:
        arguments: object = orjson.loads(call.arguments_json) if call.arguments_json.strip() else {}
    except orjson.JSONDecodeError:
        return InvalidToolCall(call.name, [{"path": "", "message": _NOT_JSON}])
    errors = _schema_errors(tool.parameters, arguments)
    if errors:
        return InvalidToolCall(call.name, errors)
    return _typed(call.name, cast("dict[str, Any]", arguments))


def resolve_transfer(args: TransferCallArgs, facts: SystemToolFacts) -> str | None:
    """The number to transfer to: a transfer list entry, else the transfer number for ``default``/no label."""
    if args.label is not None and args.label in facts.transfer_list:
        return facts.transfer_list[args.label]
    if args.label in (None, DEFAULT_TRANSFER_LABEL):
        return facts.transfer_phone_number
    return None
