"""The runtime form of a tool (specs/13-tools-and-dynamic-data.md §2-§4).

Rules: specs/13 §5 Decision P08-T02a. A :class:`ToolSpec` is built from a stored ``tools`` row (with its
decrypted secret headers) or from a Send Call inline tool; :func:`function_def` is its specs/13 §4 function
definition. Secret header values never appear in the ``repr``; inline tools have no secret headers, but
every inline ``headers`` value counts as secret in logs and events (§4), which :meth:`ToolSpec.masked_names`
reports.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import JsonValue

from va_common.ids import PublicPrefix, to_public
from va_domain.calls.tools import InlineTool
from va_engine.ports import HttpMethod
from va_llm.models import ToolDef

if TYPE_CHECKING:
    from va_db.models import Tool

__all__ = ["ToolSpec", "function_def"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolSpec:
    """Everything needed to expose and run one tool; ``tool_id`` is ``TL-<uuid>`` (``None`` inline)."""

    tool_id: str | None
    name: str
    description: str
    speech: str | None
    url: str
    method: HttpMethod
    headers: Mapping[str, str] = field(default_factory=dict)
    secret_headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    query: Mapping[str, str] = field(default_factory=dict)
    body: JsonValue = None
    input_schema: Mapping[str, JsonValue] = field(default_factory=dict)
    response_map: Mapping[str, str] = field(default_factory=dict)
    timeout_ms: int = 10_000
    retries: int = 0

    @property
    def inline(self) -> bool:
        """Whether the tool was defined inside the call request."""
        return self.tool_id is None

    @property
    def source_id(self) -> str:
        """What sent a request (pathway unit test mocks key on it): the ``TL-`` id, else the name."""
        return self.tool_id or self.name

    def masked_names(self) -> frozenset[str]:
        """Lower-cased header names whose values are masked in events and API answers."""
        names = set(self.secret_headers)
        if self.inline:
            names.update(self.headers)
        return frozenset(name.lower() for name in names)

    @classmethod
    def from_row(cls, row: Tool, secret_headers: Mapping[str, str]) -> ToolSpec:
        """A stored tool with its decrypted secret headers."""
        return cls(
            tool_id=to_public(PublicPrefix.TOOL, row.id),
            name=row.name,
            description=row.description,
            speech=row.speech,
            url=row.url,
            method=row.method,
            headers={str(name): str(value) for name, value in row.headers.items()},
            secret_headers=dict(secret_headers),
            query={str(name): str(value) for name, value in row.query.items()},
            body=row.body,
            input_schema=dict(row.input_schema),
            response_map={str(name): str(path) for name, path in row.response_map.items()},
            timeout_ms=row.timeout_ms,
            retries=row.retries,
        )

    @classmethod
    def from_inline(cls, tool: InlineTool) -> ToolSpec:
        """A Send Call inline tool (no secret headers; its headers are masked like secrets)."""
        return cls(
            tool_id=None,
            name=tool.name,
            description=tool.description,
            speech=tool.speech,
            url=tool.url,
            method=tool.method,
            headers=dict(tool.headers),
            query=dict(tool.query),
            body=tool.body,
            input_schema=dict(tool.input_schema),
            response_map=dict(tool.response),
            timeout_ms=tool.timeout_ms,
            retries=tool.retries,
        )


def function_def(spec: ToolSpec) -> ToolDef:
    """The OpenAI-style function the dialogue model sees (specs/13 §4)."""
    return ToolDef(name=spec.name, description=spec.description, parameters=dict(spec.input_schema))
