"""Checks of a stored tool definition beyond its field shapes (specs/13-tools-and-dynamic-data.md §2, §4).

Rules: specs/13 §2 Decision P08-T01. :class:`va_domain.calls.tools.InlineTool` already validates the field
shapes (name pattern, lengths, templated ``http(s)`` URL, header-name tokens, body size, ``type: object``
schema, timeout and retries). :func:`check_tool_definition` adds the rules that need more than one field or
another package: reserved system tool names, ``https`` outside ``local``/``test``, a draft 2020-12 JSON
Schema, parsable JSONPath expressions and variable names in ``response``, and the secret headers. Each failure
is one ``INVALID_INPUT`` :class:`~va_common.errors.AppError` with the field path.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Final

from jsonschema import Draft202012Validator, SchemaError

from va_common.errors import AppError
from va_domain.calls.tools import HEADER_NAME, InlineTool
from va_engine.system_tools import RESERVED_TOOL_NAMES
from va_tools.jsonpath import InvalidJsonPathError, compile_path

__all__ = [
    "HTTPS_REQUIRED_MESSAGE",
    "MAX_SECRET_HEADER_LENGTH",
    "VARIABLE_NAME",
    "check_secret_headers",
    "check_tool_definition",
]

HTTPS_REQUIRED_MESSAGE: Final = "url must start with https://."
MAX_SECRET_HEADER_LENGTH: Final = 4096
VARIABLE_NAME: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_MAX_SCHEMA_MESSAGE: Final = 300


def _invalid(message: str, field: str) -> AppError:
    return AppError("INVALID_INPUT", message, field=field)


def check_tool_definition(
    tool: InlineTool, *, secret_headers: Mapping[str, str] | None = None, allow_http: bool
) -> None:
    """Raise ``INVALID_INPUT`` for the first rule ``tool`` (with ``secret_headers``) breaks."""
    if tool.name in RESERVED_TOOL_NAMES:
        message = f"'{tool.name}' is a reserved system tool name."
        raise _invalid(message, "name")
    if not allow_http and not tool.url.lower().startswith("https://"):
        raise _invalid(HTTPS_REQUIRED_MESSAGE, "url")
    _check_schema(tool)
    _check_response(tool.response)
    check_secret_headers(secret_headers or {}, plain_headers=tool.headers)


def _check_schema(tool: InlineTool) -> None:
    try:
        Draft202012Validator.check_schema(tool.input_schema)
    except SchemaError as exc:
        message = f"input_schema is not a valid JSON Schema (draft 2020-12): {exc.message}"
        raise _invalid(message[:_MAX_SCHEMA_MESSAGE], "input_schema") from None


def _check_response(response: Mapping[str, str]) -> None:
    for name, path in response.items():
        if not VARIABLE_NAME.match(name):
            message = f"response key {name[:64]!r} is not a variable name (letters, digits, _; max 64)."
            raise _invalid(message, "response")
        try:
            compile_path(path)
        except InvalidJsonPathError:
            message = f"response.{name} is not a valid JSONPath expression."
            raise _invalid(message, f"response.{name}") from None


def check_secret_headers(secret_headers: Mapping[str, str], *, plain_headers: Mapping[str, str]) -> None:
    """Token names, no clash with a plain header (case-insensitive), non-empty single-line values ≤ 4 096."""
    plain = {name.lower() for name in plain_headers}
    for name, value in secret_headers.items():
        if not HEADER_NAME.match(name):
            message = f"secret header name {name[:64]!r} is not an RFC 7230 token."
            raise _invalid(message, "secret_headers")
        problem = _value_problem(name, value, plain)
        if problem is not None:
            raise _invalid(problem, f"secret_headers.{name}")


def _value_problem(name: str, value: str, plain: set[str]) -> str | None:
    if name.lower() in plain:
        return f"Header {name} is set in both headers and secret_headers."
    if not value:
        return f"secret header {name} has an empty value."
    if len(value) > MAX_SECRET_HEADER_LENGTH:
        return f"secret header {name} is longer than {MAX_SECRET_HEADER_LENGTH} characters."
    if "\r" in value or "\n" in value:
        return f"secret header {name} contains a line break."
    return None
