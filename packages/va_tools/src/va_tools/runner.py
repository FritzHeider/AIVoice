"""Tool execution (specs/13-tools-and-dynamic-data.md §5 steps 1, 3-8 and 10).

Rules: specs/13 §5 Decision P08-T02a. :meth:`ToolRunner.run` never raises:

1. The arguments (JSON text or a mapping; blank text = ``{}``) must be a JSON object valid against the tool's
   ``input_schema`` (Draft 2020-12). Otherwise the tool message is ``{"error":"invalid_arguments","details":
   [{path, message}]}`` (at most 10 details, sorted by path) and nothing is sent.
2. The request is rendered (:mod:`va_tools.render`) with the arguments under ``input`` (``{{input.date}}``) in
   front of the caller's scope, then sent through the :class:`~va_engine.ports.HttpPort` (SSRF guard,
   retries, size cap, ``response_map`` extraction). A template that does not parse is the outcome
   ``render_error``.
3. The tool message is ``{"status": code, "data": body, "extracted": {every response_map name}}`` for a 2xx
   answer (``data`` becomes an excerpt string when its compact JSON exceeds 4 000 characters) and
   ``{"status": code or 0, "error": kind}`` otherwise (``http_error``, ``timeout``, ``connection``,
   ``blocked``, ``invalid_url``, ``render_error``).

:class:`ToolInvocation` carries the ``call.tool`` event data (masked headers, URL without query, a ≤ 2 000
character response excerpt) and the variables to merge (non-null extractions of a 2xx answer only).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Final, Literal

import orjson
from jsonschema import Draft202012Validator, ValidationError
from jsonschema.exceptions import SchemaError
from pydantic import JsonValue
from referencing.exceptions import Unresolvable

from va_common.json import dumps_str
from va_common.time import Clock
from va_engine.handlers.reports import redacted_url
from va_engine.ports import HttpOutcome, HttpOutcomeKind, HttpPort
from va_engine.task_tools import tool_result
from va_engine.templating import TemplateScope
from va_tools.models import ToolSpec
from va_tools.render import ToolRenderError, excerpt, render_request

__all__ = [
    "DATA_LIMIT",
    "MAX_ARGUMENT_ERRORS",
    "ToolInvocation",
    "ToolOutcome",
    "ToolRunner",
    "argument_errors",
    "failure_detail",
]

DATA_LIMIT: Final = 4_000
MAX_ARGUMENT_ERRORS: Final = 10
_MESSAGE_LIMIT: Final = 300
_NOT_AN_OBJECT: Final = "arguments are not a JSON object"

type ToolOutcome = HttpOutcomeKind | Literal["invalid_arguments", "render_error"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolInvocation:
    """What running one tool call produced."""

    tool_name: str
    tool_id: str | None
    outcome: ToolOutcome
    content: str
    """The ``tool`` message answering the call."""
    status_code: int | None = None
    request: dict[str, JsonValue] | None = None
    """``{url, method, headers (masked), body}`` of the sent request (full URL); ``None`` when none was."""
    response: JsonValue = None
    extracted: dict[str, JsonValue] = field(default_factory=dict)
    """Every ``response_map`` name with its value (``None`` = no match)."""
    details: tuple[dict[str, JsonValue], ...] = ()
    attempts: int = 0
    duration_ms: int = 0
    truncated: bool = False

    @property
    def failed(self) -> bool:
        """Whether the call is a ``TOOL_FAILED`` (no 2xx answer; invalid arguments are the model's to fix)."""
        return self.outcome not in {"ok", "invalid_arguments"}

    @property
    def variables(self) -> dict[str, JsonValue]:
        """The non-null extractions of a 2xx answer (merged into the conversation's variables)."""
        if self.outcome != "ok":
            return {}
        return {name: value for name, value in self.extracted.items() if value is not None}

    def event_data(self) -> dict[str, JsonValue]:
        """The ``call.tool`` event data (specs/19 §2): no query string, no secret header value."""
        request: JsonValue = None
        if self.request is not None:
            request = {**self.request, "url": redacted_url(str(self.request["url"]))}
        return {
            "tool_name": self.tool_name,
            "tool_id": self.tool_id,
            "outcome": self.outcome,
            "request": request,
            "status_code": self.status_code,
            "response_excerpt": excerpt(self.response),
            "duration_ms": self.duration_ms,
            "attempts": self.attempts,
            "truncated": self.truncated,
            "extracted": dict(self.extracted),
        }


def failure_detail(invocation: ToolInvocation) -> str:
    """The ``call.error`` ``TOOL_FAILED`` detail: ``"<tool>: <kind>"`` (plus the status of an HTTP error)."""
    detail = f"{invocation.tool_name}: {invocation.outcome}"
    if invocation.status_code is not None:
        detail += f" ({invocation.status_code})"
    return detail


@lru_cache(maxsize=256)
def _validator(schema_json: str) -> Draft202012Validator | None:
    schema = orjson.loads(schema_json)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        return None
    return Draft202012Validator(schema)


def _path(error: ValidationError) -> str:
    text = ""
    for step in error.absolute_path:
        text += f"[{step}]" if isinstance(step, int) else (f".{step}" if text else str(step))
    return text


def argument_errors(schema: Mapping[str, JsonValue], arguments: object) -> list[dict[str, JsonValue]]:
    """Why ``arguments`` do not fit ``schema`` (empty when they do); an invalid schema accepts any object."""
    if not isinstance(arguments, dict):
        return [{"path": "", "message": _NOT_AN_OBJECT}]
    validator = _validator(dumps_str(dict(schema), sort_keys=True))
    if validator is None:
        return []
    try:
        errors = sorted(validator.iter_errors(arguments), key=lambda error: (_path(error), error.message))
    except Unresolvable:
        return []
    return [
        {"path": _path(error), "message": error.message[:_MESSAGE_LIMIT]}
        for error in errors[:MAX_ARGUMENT_ERRORS]
    ]


def _decoded(arguments: str | Mapping[str, JsonValue]) -> object:
    if not isinstance(arguments, str):
        return dict(arguments)
    if not arguments.strip():
        return {}
    try:
        value: object = orjson.loads(arguments)
    except orjson.JSONDecodeError:
        return None
    return value


def _capped(value: JsonValue, limit: int) -> JsonValue:
    """``value`` itself when its compact JSON fits ``limit`` characters, else an excerpt string."""
    if value is None or len(value if isinstance(value, str) else dumps_str(value)) <= limit:
        return value
    return excerpt(value, limit)


class ToolRunner:
    """Runs tool calls over ``http`` (see the module docstring)."""

    def __init__(self, http: HttpPort, *, clock: Clock) -> None:
        self._http = http
        self._clock = clock

    async def run(
        self, spec: ToolSpec, arguments: str | Mapping[str, JsonValue], scope: TemplateScope
    ) -> ToolInvocation:
        """Validate, render, send, and describe one call of ``spec``."""
        decoded = _decoded(arguments)
        details = argument_errors(spec.input_schema, decoded)
        if details or not isinstance(decoded, dict):
            content = tool_result({"error": "invalid_arguments", "details": details})
            return self._invocation(spec, "invalid_arguments", content, details=tuple(details))
        started = self._clock.monotonic()
        try:
            rendered = render_request(spec, dataclasses.replace(scope, input={"input": decoded}))
        except ToolRenderError:
            content = tool_result({"status": 0, "error": "render_error"})
            return self._invocation(spec, "render_error", content, duration_ms=self._since(started))
        call = rendered.call
        outcome = await self._http.execute(call)
        request: dict[str, JsonValue] = {
            "url": call.url,
            "method": call.method,
            "headers": dict(rendered.shown_headers),
            "body": _capped(call.body, DATA_LIMIT),
        }
        return self._answered(spec, outcome, request)

    def _since(self, started: float) -> int:
        return max(round((self._clock.monotonic() - started) * 1000), 0)

    def _answered(
        self, spec: ToolSpec, outcome: HttpOutcome, request: dict[str, JsonValue]
    ) -> ToolInvocation:
        extracted = {name: outcome.extracted.get(name) for name in spec.response_map}
        if outcome.succeeded:
            message: dict[str, JsonValue] = {
                "status": outcome.status,
                "data": _capped(outcome.body, DATA_LIMIT),
                "extracted": extracted,
            }
        else:
            message = {"status": outcome.status or 0, "error": outcome.kind}
        return self._invocation(
            spec,
            outcome.kind,
            tool_result(message),
            status_code=outcome.status,
            request=request,
            response=outcome.body,
            extracted=extracted,
            attempts=outcome.attempts,
            duration_ms=outcome.duration_ms,
            truncated=outcome.truncated,
        )

    @staticmethod
    def _invocation(  # noqa: PLR0913 - one keyword per invocation field keeps call sites explicit
        spec: ToolSpec,
        outcome: ToolOutcome,
        content: str,
        *,
        details: Sequence[dict[str, JsonValue]] = (),
        status_code: int | None = None,
        request: dict[str, JsonValue] | None = None,
        response: JsonValue = None,
        extracted: dict[str, JsonValue] | None = None,
        attempts: int = 0,
        duration_ms: int = 0,
        truncated: bool = False,
    ) -> ToolInvocation:
        return ToolInvocation(
            tool_name=spec.name,
            tool_id=spec.tool_id,
            outcome=outcome,
            content=content,
            status_code=status_code,
            request=request,
            response=response,
            extracted=extracted if extracted is not None else {},
            details=tuple(details),
            attempts=attempts,
            duration_ms=duration_ms,
            truncated=truncated,
        )
