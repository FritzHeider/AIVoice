"""Success bodies of envelope routes (specs/31-error-catalog.md §1): ``{"data": ..., "errors": null}``."""

from __future__ import annotations

from typing import Any, get_args, get_origin

from pydantic import BaseModel, ConfigDict

__all__ = ["DataResponse"]


def _model_examples(model: object) -> list[Any]:
    """The ``examples`` of a pydantic model class, else an empty list."""
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        return []
    extra = model.model_config.get("json_schema_extra")
    examples = extra.get("examples") if isinstance(extra, dict) else None
    return list(examples) if isinstance(examples, list) else []


def _wrap_examples(schema: dict[str, Any], cls: type[Any]) -> None:
    """Give ``DataResponse[T]`` the examples of ``T`` in the envelope (specs/26 §1: every 2xx has one).

    For ``DataResponse[list[M]]`` each example of ``M`` becomes a one-item list.
    """
    args = getattr(cls, "__pydantic_generic_metadata__", {}).get("args", ())
    if not args:
        return
    if get_origin(args[0]) is list:
        data = [[example] for example in _model_examples(get_args(args[0])[0])]
    else:
        data = _model_examples(args[0])
    if data:
        schema["examples"] = [{"data": example, "errors": None} for example in data]


class DataResponse[T](BaseModel):
    """The envelope success body around ``data``."""

    model_config = ConfigDict(json_schema_extra=_wrap_examples)

    data: T
    errors: None = None
