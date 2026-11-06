"""JSONPath extraction (specs/13 §5 step 6) over jsonpath-ng's extended parser (filters, arithmetic).

A single match gives its value, several matches a list, no match ``None``. An invalid path never raises from
:func:`extract`: it is logged (``tools.jsonpath_invalid``, path length only) and gives ``None``. Compiled
paths are cached.

:func:`inline_read` tells whether an answer may be parsed and searched on the event loop (specs/13 §9
Decision P08-FIX-25). Parsing costs the same in any thread (it holds the interpreter lock: about 9 ms per
MiB), so only the paths decide. The rule is an allowlist: a path is read on the loop only when its work is
known to be bounded by the answer's size; every other path goes to the reader thread whatever the size.

* A path with ``..`` anywhere in its text is never read on the loop: a descendant step visits the whole
  document, and two of them are quadratic in its nesting depth (a 4 KiB answer nested 680 deep: 650 ms with
  ``$..*..x``). This is a textual test, so a quoted name holding ``..`` only costs a thread hop.
* A direct path in the plain spelling, names and indexes only (``$.a.b[0]``, ``$['a b']``), costs
  microseconds at any size and never counts.
* Any other path is judged by its compiled tree. It is *descending* when it is ``$`` at the far left
  followed only by steps that each go from a value to distinct children of it: one name, ``*``, one index,
  a slice, or a filter whose expressions each test (existence, ``!``) or compare (``==``, ``=``, ``!=``,
  ``<``, ``<=``, ``>``, ``>=``) a relative path of single names and indexes (``@``, ``@.a[0].b``) with a
  literal. Every step moves one level down, so no value of the answer is visited twice by the path: its
  work is linear in the answer. A descending path weighs 1, plus 1 and the steps of the relative path for
  every filter expression (each candidate walks them). The answer is read on the loop while its bytes times
  the summed weights stay within :data:`INLINE_SCAN_BYTES` (about 0.5 ms in the common shapes, 1.4 ms in
  the worst one measured).
* Everything else is never read on the loop, at any size: ``$`` or ``@`` anywhere else, the named operators
  (```parent```, ```len```, ```sorted```, ```sub(...)```, ...), a union or a step with several names or
  indexes, ``where``, arithmetic, ``=~``, ``||``, a filter that compares with another path or holds a
  wildcard, a path that does not start with ``$``, and a path that does not compile (each read parses it
  again). These shapes multiply: root re-entry or ```parent``` after a wildcard is quadratic in the element
  count (2 KiB: 2 s), a multi-name step doubles the matches per step (a 121-byte answer: 6 s).

``$['*']`` is a wildcard in jsonpath-ng, so a quoted name holding ``*`` is not a direct path.
"""

from __future__ import annotations

import functools
import re
from collections.abc import Iterable, Mapping
from typing import Final

from jsonpath_ng import JSONPath
from jsonpath_ng.exceptions import JsonPathLexerError, JsonPathParserError
from jsonpath_ng.ext.filter import Expression, Filter
from jsonpath_ng.ext.parser import parse
from jsonpath_ng.jsonpath import Child, Fields, Index, Root, Slice, This
from pydantic import JsonValue

from va_common.logging import get_logger

__all__ = [
    "CACHE_SIZE",
    "INLINE_SCAN_BYTES",
    "InvalidJsonPathError",
    "compile_path",
    "extract",
    "extract_all",
    "inline_read",
]

CACHE_SIZE: Final = 512
INLINE_SCAN_BYTES: Final = 2_048
"""Answer bytes times the summed weights of its counting paths that may be searched on the event loop."""
_DIRECT: Final = re.compile(
    r"""\$(?:\.[A-Za-z_][A-Za-z0-9_]*|\[\d+\]|\['[^'\[\]\\*]*'\]|\["[^"\[\]\\*]*"\])*""", re.ASCII
)
"""A path of names and indexes only, in the plain spelling."""
_COMPARISONS: Final = frozenset({None, "!", "==", "=", "!=", "<", "<=", ">", ">="})
"""The filter operators whose work is bounded by the compared value (``=~`` runs a regular expression)."""

_log = get_logger(__name__)


class InvalidJsonPathError(ValueError):
    """``path`` is not a valid JSONPath expression."""

    def __init__(self, path: str) -> None:
        super().__init__(f"invalid JSONPath expression ({len(path)} characters)")
        self.path = path


