"""Success bodies of envelope routes (specs/31-error-catalog.md §1): ``{"data": ..., "errors": null}``."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

__all__ = ["DataResponse"]


def _wrap_examples(schema: dict[str, Any], cls: type[Any]) -> None:
    """Give ``DataResponse[T]`` the examples of ``T`` in the envelope (specs/26 §1: every 2xx has one)."""
    args = getattr(cls, "__pydantic_generic_metadata__", {}).get("args", ())
    if not args or not isinstance(args[0], type) or not issubclass(args[0], BaseModel):
        return
    extra = args[0].model_config.get("json_schema_extra")
    examples = extra.get("examples") if isinstance(extra, dict) else None
    if isinstance(examples, list) and examples:
        schema["examples"] = [{"data": example, "errors": None} for example in examples]


class DataResponse[T](BaseModel):
    """The envelope success body around ``data``."""

    model_config = ConfigDict(json_schema_extra=_wrap_examples)

    data: T
    errors: None = None
