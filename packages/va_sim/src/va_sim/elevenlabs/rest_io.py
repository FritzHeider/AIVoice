"""FakeElevenLabs REST request helpers (Decisions P03-T04 rule 11, P03-T05b rule 1, P03-T05c rule 3).

ElevenLabs answers invalid input with HTTP 422 ``{"detail": [{"loc", "msg", "type"}]}``, the FastAPI shape;
REST synthesis, batch transcription, and the voices routes share these helpers, the JSON body parser, and the
multipart form opener.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Final

import orjson
from pydantic import BaseModel, ValidationError
from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import JSONResponse

__all__ = [
    "InvalidBodyError",
    "error_item",
    "multipart_form",
    "parse_json_body",
    "unprocessable",
    "validation_errors",
]

_MULTIPART: Final = "multipart/form-data"
_MAX_FIELDS: Final = 2000


def error_item(loc: Sequence[str | int], msg: str, error_type: str) -> dict[str, object]:
    return {"loc": list(loc), "msg": msg, "type": error_type}


def unprocessable(errors: list[dict[str, object]]) -> JSONResponse:
    return JSONResponse({"detail": errors}, status_code=422)


def validation_errors(exc: ValidationError, source: str = "body") -> list[dict[str, object]]:
    """``exc``'s errors with ``loc`` prefixed by ``source`` (``body`` or ``query``)."""
    return [error_item([source, *error["loc"]], error["msg"], error["type"]) for error in exc.errors()]


def parse_json_body[M: BaseModel](raw: bytes, model: type[M]) -> M | JSONResponse:
    """``raw`` validated as ``model``, or the 422 response (``["body"]`` for invalid JSON or a non-object)."""
    try:
        payload = orjson.loads(raw)
    except orjson.JSONDecodeError as exc:
        return unprocessable([error_item(["body"], str(exc), "json_invalid")])
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        return unprocessable(validation_errors(exc))


class InvalidBodyError(Exception):
    """The body is not a parseable multipart form; ``response`` is the 422 ``["body"]`` answer."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.response = unprocessable([error_item(["body"], message, "value_error")])


@asynccontextmanager
async def multipart_form(request: Request, *, max_files: int) -> AsyncIterator[FormData]:
    """The parsed multipart form (closed on exit); :class:`InvalidBodyError` for another content type or a
    parse failure such as more than ``max_files`` files."""
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != _MULTIPART:
        msg = "the body must be multipart/form-data"
        raise InvalidBodyError(msg)
    try:
        form = await request.form(max_files=max_files, max_fields=_MAX_FIELDS)
    except (HTTPException, MultiPartException) as exc:
        message = exc.detail if isinstance(exc, HTTPException) else exc.message
        raise InvalidBodyError(str(message)) from exc
    try:
        yield form
    finally:
        await form.close()
