"""Chat completion request parsing (specs/30 §3.4 Decision P03-T06a rules 2, 3).

Unknown OpenAI parameters are accepted (``extra="allow"``) so a newer SDK never fails against the simulator;
known parameters are validated like the OpenAI API, and a failure answers HTTP 400 with the offending field
path (``messages[0].role``) in ``error.param``.
"""

from __future__ import annotations

from typing import Annotated, Any, Final, Literal, Self

import orjson
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, ValidationError, model_validator
from pydantic_core import ErrorDetails
from starlette.responses import JSONResponse

from va_sim.llm.errors import openai_error

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ContentPart",
    "FunctionDef",
    "NamedToolChoice",
    "ToolDef",
    "forced_tool",
    "last_user_text",
    "message_text",
    "parse_chat",
    "system_text",
]

_TOOL_NAME_PATTERN: Final = r"^[a-zA-Z0-9_-]{1,64}$"
_SYSTEM_ROLES: Final = frozenset({"system", "developer"})
_BAD_JSON: Final = (
    "We could not parse the JSON body of your request. The OpenAI API expects a JSON payload, but what was "
    "sent was not valid JSON."
)


class _Open(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)


class ContentPart(_Open):
    """One content part; only ``text`` parts contribute text."""

    type: str
    text: str | None = None


class ToolCallFunction(_Open):
    name: str
    arguments: str


class MessageToolCall(_Open):
    id: str
    type: Literal["function"] = "function"
    function: ToolCallFunction


class ChatMessage(_Open):
    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str | list[ContentPart] | None = None
    name: str | None = None
    tool_calls: list[MessageToolCall] | None = None
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def _content_rules(self) -> Self:
        if self.content is None and self.role != "assistant":
            msg = f"a {self.role} message needs content"
            raise ValueError(msg)
        if self.role == "assistant" and self.content is None and not self.tool_calls:
            msg = "an assistant message needs content or tool_calls"
            raise ValueError(msg)
        if self.role == "tool" and not self.tool_call_id:
            msg = "a tool message needs tool_call_id"
            raise ValueError(msg)
        return self


class FunctionDef(_Open):
    name: Annotated[str, Field(pattern=_TOOL_NAME_PATTERN)]
    description: str | None = None
    parameters: dict[str, Any] | None = None
    strict: bool | None = None


class ToolDef(_Open):
    type: Literal["function"]
    function: FunctionDef


class NamedFunction(_Open):
    name: str


class NamedToolChoice(_Open):
    type: Literal["function"]
    function: NamedFunction


class StreamOptions(_Open):
    include_usage: bool = False


class ResponseFormat(_Open):
    type: Literal["text", "json_object", "json_schema"]
    json_schema: dict[str, Any] | None = None


class ChatRequest(_Open):
    model: Annotated[str, Field(min_length=1)]
    messages: Annotated[list[ChatMessage], Field(min_length=1)]
    stream: bool = False
    stream_options: StreamOptions | None = None
    tools: list[ToolDef] | None = None
    tool_choice: Literal["none", "auto", "required"] | NamedToolChoice | None = None
    temperature: Annotated[float, Field(ge=0, le=2)] | None = None
    max_tokens: PositiveInt | None = None
    max_completion_tokens: PositiveInt | None = None
    response_format: ResponseFormat | None = None
    n: int | None = None

    @property
    def include_usage(self) -> bool:
        return self.stream_options is not None and self.stream_options.include_usage

    @property
    def token_limit(self) -> int | None:
        """``max_completion_tokens`` (the current name), else the legacy ``max_tokens``."""
        return self.max_completion_tokens if self.max_completion_tokens is not None else self.max_tokens

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool.function.name for tool in self.tools or ())


def message_text(message: ChatMessage) -> str:
    """The text of ``message``: a string content, or its text parts joined by a space."""
    content = message.content
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return " ".join(part.text for part in content if part.type == "text" and part.text is not None)


def system_text(request: ChatRequest) -> str:
    """Every system and developer message, joined by newlines."""
    return "\n".join(message_text(message) for message in request.messages if message.role in _SYSTEM_ROLES)


def last_user_text(request: ChatRequest) -> str | None:
    """The text of the last user message, or None without one."""
    for message in reversed(request.messages):
        if message.role == "user":
            return message_text(message)
    return None


def forced_tool(request: ChatRequest) -> str | None:
    """The function a named ``tool_choice`` forces (``required`` names none)."""
    choice = request.tool_choice
    return choice.function.name if isinstance(choice, NamedToolChoice) else None


def _invalid(message: str, param: str | None, code: str | None = None) -> JSONResponse:
    return openai_error(400, message, param=param, code=code)


def _indexes(node: Any, part: int | str) -> bool:
    """Whether ``part`` is a key of the dict ``node`` or an index of the list ``node``."""
    if isinstance(node, dict):
        return isinstance(part, str) and part in node
    return isinstance(node, list) and isinstance(part, int) and 0 <= part < len(node)


def _param_of(payload: Any, loc: tuple[int | str, ...], *, missing: bool) -> str:
    """The path of ``loc`` inside ``payload`` (``messages[0].role``), dropping pydantic's union branch tags:
    a part counts only while it indexes into the payload, except a missing key at the end."""
    parts: list[int | str] = []
    node = payload
    for position, part in enumerate(loc):
        if _indexes(node, part):
            node = node[part]
        elif missing and isinstance(node, dict) and isinstance(part, str) and position == len(loc) - 1:
            node = None
        else:
            break
        parts.append(part)
    path = ""
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else part)
    return path


def _validation_error(payload: Any, error: ErrorDetails) -> JSONResponse:
    missing = error["type"] == "missing"
    param = _param_of(payload, tuple(error["loc"]), missing=missing)
    if missing:
        return _invalid(f"Missing required parameter: '{param}'.", param, "missing_required_parameter")
    message = error["msg"].removeprefix("Value error, ")
    return _invalid(f"Invalid value for '{param}': {message}", param or None, "invalid_value")


def _semantic_error(request: ChatRequest) -> JSONResponse | None:
    if request.n is not None and request.n != 1:
        return _invalid("Invalid value for 'n': the simulator only supports n = 1.", "n", "invalid_value")
    choice = request.tool_choice
    if choice == "required" or isinstance(choice, NamedToolChoice):
        if not request.tools:
            return _invalid(
                "Invalid value for 'tool_choice': 'tool_choice' is only allowed when 'tools' are specified.",
                "tool_choice",
            )
        name = forced_tool(request)
        if name is not None and name not in request.tool_names:
            return _invalid(
                f"Invalid value for 'tool_choice': function '{name}' is not one of the provided tools.",
                "tool_choice",
                "invalid_value",
            )
    return None


def parse_chat(raw: bytes) -> ChatRequest | JSONResponse:
    """``raw`` as a validated :class:`ChatRequest`, or the OpenAI 400 response."""
    try:
        payload = orjson.loads(raw)
    except orjson.JSONDecodeError:
        return _invalid(_BAD_JSON, None)
    if not isinstance(payload, dict):
        return _invalid(_BAD_JSON, None)
    try:
        request = ChatRequest.model_validate(payload)
    except ValidationError as exc:
        return _validation_error(payload, exc.errors(include_url=False)[0])
    semantic = _semantic_error(request)
    return semantic if semantic is not None else request
