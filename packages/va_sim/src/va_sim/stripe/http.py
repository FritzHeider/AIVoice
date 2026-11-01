"""The FakeStripe request pipeline (specs/30 §4.1 Decision P03-T07a rules 1-5).

:func:`stripe_endpoint` wraps a handler: ``Request-Id`` → API key auth → form decoding (``expand`` is accepted
and ignored) → ``Idempotency-Key`` replay/mismatch → handler → JSON answer with ``Request-Id`` and
``Stripe-Version``. Handlers return the 200 body or raise :class:`va_sim.stripe.errors.StripeError`.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import parse_qsl

import orjson
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from va_sim.stripe.errors import StripeError, invalid_request
from va_sim.stripe.form import FormError, decode_form
from va_sim.stripe.objects import DEFAULT_API_VERSION
from va_sim.stripe.params import Params
from va_sim.stripe.store import IdempotencyEntry, StripeStore

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["Handler", "StripeCall", "stripe_endpoint"]

_REQUEST_ID_LENGTH: Final = 14
_IGNORED_PARAMS: Final = frozenset({"expand"})
_MISSING_KEY: Final = (
    "You did not provide an API key. You need to provide your API key in the Authorization header, using "
    "Bearer auth (e.g. 'Authorization: Bearer YOUR_SECRET_KEY'). See "
    "https://stripe.com/docs/api#authentication for details, or we can help at https://support.stripe.com/."
)
_KEY_MISMATCH: Final = (
    "Keys for idempotent requests can only be used with the same parameters they were first used with. Try "
    "using a key other than '{key}' if you meant to execute a different request."
)
_KEY_IN_USE: Final = (
    "There is currently another in-progress request using this Idempotency-Key (that probably means you "
    "submitted twice, and the other request is still going through): {key}. Please try again later."
)
_VISIBLE_KEY_CHARS: Final = 4


@dataclass(frozen=True, slots=True)
class StripeCall:
    """One authenticated, decoded request as a handler sees it."""

    state: SimState
    method: str
    path: str
    params: Params
    path_params: Mapping[str, str]
    request_id: str
    idempotency_key: str | None
    api_version: str
    base_url: str

    @property
    def store(self) -> StripeStore:
        return self.state.stripe

    @property
    def now(self) -> int:
        """Sim-clock unix seconds (``created`` of new objects)."""
        return int(self.state.clock.now().timestamp())


type Handler = Callable[[StripeCall], Awaitable[dict[str, Any]]]


def _presented_key(request: Request) -> str:
    header = request.headers.get("authorization", "").strip()
    scheme, _, credentials = header.partition(" ")
    if scheme.lower() == "bearer":
        return credentials.strip()
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(credentials.strip(), validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            return ""
        return decoded.partition(":")[0]
    return ""


def _auth_error(key: str, store: StripeStore) -> StripeError | None:
    if not key:
        return StripeError(401, "invalid_request_error", _MISSING_KEY)
    if store.accepts(key):
        return None
    prefix = key.split("_", 2)
    shown = "_".join(prefix[:2]) + "_" if len(prefix) > 2 else ""  # noqa: PLR2004 - "sk_test_..." parts
    masked = f"{shown}{'*' * 4}{key[-_VISIBLE_KEY_CHARS:]}"
    return StripeError(401, "invalid_request_error", f"Invalid API Key provided: {masked}")


async def _decoded_params(request: Request) -> dict[str, Any]:
    # GET reads the query string; DELETE reads both (the SDKs send its parameters as a query).
    pairs = request.query_params.multi_items() if request.method in {"GET", "DELETE"} else []
    if request.method != "GET":
        raw = await request.body()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            msg = "The request body is not valid UTF-8."
            raise invalid_request(msg) from exc
        pairs += parse_qsl(text, keep_blank_values=True)
    try:
        decoded = decode_form(pairs)
    except FormError as exc:
        raise invalid_request(exc.message, param=exc.param) from exc
    return {key: value for key, value in decoded.items() if key not in _IGNORED_PARAMS}


def _answer(status: int, body: Mapping[str, Any], call_headers: Mapping[str, str]) -> JSONResponse:
    return JSONResponse(dict(body), status_code=status, headers=dict(call_headers))


def _fingerprint(method: str, path: str, params: Params) -> bytes:
    return orjson.dumps([method, path, params], option=orjson.OPT_SORT_KEYS)


def stripe_endpoint(state: SimState, handler: Handler) -> Callable[[Request], Awaitable[Response]]:
    """A Starlette endpoint running ``handler`` through the FakeStripe pipeline."""

    async def endpoint(request: Request) -> Response:
        request_id = f"req_{state.ids.alnum(_REQUEST_ID_LENGTH)}"
        api_version = request.headers.get("stripe-version") or DEFAULT_API_VERSION
        headers = {"request-id": request_id, "stripe-version": api_version}
        key = _presented_key(request)
        denied = _auth_error(key, state.stripe)
        if denied is not None:
            return _answer(denied.status, denied.body(), headers)
        try:
            params = await _decoded_params(request)
        except StripeError as exc:
            return _answer(exc.status, exc.body(), headers)
        idempotency_key = request.headers.get("idempotency-key") if request.method == "POST" else None
        call = StripeCall(
            state=state,
            method=request.method,
            path=request.url.path,
            params=params,
            path_params=dict(request.path_params),
            request_id=request_id,
            idempotency_key=idempotency_key,
            api_version=api_version,
            base_url=str(request.base_url).rstrip("/"),
        )
        if idempotency_key is None:
            status, body = await _run(handler, call)
            return _answer(status, body, headers)
        return await _idempotent(call, key, handler, headers)

    return endpoint


async def _run(handler: Handler, call: StripeCall) -> tuple[int, dict[str, Any]]:
    try:
        return 200, await handler(call)
    except StripeError as exc:
        return exc.status, exc.body()


async def _idempotent(
    call: StripeCall, api_key: str, handler: Handler, headers: dict[str, str]
) -> JSONResponse:
    assert call.idempotency_key is not None  # noqa: S101 - narrowed by the caller
    slot = (api_key, call.idempotency_key)
    fingerprint = _fingerprint(call.method, call.path, call.params)
    stored = call.store.idempotency.get(slot)
    if stored is not None:
        if stored.fingerprint != fingerprint:
            error = StripeError(400, "idempotency_error", _KEY_MISMATCH.format(key=call.idempotency_key))
            return _answer(error.status, error.body(), headers)
        if stored.status is None:
            error = StripeError(
                409,
                "idempotency_error",
                _KEY_IN_USE.format(key=call.idempotency_key),
                code="idempotency_key_in_use",
            )
            return _answer(error.status, error.body(), headers)
        replay_headers = {
            **headers,
            "idempotent-replayed": "true",
            "original-request": stored.request_id,
            "idempotency-key": call.idempotency_key,
        }
        return _answer(stored.status, stored.body, replay_headers)
    entry = IdempotencyEntry(fingerprint=fingerprint, request_id=call.request_id)
    call.store.idempotency[slot] = entry
    try:
        status, body = await _run(handler, call)
    except BaseException:
        # An unexpected crash (or cancellation) must not leave the key locked forever.
        call.store.idempotency.pop(slot, None)
        raise
    entry.status = status
    entry.body = body
    return _answer(status, body, {**headers, "idempotency-key": call.idempotency_key})
