"""The ``{{ expression }}`` templating engine (specs/11-pathways-engine.md §8, Decision P05-T09a).

Templates are parsed once with :func:`parse_template` (a hand-written scanner, no ``eval``) and rendered any
number of times against a :class:`TemplateScope`:

- :func:`render` gives text, :func:`render_url` percent-encodes every expression value (literal text is kept),
  :func:`render_json` walks a JSON value and keeps the type of a string that is exactly one expression.
- A path's first segment is looked up in the scope layers in order: ``input`` (tool templates only),
  node-local variables, ``variables``, ``request_data`` (also addressable as ``request_data.x``), built-ins.
- A missing variable without ``default`` renders as ``""`` and is reported once per path through
  :class:`MissingVariables` (the runner turns the report into the ``TEMPLATE_VAR_MISSING`` warning event).
- ``\\{{`` renders a literal ``{{``; a ``}}`` outside an expression and any other backslash are literal text.
- :func:`render` and :func:`render_url` also accept template text, parsed through the LRU cache
  :func:`cached_template`. Only parsing raises (:class:`TemplateSyntaxError`); rendering never raises for any
  scope value (Decision P07-T02).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import lru_cache
from typing import Final
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import orjson
import phonenumbers

__all__ = [
    "MAX_EXPRESSIONS",
    "MAX_TEMPLATE_CHARS",
    "Expression",
    "Filter",
    "MissingVariables",
    "Template",
    "TemplateLimitError",
    "TemplateScope",
    "TemplateSyntaxError",
    "TextSegment",
    "cached_template",
    "parse_template",
    "render",
    "render_json",
    "render_text",
    "render_url",
    "stringify",
    "zone_named",
]

MAX_TEMPLATE_CHARS: Final = 20_000
MAX_EXPRESSIONS: Final = 500

_IDENT: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INDEX: Final = re.compile(r"[0-9]+")
_SIMPLE_FILTERS: Final = frozenset({"upper", "lower", "title", "json", "phone_spoken", "trim"})
_ARG_FILTERS: Final = frozenset({"default", "date"})
_WHITESPACE: Final = " \t\r\n"

_UNTERMINATED_EXPRESSION: Final = "unterminated expression"
_EMPTY_EXPRESSION: Final = "empty expression"
_EXPECTED_CLOSE: Final = "expected '}}'"
_EXPECTED_INDEX: Final = "expected a list index"
_UNCLOSED_INDEX: Final = "unclosed '['"
_EXPECTED: Final = "expected {}"
_UNKNOWN_FILTER: Final = "unknown filter {!r}"
_NEEDS_ARGUMENT: Final = "filter {!r} needs a quoted argument"
_EXPECTED_QUOTED: Final = "expected a quoted string"
_UNTERMINATED_STRING: Final = "unterminated string"
_TOO_MANY_EXPRESSIONS: Final = f"more than {MAX_EXPRESSIONS} expressions"
_TOO_LONG: Final = f"template longer than {MAX_TEMPLATE_CHARS} characters"


class TemplateSyntaxError(ValueError):
    """The template text does not follow the specs/11 §8 grammar; ``position`` is the offending index."""

    def __init__(self, message: str, position: int) -> None:
        super().__init__(f"{message} at position {position}")
        self.message = message
        self.position = position


class TemplateLimitError(TemplateSyntaxError):
    """The template is longer than 20 000 characters or holds more than 500 expressions."""


@dataclass(frozen=True, slots=True)
class TextSegment:
    """Literal text between expressions (escapes already resolved)."""

    text: str


@dataclass(frozen=True, slots=True)
class Filter:
    """One ``| name`` or ``| name:"arg"`` step."""

    name: str
    arg: str | None = None


@dataclass(frozen=True, slots=True)
class Expression:
    """A parsed ``{{ path | filter ... }}``; ``path_text`` is the canonical spelling for missing reports."""

    path: tuple[str | int, ...]
    filters: tuple[Filter, ...] = ()

    @property
    def path_text(self) -> str:
        """``a.b[0].c``: the path written back without whitespace."""
        parts: list[str] = []
        for step in self.path:
            if isinstance(step, int):
                parts.append(f"[{step}]")
            else:
                parts.append(f".{step}" if parts else step)
        return "".join(parts)


@dataclass(frozen=True, slots=True)
class Template:
    """A parsed template: literal text and expressions in source order."""

    source: str
    segments: tuple[TextSegment | Expression, ...]

    @property
    def expressions(self) -> tuple[Expression, ...]:
        """The template's expressions in order."""
        return tuple(segment for segment in self.segments if isinstance(segment, Expression))

    @property
    def single_expression(self) -> Expression | None:
        """The expression when the template is exactly one expression with no literal text, else ``None``."""
        if len(self.segments) == 1 and isinstance(self.segments[0], Expression):
            return self.segments[0]
        return None


