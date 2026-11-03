"""JSONPath extraction (specs/13 §5 step 6) over jsonpath-ng's extended parser (filters, arithmetic).

A single match gives its value, several matches a list, no match ``None``. An invalid path never raises from
:func:`extract`: it is logged (``tools.jsonpath_invalid``, path length only) and gives ``None``. Compiled
paths are cached.
"""

from __future__ import annotations

import functools
from collections.abc import Iterable, Mapping
from typing import Final

from jsonpath_ng import JSONPath
from jsonpath_ng.exceptions import JsonPathLexerError, JsonPathParserError
from jsonpath_ng.ext.parser import parse
from pydantic import JsonValue

from va_common.logging import get_logger

__all__ = ["CACHE_SIZE", "InvalidJsonPathError", "compile_path", "extract", "extract_all"]

CACHE_SIZE: Final = 512

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
