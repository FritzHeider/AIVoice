"""Telnyx-shaped error responses (specs/30-testing-and-evals.md §3.2; specs/06-telephony-telnyx.md §2).

Body ``{"errors": [{"code", "title", "detail", "source"?: {"pointer"} | {"parameter"}}]}``. Body fields are
addressed with JSON pointers (``/phone_numbers/0/phone_number``), query parameters by name
(``filter[limit]``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from pydantic import ValidationError
from starlette.responses import JSONResponse

__all__ = [
    "AUTHENTICATION_FAILED",
    "INVALID_FROM",
    "INVALID_VALUE",
    "NOT_FOUND",
    "NUMBER_NOT_AVAILABLE",
    "TelnyxIssue",
    "invalid",
    "invalid_from",
    "invalid_parameter",
    "issues_from_validation",
    "not_found",
    "telnyx_error",
    "unauthorized",
]

AUTHENTICATION_FAILED: Final = "10009"
NOT_FOUND: Final = "10005"
INVALID_VALUE: Final = "10015"
INVALID_FROM: Final = "40305"
NUMBER_NOT_AVAILABLE: Final = "85001"

_TITLES: Final = {
    AUTHENTICATION_FAILED: "Authentication failed",
    NOT_FOUND: "Resource not found",
    INVALID_VALUE: "Invalid value",
    INVALID_FROM: "Invalid 'from' address",
    NUMBER_NOT_AVAILABLE: "Number not available",
}


@dataclass(frozen=True, slots=True)
class TelnyxIssue:
    """One entry of a Telnyx ``errors`` array."""

    code: str
    detail: str
    pointer: str | None = None
    parameter: str | None = None

    def to_json(self) -> dict[str, object]:
        body: dict[str, object] = {"code": self.code, "title": _TITLES[self.code], "detail": self.detail}
        if self.pointer is not None:
            body["source"] = {"pointer": self.pointer}
        elif self.parameter is not None:
            body["source"] = {"parameter": self.parameter}
        return body


def telnyx_error(status: int, *issues: TelnyxIssue) -> JSONResponse:
    return JSONResponse({"errors": [issue.to_json() for issue in issues]}, status_code=status)


def unauthorized() -> JSONResponse:
    return telnyx_error(
        401, TelnyxIssue(AUTHENTICATION_FAILED, "Could not understand the provided credentials.")
    )


def not_found(kind: str, resource_id: str) -> JSONResponse:
    return telnyx_error(
        404, TelnyxIssue(NOT_FOUND, f"The requested {kind} {resource_id!r} could not be found.")
    )


def invalid(detail: str, pointer: str) -> JSONResponse:
    return telnyx_error(422, TelnyxIssue(INVALID_VALUE, detail, pointer=pointer))


def invalid_parameter(detail: str, parameter: str) -> JSONResponse:
    return telnyx_error(422, TelnyxIssue(INVALID_VALUE, detail, parameter=parameter))


def invalid_from(phone_number: str) -> JSONResponse:
    return telnyx_error(
        422,
        TelnyxIssue(
            INVALID_FROM,
            f"The 'from' number {phone_number} is not a number on this account.",
            pointer="/from",
        ),
    )


def _detail(error_type: str, message: str) -> str:
    if error_type == "missing":
        return "is required"
    if error_type == "extra_forbidden":
        return "is not a recognised parameter"
    return message


def issues_from_validation(error: ValidationError) -> list[TelnyxIssue]:
    """One ``10015`` issue per pydantic error, located by JSON pointer."""
    return [
        TelnyxIssue(
            INVALID_VALUE,
            _detail(item["type"], item["msg"]),
            pointer="".join(f"/{part}" for part in item["loc"]),
        )
        for item in error.errors(include_url=False)
    ]