class MissingVariables:
    """Reports each missing path once (per call): the ``TEMPLATE_VAR_MISSING`` deduplication."""

    def __init__(self, sink: Callable[[str], None] | None = None) -> None:
        self._sink = sink
        self._seen: set[str] = set()

    @property
    def paths(self) -> frozenset[str]:
        """Every path reported so far."""
        return frozenset(self._seen)

    def report(self, path: str) -> None:
        """Record ``path``; the sink hears about it only the first time."""
        if path in self._seen:
            return
        self._seen.add(path)
        if self._sink is not None:
            self._sink(path)


@dataclass(frozen=True, slots=True)
class TemplateScope:
    """The values a template can read, in resolution order (``input`` and ``node_vars`` only when given)."""

    variables: Mapping[str, object] = field(default_factory=dict)
    request_data: Mapping[str, object] = field(default_factory=dict)
    builtins: Mapping[str, object] = field(default_factory=dict)
    node_vars: Mapping[str, object] | None = None
    input: Mapping[str, object] | None = None
    timezone: str = "UTC"
    missing: MissingVariables | None = None

    def layers(self) -> tuple[Mapping[str, object], ...]:
        """The lookup layers in specs/11 §8 order."""
        optional = tuple(layer for layer in (self.input, self.node_vars) if layer is not None)
        alias: Mapping[str, object] = {"request_data": self.request_data}
        return (*optional, self.variables, self.request_data, alias, self.builtins)


class _Missing:
    """Sentinel for a value the scope does not have."""

    __slots__ = ()


_MISSING: Final = _Missing()


# ------------------------------------------------------------------------------------------------
# Parsing


