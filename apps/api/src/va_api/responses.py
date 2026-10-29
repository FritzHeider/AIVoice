"""Success bodies of envelope routes (specs/31-error-catalog.md §1): ``{"data": ..., "errors": null}``."""

from __future__ import annotations

from pydantic import BaseModel

__all__ = ["DataResponse"]


class DataResponse[T](BaseModel):
    """The envelope success body around ``data``."""

    data: T
    errors: None = None
