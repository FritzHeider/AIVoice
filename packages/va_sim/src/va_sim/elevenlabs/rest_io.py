"""FakeElevenLabs REST validation errors (Decisions P03-T04 rule 11, P03-T05b rule 1).

ElevenLabs answers invalid input with HTTP 422 ``{"detail": [{"loc", "msg", "type"}]}``, the FastAPI shape;
REST synthesis and batch transcription share these helpers.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import ValidationError
from starlette.responses import JSONResponse

__all__ = ["error_item", "unprocessable", "validation_errors"]


def error_item(loc: Sequence[str | int], msg: str, error_type: str) -> dict[str, object]:
    return {"loc": list(loc), "msg": msg, "type": error_type}


def unprocessable(errors: list[dict[str, object]]) -> JSONResponse:
    return JSONResponse({"detail": errors}, status_code=422)


def validation_errors(exc: ValidationError, source: str = "body") -> list[dict[str, object]]:
    """``exc``'s errors with ``loc`` prefixed by ``source`` (``body`` or ``query``)."""
    return [error_item([source, *error["loc"]], error["msg"], error["type"]) for error in exc.errors()]