class _Parser:
    """Scanner for one template text."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0
        self.segments: list[TextSegment | Expression] = []
        self.buffer: list[str] = []
        self.count = 0

    def parse(self) -> Template:
        text = self.text
        while self.pos < len(text):
            start = text.find("{{", self.pos)
            if start == -1:
                self.buffer.append(text[self.pos :])
                break
            if start > self.pos and text[start - 1] == "\\":
                self.buffer.append(text[self.pos : start - 1] + "{{")
                self.pos = start + 2
                continue
            self.buffer.append(text[self.pos : start])
            self._flush()
            self._expression(start)
        self._flush()
        return Template(source=text, segments=tuple(self.segments))

    def _flush(self) -> None:
        if self.buffer:
            joined = "".join(self.buffer)
            self.buffer.clear()
            if joined:
                self.segments.append(TextSegment(joined))

    def _expression(self, start: int) -> None:
        self.count += 1
        if self.count > MAX_EXPRESSIONS:
            raise TemplateLimitError(_TOO_MANY_EXPRESSIONS, start)
        self.pos = start + 2
        self._skip_ws()
        if self._at_end():
            raise TemplateSyntaxError(_UNTERMINATED_EXPRESSION, start)
        if self.text.startswith("}}", self.pos):
            raise TemplateSyntaxError(_EMPTY_EXPRESSION, start)
        path = self._path()
        filters: list[Filter] = []
        self._skip_ws()
        while self._peek() == "|":
            self.pos += 1
            filters.append(self._filter())
            self._skip_ws()
        if self._at_end():
            raise TemplateSyntaxError(_UNTERMINATED_EXPRESSION, start)
        if not self.text.startswith("}}", self.pos):
            raise TemplateSyntaxError(_EXPECTED_CLOSE, self.pos)
        self.pos += 2
        self.segments.append(Expression(path=tuple(path), filters=tuple(filters)))

    def _path(self) -> list[str | int]:
        steps: list[str | int] = [self._ident("variable name")]
        while True:
            char = self._peek()
            if char == ".":
                self.pos += 1
                steps.append(self._ident("name after '.'"))
            elif char == "[":
                steps.append(self._index())
            else:
                return steps

    def _index(self) -> int:
        opening = self.pos
        self.pos += 1
        match = _INDEX.match(self.text, self.pos)
        if match is None:
            raise TemplateSyntaxError(_EXPECTED_INDEX, self.pos)
        self.pos = match.end()
        if self._peek() != "]":
            raise TemplateSyntaxError(_UNCLOSED_INDEX, opening)
        self.pos += 1
        return int(match.group())

    def _ident(self, what: str) -> str:
        match = _IDENT.match(self.text, self.pos)
        if match is None:
            raise TemplateSyntaxError(_EXPECTED.format(what), self.pos)
        self.pos = match.end()
        return match.group()

    def _filter(self) -> Filter:
        self._skip_ws()
        at = self.pos
        name = self._ident("filter name")
        if name in _SIMPLE_FILTERS:
            return Filter(name)
        if name not in _ARG_FILTERS:
            raise TemplateSyntaxError(_UNKNOWN_FILTER.format(name), at)
        if self._peek() != ":":
            raise TemplateSyntaxError(_NEEDS_ARGUMENT.format(name), self.pos)
        self.pos += 1
        self._skip_ws()
        return Filter(name, self._quoted())

    def _quoted(self) -> str:
        quote_char = self._peek()
        if quote_char not in ('"', "'"):
            raise TemplateSyntaxError(_EXPECTED_QUOTED, self.pos)
        start = self.pos
        self.pos += 1
        chars: list[str] = []
        while not self._at_end():
            char = self.text[self.pos]
            if char == quote_char:
                self.pos += 1
                return "".join(chars)
            if char == "\\" and self.pos + 1 < len(self.text):
                self.pos += 1
                char = self.text[self.pos]
            chars.append(char)
            self.pos += 1
        raise TemplateSyntaxError(_UNTERMINATED_STRING, start)

    def _skip_ws(self) -> None:
        while not self._at_end() and self.text[self.pos] in _WHITESPACE:
            self.pos += 1

    def _peek(self) -> str:
        return "" if self._at_end() else self.text[self.pos]

    def _at_end(self) -> bool:
        return self.pos >= len(self.text)


def parse_template(text: str) -> Template:
    """Parse ``text``; raises :class:`TemplateSyntaxError` (or :class:`TemplateLimitError`) at a position."""
    if len(text) > MAX_TEMPLATE_CHARS:
        raise TemplateLimitError(_TOO_LONG, MAX_TEMPLATE_CHARS)
    return _Parser(text).parse()


@lru_cache(maxsize=1024)
def cached_template(text: str) -> Template:
    """:func:`parse_template` through a 1 024-entry LRU cache (templates are immutable: sharing is safe)."""
    return parse_template(text)


def _template_of(template: Template | str) -> Template:
    return cached_template(template) if isinstance(template, str) else template


# ------------------------------------------------------------------------------------------------
# Values


def _json_key(key: object) -> str:
    """A mapping key as orjson's ``OPT_NON_STR_KEYS`` writes it."""
    if isinstance(key, str):
        return key
    if isinstance(key, bool):
        return "true" if key else "false"
    if key is None:
        return "null"
    return str(key)


