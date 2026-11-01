"""Stripe error bodies (specs/30 §4.1 Decision P03-T07a rule 3).

``{"error": {"type", "message", "code"?, "decline_code"?, "param"?, "doc_url"?, ...}}``. Handlers raise
:class:`StripeError`; the endpoint wrapper renders it (and stores it for ``Idempotency-Key`` replays).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, Literal

__all__ = [
    "ErrorType",
    "StripeError",
    "error_object",
    "invalid_request",
    "missing_param",
    "resource_missing",
    "unrecognized_url",
]

type ErrorType = Literal[
    "api_error", "card_error", "idempotency_error", "invalid_request_error", "authentication_error"
]

_DOC_BASE: Final = "https://docs.stripe.com/error-codes/"


def error_object(
    type_: ErrorType,
    message: str,
    *,
    code: str | None = None,
    param: str | None = None,
    decline_code: str | None = None,
) -> dict[str, Any]:
    """The ``error`` member (also a payment intent's ``last_payment_error``); absent fields are omitted."""
    error: dict[str, Any] = {"type": type_, "message": message}
    if code is not None:
        error["code"] = code
        error["doc_url"] = _DOC_BASE + code.replace("_", "-")
    if decline_code is not None:
        error["decline_code"] = decline_code
    if param is not None:
        error["param"] = param
    return error


class StripeError(Exception):
    """An error answer: HTTP ``status`` and the ``error`` object, plus ``extra`` members inside it."""

    def __init__(  # noqa: PLR0913 - mirrors the Stripe error object fields
        self,
        status: int,
        type_: ErrorType,
        message: str,
        *,
        code: str | None = None,
        param: str | None = None,
        decline_code: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.error = error_object(type_, message, code=code, param=param, decline_code=decline_code)
        if extra:
            self.error.update(extra)

    def body(self) -> dict[str, Any]:
        return {"error": self.error}


def invalid_request(message: str, *, code: str | None = None, param: str | None = None) -> StripeError:
    """A 400 ``invalid_request_error``."""
    return StripeError(400, "invalid_request_error", message, code=code, param=param)


def missing_param(param: str) -> StripeError:
    return invalid_request(f"Missing required param: {param}.", code="parameter_missing", param=param)


def resource_missing(kind: str, object_id: str, *, param: str = "id") -> StripeError:
    """404 ``No such <kind>: '<id>'``."""
    return StripeError(
        404,
        "invalid_request_error",
        f"No such {kind}: '{object_id}'",
        code="resource_missing",
        param=param,
    )


def unrecognized_url(method: str, path: str) -> StripeError:
    return StripeError(
        404,
        "invalid_request_error",
        f"Unrecognized request URL ({method}: {path}). Please see https://stripe.com/docs or we can help at "
        "https://support.stripe.com/.",
    )
