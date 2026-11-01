"""Typed readers over decoded Stripe form parameters (specs/30 §4.1 Decision P03-T07a rule 2).

Values arrive as strings; a handler declares which parameters it knows (anything else is 400
``parameter_unknown``, as Stripe answers) and which are integers or booleans. Readers raise
:class:`va_sim.stripe.errors.StripeError` (400 ``invalid_request_error`` with ``param``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from va_sim.stripe.errors import invalid_request, missing_param
from va_sim.stripe.form import FormValue

__all__ = [
    "MetadataPatch",
    "Params",
    "boolean",
    "check_known",
    "integer",
    "limit",
    "mapping",
    "metadata",
    "string",
]

type Params = Mapping[str, FormValue]

_INTEGER: Final = re.compile(r"-?\d+")
_TRUE: Final = "true"
_FALSE: Final = "false"
_MAX_LIMIT: Final = 100
_DEFAULT_LIMIT: Final = 10
_METADATA_MAX_KEYS: Final = 50


def _name(prefix: str, name: str) -> str:
    return f"{prefix}[{name}]" if prefix else name


def check_known(params: Params, allowed: frozenset[str], *, prefix: str = "") -> None:
    """400 ``parameter_unknown`` for the first key of ``params`` not in ``allowed``."""
    for key in params:
        if key not in allowed:
            name = _name(prefix, key)
            msg = f"Received unknown parameter: {name}"
            raise invalid_request(msg, code="parameter_unknown", param=name)


def string(params: Params, name: str, *, required: bool = False, prefix: str = "") -> str | None:
    """The string value of ``name`` (None when absent and not ``required``)."""
    value = params.get(name)
    full = _name(prefix, name)
    if value is None:
        if required:
            raise missing_param(full)
        return None
    if not isinstance(value, str):
        msg = f"Invalid string: {full} must be a string"
        raise invalid_request(msg, param=full)
    return value


def integer(params: Params, name: str, *, required: bool = False) -> int | None:
    """A base-10 integer (``parameter_invalid_integer`` otherwise)."""
    raw = string(params, name, required=required)
    if raw is None:
        return None
    if _INTEGER.fullmatch(raw) is None:
        msg = f"Invalid integer: {raw}"
        raise invalid_request(msg, code="parameter_invalid_integer", param=name)
    return int(raw)


def boolean(params: Params, name: str) -> bool | None:
    """``true``/``false`` (``parameter_invalid_boolean`` otherwise)."""
    raw = string(params, name)
    if raw is None:
        return None
    if raw not in {_TRUE, _FALSE}:
        msg = f"Invalid boolean: {raw}"
        raise invalid_request(msg, code="parameter_invalid_boolean", param=name)
    return raw == _TRUE


def mapping(params: Params, name: str) -> Mapping[str, FormValue] | None:
    """A nested hash (``name[key]=...``); an indexed list is read as a hash keyed ``"0"``, ``"1"``, ..."""
    value = params.get(name)
    if value is None:
        return None
    if isinstance(value, list):
        return {str(index): item for index, item in enumerate(value)}
    if isinstance(value, str):
        msg = f"Invalid object: {name} must be a hash"
        raise invalid_request(msg, param=name)
    return value


def limit(params: Params) -> int:
    """List ``limit``: 1..100, default 10."""
    value = integer(params, "limit")
    if value is None:
        return _DEFAULT_LIMIT
    if not 1 <= value <= _MAX_LIMIT:
        msg = f"Invalid limit: must be between 1 and {_MAX_LIMIT}"
        raise invalid_request(
            msg,
            code="parameter_invalid_integer",
            param="limit",
        )
    return value


@dataclass(frozen=True, slots=True)
class MetadataPatch:
    """A ``metadata`` update: ``clear`` (``metadata=``) empties it first; empty values delete keys."""

    clear: bool = False
    values: Mapping[str, str] = field(default_factory=dict)

    def apply(self, current: Mapping[str, str]) -> dict[str, str]:
        merged = {} if self.clear else dict(current)
        for key, value in self.values.items():
            if value:
                merged[key] = value
            else:
                merged.pop(key, None)
        return merged


def metadata(params: Params) -> MetadataPatch | None:
    """The ``metadata`` parameter as a patch (None when absent)."""
    if params.get("metadata") == "":
        return MetadataPatch(clear=True)
    hash_ = mapping(params, "metadata")
    if hash_ is None:
        return None
    values: dict[str, str] = {}
    for key, item in hash_.items():
        if not isinstance(item, str):
            msg = f"Invalid metadata[{key}]: values must be strings"
            raise invalid_request(msg, param=f"metadata[{key}]")
        values[key] = item
    if len(values) > _METADATA_MAX_KEYS:
        msg = f"Invalid metadata: at most {_METADATA_MAX_KEYS} keys are allowed"
        raise invalid_request(msg, param="metadata")
    return MetadataPatch(values=values)