def _jsonable(value: object) -> object:
    """A copy of ``value`` the stdlib encoder accepts (fallback when orjson refuses, e.g. huge integers)."""
    if isinstance(value, str | bool | int) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {_json_key(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return str(value)


def _json_text(value: object) -> str:
    """Compact JSON for any value; never raises."""
    try:
        return orjson.dumps(value, default=str, option=orjson.OPT_NON_STR_KEYS).decode()
    except (orjson.JSONEncodeError, TypeError):
        return json.dumps(_jsonable(value), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def stringify(value: object) -> str:
    """Text form: strings as-is, ``None`` → ``""``, booleans ``true``/``false``, everything else as JSON."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return _json_text(value)


def _lookup(scope: TemplateScope, path: tuple[str | int, ...]) -> object:
    root = str(path[0])
    value: object = _MISSING
    for layer in scope.layers():
        if root in layer:
            value = layer[root]
            break
    for step in path[1:]:
        value = _step(value, step)
    return value


def _step(value: object, step: str | int) -> object:
    if isinstance(step, int):
        if isinstance(value, list | tuple) and step < len(value):
            item: object = value[step]
            return item
        return _MISSING
    if isinstance(value, Mapping) and step in value:
        found: object = value[step]
        return found
    return _MISSING


@lru_cache(maxsize=64)
def zone_named(name: str) -> ZoneInfo:
    """The zone called ``name``; UTC when the name is unknown."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _phone_spoken(text: str) -> str:
    digits = [char for char in text if char.isdigit()]
    try:
        number = phonenumbers.parse(text, "US")
    except phonenumbers.NumberParseException:
        number = None
    if number is None or not phonenumbers.is_possible_number(number):
        return " ".join(digits) if digits else text
    national = phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.NATIONAL)
    groups = re.findall(r"\d+", national)
    return ", ".join(" ".join(group) for group in groups)


def _parse_moment(value: object) -> datetime | date | None:
    if isinstance(value, datetime | date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.strip())
    except ValueError:
        pass
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _format_date(value: object, fmt: str, timezone: str) -> str:
    moment = _parse_moment(value)
    if moment is None:
        return stringify(value)
    try:
        if isinstance(moment, datetime) and moment.tzinfo is not None:
            moment = moment.astimezone(zone_named(timezone))
        return moment.strftime(fmt)
    except (ValueError, OverflowError):
        return stringify(value)


_TEXT_FILTERS: Final[Mapping[str, Callable[[str], str]]] = {
    "upper": str.upper,
    "lower": str.lower,
    "title": str.title,
    "trim": str.strip,
    "phone_spoken": _phone_spoken,
}


def _apply(expression: Expression, scope: TemplateScope) -> tuple[object, bool]:
    """Resolve and filter; returns ``(value, missing)``."""
    value = _lookup(scope, expression.path)
    missing = value is _MISSING
    for step in expression.filters:
        if step.name == "default":
            if missing or value is None or value == "":
                value, missing = step.arg, False
        elif missing:
            continue
        elif step.name == "json":
            value = _json_text(value)
        elif step.name == "date":
            value = _format_date(value, step.arg or "", scope.timezone)
        else:
            value = _TEXT_FILTERS[step.name](stringify(value))
    return value, missing


def _evaluate(expression: Expression, scope: TemplateScope) -> object:
    value, missing = _apply(expression, scope)
    if missing:
        if scope.missing is not None:
            scope.missing.report(expression.path_text)
        return ""
    return value


# ------------------------------------------------------------------------------------------------
# Rendering


def _render(template: Template, scope: TemplateScope, encode: Callable[[str], str]) -> str:
    parts: list[str] = []
    for segment in template.segments:
        if isinstance(segment, TextSegment):
            parts.append(segment.text)
        else:
            parts.append(encode(stringify(_evaluate(segment, scope))))
    return "".join(parts)


def _as_is(text: str) -> str:
    return text


def _percent(text: str) -> str:
    return quote(text, safe="")


def render(template: Template | str, scope: TemplateScope) -> str:
    """Render ``template`` (parsed, or text parsed through :func:`cached_template`) as text."""
    return _render(_template_of(template), scope, _as_is)


def render_url(template: Template | str, scope: TemplateScope) -> str:
    """Render ``template`` as a URL: every expression value is percent-encoded, literal text is kept."""
    return _render(_template_of(template), scope, _percent)


def render_text(text: str, scope: TemplateScope) -> str:
    """Parse and render ``text`` (raises :class:`TemplateSyntaxError` for invalid templates)."""
    return render(text, scope)


def render_json(value: object, scope: TemplateScope) -> object:
    """Render every string inside a JSON value; a lone-expression string keeps the value's type.

    Mapping keys are not templated. A missing single expression renders as ``""``.
    """
    if isinstance(value, str):
        template = cached_template(value)
        single = template.single_expression
        if single is None:
            return render(template, scope)
        return _evaluate(single, scope)
    if isinstance(value, Mapping):
        return {key: render_json(item, scope) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [render_json(item, scope) for item in value]
    return value
