"""Request rendering of tools (specs/13-tools-and-dynamic-data.md §5 step 3).

Rules: specs/13 §5 Decision P08-T02a.

* ``url``: :func:`va_engine.templating.render_url` (expression values percent-encoded). Each ``query``
  value is rendered as text and merged into the URL query; a ``query`` key replaces every pair of the same
  key already in the URL, other pairs keep their order.
* ``headers``: rendered as text. Secret headers are sent verbatim (a secret is never a template) and win
  over a plain header of the same name (case-insensitive).
* ``body``: :func:`va_engine.templating.render_json` (a lone expression keeps its value's type).

The shown headers (API answers, ``call.tool`` events) mask every header in
:meth:`va_tools.models.ToolSpec.masked_names` and every secret-named one (``Authorization``, ``X-Api-Key``,
…) as ``••••``. A template that does not parse raises :class:`ToolRenderError` naming the field.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import JsonValue

from va_common.json import dumps_str
from va_common.masking import is_secret_key
from va_engine.ports import HttpCall
from va_engine.templating import TemplateScope, TemplateSyntaxError, render, render_json, render_url
from va_tools.models import ToolSpec
from va_tools.secrets import MASK

__all__ = [
    "EXCERPT_LIMIT",
    "RenderedRequest",
    "ToolRenderError",
    "excerpt",
    "merge_query",
    "render_request",
    "shown_headers",
]

EXCERPT_LIMIT: Final = 2_000
_ELLIPSIS: Final = "…"


class ToolRenderError(ValueError):
    """A tool template does not parse; ``field``: ``url``, ``query.<key>``, ``headers.<name>``, ``body``."""

    def __init__(self, field: str) -> None:
        super().__init__(f"tool template in {field} does not parse")
        self.field = field


@dataclass(frozen=True, slots=True)
class RenderedRequest:
    """The request to send and its headers as they may be shown."""

    call: HttpCall
    shown_headers: dict[str, str]


def excerpt(value: JsonValue, limit: int = EXCERPT_LIMIT) -> str | None:
    """``value`` as text (JSON unless it is a string), at most ``limit`` characters (``…`` marks a cut)."""
    if value is None:
        return None
    text = value if isinstance(value, str) else dumps_str(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1] + _ELLIPSIS


def merge_query(url: str, query: Mapping[str, str]) -> str:
    """``url`` with ``query`` merged in (a ``query`` key replaces the URL's pairs of that key)."""
    if not query:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url  # the executor's preflight answers invalid_url
    kept = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in query]
    merged = urlencode([*kept, *query.items()])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, merged, parts.fragment))


def shown_headers(headers: Mapping[str, str], masked: frozenset[str]) -> dict[str, str]:
    """``headers`` with masked and secret-named values replaced by ``••••``."""
    return {
        name: MASK if name.lower() in masked or is_secret_key(name) else value
        for name, value in headers.items()
    }


def _rendered(field: str, text: str, scope: TemplateScope, *, url: bool = False) -> str:
    try:
        return render_url(text, scope) if url else render(text, scope)
    except TemplateSyntaxError:
        raise ToolRenderError(field) from None


def _headers(spec: ToolSpec, scope: TemplateScope) -> dict[str, str]:
    secret_names = {name.lower() for name in spec.secret_headers}
    headers = {
        name: _rendered(f"headers.{name}", value, scope)
        for name, value in spec.headers.items()
        if name.lower() not in secret_names
    }
    headers.update(spec.secret_headers)
    return headers


def render_request(spec: ToolSpec, scope: TemplateScope) -> RenderedRequest:
    """The rendered request of ``spec`` in ``scope`` (see the module docstring)."""
    url = _rendered("url", spec.url, scope, url=True).strip()
    query = {key: _rendered(f"query.{key}", value, scope) for key, value in spec.query.items()}
    headers = _headers(spec, scope)
    try:
        body = cast("JsonValue", render_json(spec.body, scope)) if spec.body is not None else None
    except TemplateSyntaxError:
        raise ToolRenderError("body") from None
    call = HttpCall(
        method=spec.method,
        url=merge_query(url, query),
        headers=headers,
        body=body,
        timeout_ms=spec.timeout_ms,
        retries=spec.retries,
        extract=tuple(spec.response_map.items()),
        source_id=spec.source_id,
    )
    return RenderedRequest(call=call, shown_headers=shown_headers(headers, spec.masked_names()))
