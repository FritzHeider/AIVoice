"""FakeTelnyx HTTP plumbing: Bearer auth, ``{"data"}`` responses, body parsing, pagination, query integers.

Lists page with ``page[number]`` (default 1) and ``page[size]`` (default 20, at most 250) and report
``meta {"page_number", "page_size", "total_pages", "total_results"}`` (specs/30 §3.2 Decision 2026-10-29).
"""

from __future__ import annotations

import math
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final

import orjson
from pydantic import BaseModel, ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from va_sim.telnyx.errors import (
    invalid,
    invalid_parameter,
    issues_from_validation,
    telnyx_error,
    unauthorized,
)
from va_sim.telnyx.store import Account, TelnyxStore

__all__ = [
    "AccountHandler",
    "account_for",
    "authenticated",
    "data",
    "int_param",
    "page",
    "parse_body",
    "parse_object",
    "validate",
]

type AccountHandler = Callable[[Request, Account], Awaitable[Response]]
type Endpoint = Callable[[Request], Awaitable[Response]]

_DEFAULT_PAGE_SIZE: Final = 20
_MAX_PAGE_SIZE: Final = 250


def data(obj: object, status: int = 200) -> JSONResponse:
    return JSONResponse({"data": obj}, status_code=status)


def account_for(request: Request, store: TelnyxStore) -> Account | JSONResponse:
    """The account of the request's ``Authorization: Bearer <key>``, else a ``10009`` response."""
    scheme, _, key = request.headers.get("authorization", "").partition(" ")
    account = store.accounts.get(key.strip()) if scheme.lower() == "bearer" else None
    return account if account is not None else unauthorized()


def authenticated(store: TelnyxStore, handler: AccountHandler) -> Endpoint:
    """``handler`` behind Bearer auth."""

    async def endpoint(request: Request) -> Response:
        account = account_for(request, store)
        if isinstance(account, JSONResponse):
            return account
        return await handler(request, account)

    return endpoint


async def parse_object(request: Request) -> dict[str, Any] | JSONResponse:
    """The request body as a JSON object, else a ``10015`` response."""
    try:
        body = orjson.loads(await request.body())
    except orjson.JSONDecodeError:
        return invalid("The request body is not valid JSON.", "")
    if not isinstance(body, dict):
        return invalid("The request body must be a JSON object.", "")
    return body


def validate[M: BaseModel](model: type[M], body: dict[str, Any]) -> M | JSONResponse:
    """``body`` validated as ``model``, else ``10015`` issues located by JSON pointer."""
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        return telnyx_error(422, *issues_from_validation(exc))


async def parse_body[M: BaseModel](request: Request, model: type[M]) -> M | JSONResponse:
    body = await parse_object(request)
    if isinstance(body, JSONResponse):
        return body
    return validate(model, body)


def int_param(request: Request, name: str, *, default: int, maximum: int) -> int | JSONResponse:
    """Query integer ``name`` in ``1..maximum`` (``default`` when absent), else a ``10015`` response."""
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return invalid_parameter(f"{name} must be an integer.", name)
    if not 1 <= value <= maximum:
        return invalid_parameter(f"{name} must be between 1 and {maximum}.", name)
    return value


def page(items: Sequence[object], request: Request) -> JSONResponse:
    """One page of ``items`` with Telnyx pagination ``meta``."""
    number = int_param(request, "page[number]", default=1, maximum=1_000_000)
    if isinstance(number, JSONResponse):
        return number
    size = int_param(request, "page[size]", default=_DEFAULT_PAGE_SIZE, maximum=_MAX_PAGE_SIZE)
    if isinstance(size, JSONResponse):
        return size
    start = (number - 1) * size
    meta = {
        "page_number": number,
        "page_size": size,
        "total_pages": max(1, math.ceil(len(items) / size)),
        "total_results": len(items),
    }
    return JSONResponse({"data": list(items[start : start + size]), "meta": meta})
