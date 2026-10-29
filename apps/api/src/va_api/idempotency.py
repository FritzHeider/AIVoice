"""``Idempotency-Key`` on POST requests (specs/04-auth-tenancy-rbac.md §12, specs/05-calls-api.md headers).

The claim is taken by :func:`claim_idempotency`, which ``require_scope`` calls once the principal passed
authentication, the ``default`` rate bucket, and the scope check (the org id is the key space). A repeated
request is answered from the stored response by raising :class:`IdempotentReplay`, rendered by the handler
:func:`install_idempotency` registers. :class:`IdempotencyMiddleware` sees the claim in the ASGI scope and,
before the last body chunk leaves, stores the response (2xx and 4xx JSON except 429) or releases the claim
(5xx, 429, non-JSON bodies, unhandled exceptions), so a retry after a failure runs the request again.

This module must not import ``va_api.auth`` at runtime (``va_api.auth.dependencies`` imports it).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Protocol

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError
from starlette.types import Message, Receive, Scope, Send

from va_api.middleware import HttpMiddleware
from va_common.errors import AppError
from va_common.logging import get_logger
from va_common.time import Clock
from va_db.repositories import ClaimOutcome, IdempotencyConflict, IdempotencyReplay, IdempotencyRepo
from va_db.session import Database

if TYPE_CHECKING:
    # Type-only: importing va_api.auth at runtime would cycle (va_api.auth.dependencies imports this module).
    from va_api.auth.context import RequestContext

__all__ = [
    "IDEMPOTENCY_HEADER",
    "IDEMPOTENCY_SCOPE_KEY",
    "MAX_KEY_LENGTH",
    "REPLAYED_HEADER",
    "ActiveClaim",
    "DbIdempotencyStore",
    "IdempotencyMiddleware",
    "IdempotencyStore",
    "IdempotentReplay",
    "canonical_body",
    "claim_idempotency",
    "idempotency_of",
    "install_idempotency",
    "is_storable_status",
    "render_replay",
    "request_hash",
    "validate_key",
]

IDEMPOTENCY_HEADER: Final = "idempotency-key"
REPLAYED_HEADER: Final = "Idempotent-Replayed"
IDEMPOTENCY_SCOPE_KEY: Final = "va.idempotency"
MAX_KEY_LENGTH: Final = 255
INVALID_KEY_MESSAGE: Final = "Idempotency-Key must be 1-255 printable ASCII characters."
CONFLICT_MESSAGES: Final = {
    IdempotencyConflict.KEY_REUSED: "Idempotency-Key was already used with a different request.",
    IdempotencyConflict.IN_PROGRESS: "A request with this Idempotency-Key is still in progress.",
}
# Bodies above this size are parsed off the event loop (MASTER_PROMPT rule 7).
THREAD_PARSE_BYTES: Final = 64 * 1024
_POST: Final = "POST"
_PRINTABLE: Final = range(0x20, 0x7F)
_TOO_MANY_REQUESTS: Final = 429
_STORE_ERRORS: Final = (SQLAlchemyError, OSError)

_log = get_logger(__name__)


class IdempotencyStore(Protocol):
    """Claims and stored responses per ``(org_id, key)``, with the semantics of :class:`IdempotencyRepo`."""

    async def claim(
        self, org_id: uuid.UUID, *, key: str, method: str, path: str, request_hash: str
    ) -> ClaimOutcome:
        """Claim ``key`` for the request, or the stored response, or the conflict."""
        ...

    async def complete(self, org_id: uuid.UUID, key: str, *, status: int, body: Any) -> None:
        """Store the response of a claimed request."""
        ...

    async def release(self, org_id: uuid.UUID, key: str) -> None:
        """Drop the claim of a request whose response is not stored."""
        ...


class DbIdempotencyStore:
    """:class:`IdempotencyRepo` in one ``tenant_session`` per call (a claim is committed at once)."""

    def __init__(self, database: Database, clock: Clock) -> None:
        self._database = database
        self._clock = clock

    async def claim(
        self, org_id: uuid.UUID, *, key: str, method: str, path: str, request_hash: str
    ) -> ClaimOutcome:
        async with self._database.tenant_session(org_id) as session:
            return await IdempotencyRepo(session, org_id).claim(
                key=key, method=method, path=path, request_hash=request_hash, now=self._clock.now()
            )

    async def complete(self, org_id: uuid.UUID, key: str, *, status: int, body: Any) -> None:
        async with self._database.tenant_session(org_id) as session:
            await IdempotencyRepo(session, org_id).complete(
                key, status=status, body=body, now=self._clock.now()
            )

    async def release(self, org_id: uuid.UUID, key: str) -> None:
        async with self._database.tenant_session(org_id) as session:
            await IdempotencyRepo(session, org_id).release(key)


def validate_key(raw: str) -> str:
    """``raw`` when it is 1-255 printable ASCII characters (0x20-0x7E), else ``INVALID_INPUT``."""
    if not 1 <= len(raw) <= MAX_KEY_LENGTH or any(ord(char) not in _PRINTABLE for char in raw):
        raise AppError("INVALID_INPUT", INVALID_KEY_MESSAGE, field="Idempotency-Key")
    return raw


def canonical_body(body: bytes) -> bytes:
    """Sorted-key compact JSON of a JSON body (key order and whitespace do not matter), else ``body``."""
    try:
        parsed = json.loads(body)
    except ValueError:
        return body
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _hash(method: str, path: str, body: bytes) -> str:
    digest = hashlib.sha256(f"{method}\n{path}\n".encode())
    digest.update(canonical_body(body))
    return digest.hexdigest()


async def request_hash(method: str, path: str, body: bytes) -> str:
    """``sha256(method + "\\n" + path + "\\n" + canonical body)`` in hex (the query string is excluded)."""
    if len(body) > THREAD_PARSE_BYTES:
        return await asyncio.to_thread(_hash, method, path, body)
    return _hash(method, path, body)


class IdempotentReplay(Exception):  # noqa: N818 - a control-flow signal rendered as a response, not an error
    """Raised by :func:`claim_idempotency` to answer with a stored response."""

    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"replay of a stored {status} response")
        self.status = status
        self.body = body


def render_replay(request: Request, exc: IdempotentReplay) -> Response:
    """The stored status and JSON body plus ``Idempotent-Replayed: true``; counts the replay."""
    request.app.state.metrics.counter("va_idempotent_replays_total").inc()
    return JSONResponse(exc.body, status_code=exc.status, headers={REPLAYED_HEADER: "true"})


async def _handle_replay(request: Request, exc: Exception) -> Response:
    if not isinstance(exc, IdempotentReplay):  # Starlette dispatches by type; this only narrows for mypy
        raise exc
    return render_replay(request, exc)


@dataclass(frozen=True, slots=True)
class ActiveClaim:
    """The claim the current request holds, kept in the ASGI scope for :class:`IdempotencyMiddleware`."""

    org_id: uuid.UUID
    key: str
    store: IdempotencyStore


def idempotency_of(request: Request) -> IdempotencyStore:
    """The idempotency store of the container the lifespan stored on ``app.state``."""
    store: IdempotencyStore = request.app.state.container.idempotency
    return store


async def claim_idempotency(request: Request, ctx: RequestContext) -> None:
    """Claim the request's ``Idempotency-Key`` for ``ctx.org_id`` (POST only, once per request).

    Raises :class:`IdempotentReplay` for a stored response, ``IDEMPOTENCY_KEY_REUSED`` /
    ``IDEMPOTENCY_IN_PROGRESS`` for conflicts, and ``INVALID_INPUT`` for a malformed key.
    """
    raw = request.headers.get(IDEMPOTENCY_HEADER)
    if request.method != _POST or raw is None or IDEMPOTENCY_SCOPE_KEY in request.scope:
        return
    key = validate_key(raw)
    path = str(request.scope["path"])
    digest = await request_hash(request.method, path, await request.body())
    store = idempotency_of(request)
    outcome = await store.claim(ctx.org_id, key=key, method=request.method, path=path, request_hash=digest)
    if isinstance(outcome, IdempotencyReplay):
        raise IdempotentReplay(outcome.status, outcome.body)
    if isinstance(outcome, IdempotencyConflict):
        raise AppError(outcome.value, CONFLICT_MESSAGES[outcome])
    request.scope[IDEMPOTENCY_SCOPE_KEY] = ActiveClaim(ctx.org_id, key, store)


def is_storable_status(status: int) -> bool:
    """2xx and 4xx responses are stored, except 429 (a throttle the client must be able to retry)."""
    return (200 <= status < 300 or 400 <= status < 500) and status != _TOO_MANY_REQUESTS  # noqa: PLR2004


def _parse_json(body: bytes) -> tuple[bool, Any]:
    try:
        return True, json.loads(body)
    except ValueError:
        return False, None


async def _stored_body(body: bytes) -> tuple[bool, Any]:
    if not body:
        return False, None
    if len(body) > THREAD_PARSE_BYTES:
        return await asyncio.to_thread(_parse_json, body)
    return _parse_json(body)


@dataclass(slots=True)
class _Capture:
    """Buffers the response of a claimed request and settles the claim before its last chunk is sent."""

    scope: Scope
    send: Send
    claim: ActiveClaim | None = None
    status: int = 0
    chunks: list[bytes] = field(default_factory=list)
    settled: bool = False

    async def forward(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            claim = self.scope.get(IDEMPOTENCY_SCOPE_KEY)
            self.claim = claim if isinstance(claim, ActiveClaim) else None
            self.status = int(message["status"])
        elif message["type"] == "http.response.body" and self.claim is not None and not self.settled:
            self.chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                await self._settle(self.claim)
        await self.send(message)

    async def _settle(self, claim: ActiveClaim) -> None:
        self.settled = True
        parsed, body = (False, None)
        if is_storable_status(self.status):
            parsed, body = await _stored_body(b"".join(self.chunks))
        if not parsed:
            await self._release(claim)
            return
        try:
            await claim.store.complete(claim.org_id, claim.key, status=self.status, body=body)
        except _STORE_ERRORS as exc:
            _log.warning(
                "idempotency_store_failed",
                operation="complete",
                org_id=str(claim.org_id),
                status=self.status,
                error=type(exc).__name__,
            )
            await self._release(claim)

    async def abandon(self) -> None:
        """Release the claim of a request that raised before its response was settled."""
        claim = self.scope.get(IDEMPOTENCY_SCOPE_KEY)
        if not self.settled and isinstance(claim, ActiveClaim):
            self.settled = True
            await self._release(claim)

    async def _release(self, claim: ActiveClaim) -> None:
        try:
            await claim.store.release(claim.org_id, claim.key)
        except _STORE_ERRORS as exc:
            _log.warning(
                "idempotency_store_failed",
                operation="release",
                org_id=str(claim.org_id),
                status=self.status,
                error=type(exc).__name__,
            )


class IdempotencyMiddleware(HttpMiddleware):
    """Stores or releases the idempotency claim of each request as its response leaves (pure ASGI)."""

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        capture = _Capture(scope, send)
        try:
            await self.app(scope, receive, capture.forward)
        except Exception:
            await capture.abandon()
            raise


def install_idempotency(app: FastAPI) -> None:
    """Register the replay handler (the middleware is added by ``create_app`` for its position)."""
    app.add_exception_handler(IdempotentReplay, _handle_replay)
