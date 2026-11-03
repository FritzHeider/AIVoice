"""Output schemas of the pathway decision calls (specs/10 §9.1-§9.3; specs/11 §5.5 Decision P07-T04 rule 7).

The router and extraction schemas depend on the node (its path ids, its variables), so each distinct option
tuple / variable tuple gets one pydantic class, cached (LRU 256). The classes are not registered with
``register_structured_function``: every graph shares the names ``select_path`` and ``extract_variables``.
"""

from __future__ import annotations

import functools
from typing import Annotated, Any, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, create_model

__all__ = [
    "SCHEMA_CACHE_SIZE",
    "ConditionScore",
    "RouteAlternative",
    "VariableType",
    "extraction_schema",
    "route_schema",
]

SCHEMA_CACHE_SIZE: Final = 256

type VariableType = Literal["string", "integer", "number", "boolean"]

Confidence = Annotated[int, Field(ge=0, le=10)]

# Every variable is required and nullable; integer/number/boolean also accept a string (spoken numbers, yes/no
# words) that the extractor coerces after validation.
_VALUE_TYPES: Final[dict[str, Any]] = {
    "string": str | None,
    "integer": int | str | None,
    "number": float | str | None,
    "boolean": bool | str | None,
}


class RouteAlternative(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path_id: str
    confidence: Confidence


class ConditionScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: Confidence
    met: bool
    missing: str = Field(max_length=200)


@functools.lru_cache(maxsize=SCHEMA_CACHE_SIZE)
def route_schema(option_ids: tuple[str, ...]) -> type[BaseModel]:
    """The ``select_path`` schema whose ``path_id`` enum is ``option_ids`` (duplicates dropped, order
    kept)."""
    unique = tuple(dict.fromkeys(option_ids))
    if not unique:
        msg = "route_schema needs at least one option"
        raise ValueError(msg)
    path_id = cast("Any", Literal).__getitem__(unique)
    return create_model(
        "SelectPath",
        __config__=ConfigDict(extra="forbid"),
        path_id=(path_id, ...),
        confidence=(Confidence, ...),
        alternatives=(list[RouteAlternative], ...),
        reason=(str, Field(max_length=200)),
    )


@functools.lru_cache(maxsize=SCHEMA_CACHE_SIZE)
def extraction_schema(variables: tuple[tuple[str, VariableType], ...]) -> type[BaseModel]:
    """The ``extract_variables`` schema: one required, nullable property per ``(name, type)`` (first
    occurrence of a name wins). Properties are named by the variable; attributes are ``v0``, ``v1``, ...
    so any name works (dump with ``by_alias=True``)."""
    names: dict[str, VariableType] = {}
    for name, kind in variables:
        names.setdefault(name, kind)
    fields: dict[str, Any] = {
        f"v{index}": (_VALUE_TYPES[kind], Field(alias=name))
        for index, (name, kind) in enumerate(names.items())
    }
    return create_model(
        "ExtractVariables",
        __config__=ConfigDict(extra="forbid", populate_by_name=False),
        **fields,
    )
