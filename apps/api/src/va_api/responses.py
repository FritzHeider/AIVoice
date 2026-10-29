"""Success bodies of envelope routes (specs/31-error-catalog.md §1, §2): ``{"data": ..., "errors": null}``,
and for cursor-paginated lists
``{"data": [...], "errors": null, "pagination": {"next_cursor": ..., "has_more": ...}}``."""

from __future__ import annotations

from typing import Any, Final, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["DataResponse", "PageResponse", "Pagination"]

_PAGINATION_EXAMPLE: Final[dict[str, Any]] = {
    "next_cursor": (
        "MjAyNi0xMC0yOVQxNDowMzoxMS4wMDAwMDArMDA6MDB8MDE5MmY0YjEtN2EyYy03ZDNlLTlmNDAtNWE2YjdjOGQ5ZTBm"
    ),
    "has_more": True,
}


def _model_examples(model: object) -> list[Any]:
    """The ``examples`` of a pydantic model class, else an empty list."""
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        return []
    extra = model.model_config.get("json_schema_extra")
    examples = extra.get("examples") if isinstance(extra, dict) else None
    return list(examples) if isinstance(examples, list) else []


def _parameter(cls: type[Any]) -> object:
    """``T`` of a parametrised generic envelope ``cls``, else ``None``."""
    args = getattr(cls, "__pydantic_generic_metadata__", {}).get("args", ())
    return args[0] if args else None


def _data_examples(cls: type[Any]) -> list[Any]:
    """The ``data`` examples of ``DataResponse[T]``: those of ``T``, or one-item lists for ``list[M]``."""
    parameter = _parameter(cls)
    if get_origin(parameter) is list:
        return [[example] for example in _model_examples(get_args(parameter)[0])]
    return _model_examples(parameter)


def _wrap_examples(schema: dict[str, Any], cls: type[Any]) -> None:
    """Give ``DataResponse[T]`` the examples of ``T`` in the envelope (specs/26 §1: every 2xx has one).

    For ``DataResponse[list[M]]`` each example of ``M`` becomes a one-item list.
    """
    data = _data_examples(cls)
    if data:
        schema["examples"] = [{"data": example, "errors": None} for example in data]


def _wrap_page_examples(schema: dict[str, Any], cls: type[Any]) -> None:
    """Give ``PageResponse[M]`` one-item pages of the examples of ``M`` with a ``pagination`` object."""
    examples = _model_examples(_parameter(cls))
    if examples:
        schema["examples"] = [
            {"data": [example], "errors": None, "pagination": _PAGINATION_EXAMPLE} for example in examples
        ]


class DataResponse[T](BaseModel):
    """The envelope success body around ``data``."""

    model_config = ConfigDict(json_schema_extra=_wrap_examples)

    data: T
    errors: None = None


class Pagination(BaseModel):
    """Where the next page starts (specs/31-error-catalog.md §2)."""

    next_cursor: str | None = Field(description="Pass as `cursor` for the next page; null on the last page.")
    has_more: bool = Field(description="Whether another page follows.")


class PageResponse[T](BaseModel):
    """The envelope success body of a cursor-paginated list: one page of ``data``, newest first."""

    model_config = ConfigDict(json_schema_extra=_wrap_page_examples)

    data: list[T]
    errors: None = None
    pagination: Pagination