@functools.lru_cache(maxsize=CACHE_SIZE)
def compile_path(path: str) -> JSONPath:
    """The compiled expression of ``path``; :class:`InvalidJsonPathError` when it does not parse."""
    text = path.strip()
    if not text:
        raise InvalidJsonPathError(path)
    try:
        return parse(text)
    except (JsonPathLexerError, JsonPathParserError, ValueError, TypeError, AttributeError) as exc:
        raise InvalidJsonPathError(path) from exc


def extract(document: JsonValue, path: str) -> JsonValue:
    """The value(s) ``path`` selects in ``document`` (one match → value, several → list, none → ``None``)."""
    try:
        expression = compile_path(path)
    except InvalidJsonPathError:
        _log.warning("tools.jsonpath_invalid", length=len(path))
        return None
    try:
        matches: list[JsonValue] = [match.value for match in expression.find(document)]
    except Exception as exc:  # jsonpath-ng evaluation errors are arbitrary (re.error, TypeError, ...)
        _log.warning("tools.jsonpath_failed", length=len(path), error=type(exc).__name__)
        return None
    if not matches:
        return None
    return matches[0] if len(matches) == 1 else matches


def extract_all(document: JsonValue, pairs: Iterable[tuple[str, str]]) -> Mapping[str, JsonValue]:
    """``{name: extract(document, path)}`` for every ``(name, path)`` pair (later names win)."""
    return {name: extract(document, path) for name, path in pairs}


def inline_read(size: int, paths: Iterable[str]) -> bool:
    """Whether an answer of ``size`` bytes searched with ``paths`` may be read on the event loop (see the
    module docstring; no paths: always)."""
    judged: list[str] = []
    for path in paths:
        text = path.strip()
        if ".." in text:
            return False
        if _DIRECT.fullmatch(text) is None:
            judged.append(path)
    if size * len(judged) > INLINE_SCAN_BYTES:
        return False  # each of them weighs at least 1: no need to compile any
    total = 0
    for path in judged:
        weight = _scan_weight(path)
        if weight is None:
            return False
        total += weight
    return size * total <= INLINE_SCAN_BYTES


@functools.lru_cache(maxsize=CACHE_SIZE)
def _scan_weight(path: str) -> int | None:
    """The weight of a descending ``path`` (at least 1); ``None`` for a path that is never read on the
    loop. Cached, so a path that does not compile is parsed on the loop once."""
    try:
        tree = compile_path(path)
    except InvalidJsonPathError:
        return None
    steps = _steps(tree, Root)
    if steps is None:
        return None
    weight = 1
    for step in steps:
        if _one_child(step, wildcard=True) or type(step) is Slice:
            continue
        extra = _filter_weight(step) if type(step) is Filter else None
        if extra is None:
            return None
        weight += extra
    return weight


def _steps(tree: JSONPath, origin: type[JSONPath]) -> list[JSONPath] | None:
    """The steps of a path that is ``origin`` at the far left followed by child steps; ``None`` for any
    other shape. Exact types only: the allowlist must not admit a subclass it has not judged."""
    steps: list[JSONPath] = []
    node = tree
    while type(node) is Child:
        steps.append(node.right)
        node = node.left
    return steps if type(node) is origin else None


def _one_child(step: JSONPath, *, wildcard: bool) -> bool:
    """Whether ``step`` is one name (or ``*`` when ``wildcard``) or one index."""
    if type(step) is Fields:
        return len(step.fields) == 1 and (wildcard or step.fields[0] != "*")
    return type(step) is Index and len(step.indices) == 1


def _filter_weight(step: Filter) -> int | None:
    """What a filter adds to its path's weight: per expression 1 and the steps of its relative path;
    ``None`` when an expression is not a test or comparison of a direct relative path with a literal."""
    weight = 0
    for expression in step.expressions:
        if type(expression) is not Expression or expression.op not in _COMPARISONS:
            return None
        if not (expression.value is None or isinstance(expression.value, bool | int | float | str)):
            return None
        target = _steps(expression.target, This)
        if target is None or not all(_one_child(part, wildcard=False) for part in target):
            return None
        weight += 1 + len(target)
    return weight
