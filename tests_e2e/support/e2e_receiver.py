"""The e2e webhook receiver (specs/30 §5 Decision P06-T08): a real uvicorn server in the pytest process
that the stack's containers reach as ``http://{E2E_RECEIVER_HOST}:{port}`` (default ``host.docker.internal``,
mapped to the docker host by ``extra_hosts`` in ``docker-compose.e2e.yml``).

Requests are recorded with raw body, lower-cased headers, monotonic and wall receipt times, and the status
they were answered with. Replies are scripted per path, optionally per event type (FIFO, then 200).
Signature checks (:func:`signature_failures`) are independent of ``va_events.webhook_signing``: they
recompute every HMAC here.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

from service_helpers import serving
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

__all__ = [
    "BLAND_SIGNATURE_HEADER",
    "EVENT_ID_HEADER",
    "EVENT_TYPE_HEADER",
    "SIGNATURE_HEADER",
    "SIGNATURE_TOLERANCE_S",
    "TIMESTAMP_HEADER",
    "USER_AGENT",
    "Received",
    "ReceiverSettings",
    "Reply",
    "ServedReceiver",
    "WebhookReceiver",
    "hex_hmac",
    "parse_signature_header",
    "served_receiver",
    "signature_failures",
]

SIGNATURE_HEADER: Final = "x-va-signature"
BLAND_SIGNATURE_HEADER: Final = "x-webhook-signature"
TIMESTAMP_HEADER: Final = "x-va-timestamp"
EVENT_ID_HEADER: Final = "x-va-event-id"
BLAND_EVENT_ID_HEADER: Final = "x-event-id"
EVENT_TYPE_HEADER: Final = "x-va-event-type"
USER_AGENT: Final = "VoiceAgents-Webhooks/1.0"
EVENT_ID_PREFIX: Final = "evt_"
SIGNATURE_TOLERANCE_S: Final = 300.0
DEFAULT_BIND: Final = "0.0.0.0"  # noqa: S104 - the containers reach the receiver through the docker host
DEFAULT_PUBLIC_HOST: Final = "host.docker.internal"


@dataclass(frozen=True, slots=True)
class Reply:
    status: int = 200


@dataclass(frozen=True, slots=True)
class Received:
    """One request: path, lower-cased headers, raw body, receipt times, and the status answered."""

    path: str
    headers: dict[str, str]
    body: bytes
    at: float
    wall: float
    status: int

    @property
    def event_type(self) -> str:
        return self.headers.get(EVENT_TYPE_HEADER, "")

    @property
    def event_id(self) -> str:
        return self.headers.get(EVENT_ID_HEADER, "")


class WebhookReceiver:
    """Records every request; replies scripted per path and event type (FIFO), else 200."""

    def __init__(self) -> None:
        self.scripts: dict[tuple[str, str | None], list[Reply]] = {}
        self.requests: list[Received] = []
        self.changed = asyncio.Event()

    def script(self, path: str, *replies: Reply, event_type: str | None = None) -> None:
        """Answer the next requests to ``path`` (of ``event_type`` only, when given) with ``replies``."""
        self.scripts.setdefault((path, event_type), []).extend(replies)

    def _reply(self, path: str, event_type: str) -> Reply:
        for key in ((path, event_type), (path, None)):
            queue = self.scripts.get(key)
            if queue:
                return queue.pop(0)
        return Reply()

    def of(self, path: str, event_type: str | None = None) -> list[Received]:
        """The requests to ``path`` (of ``event_type`` only, when given), in arrival order."""
        return [
            request
            for request in self.requests
            if request.path == path and (event_type is None or request.event_type == event_type)
        ]

    async def wait_for(self, predicate: Callable[[], bool], *, timeout_s: float) -> None:
        """Wait (event-driven) until ``predicate()`` holds after a request; AssertionError on timeout."""
        try:
            async with asyncio.timeout(timeout_s):
                while not predicate():
                    self.changed.clear()
                    await self.changed.wait()
        except TimeoutError:
            seen = [(request.path, request.event_type, request.status) for request in self.requests]
            message = f"receiver condition not met within {timeout_s} s; received {seen}"
            raise AssertionError(message) from None

    async def handle(self, request: Request) -> Response:
        body = await request.body()
        headers = {key.lower(): value for key, value in request.headers.items()}
        reply = self._reply(request.url.path, headers.get(EVENT_TYPE_HEADER, ""))
        self.requests.append(
            Received(request.url.path, headers, body, time.monotonic(), time.time(), reply.status)
        )
        self.changed.set()
        return Response(status_code=reply.status)


@dataclass(frozen=True, slots=True)
class ReceiverSettings:
    """``E2E_RECEIVER_BIND`` / ``E2E_RECEIVER_PORT`` / ``E2E_RECEIVER_HOST``."""

    bind: str = DEFAULT_BIND
    port: int = 0
    public_host: str = DEFAULT_PUBLIC_HOST

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> ReceiverSettings:
        return cls(
            bind=environ.get("E2E_RECEIVER_BIND", DEFAULT_BIND),
            port=int(environ.get("E2E_RECEIVER_PORT", "0")),
            public_host=environ.get("E2E_RECEIVER_HOST", DEFAULT_PUBLIC_HOST),
        )

    def url(self, port: int) -> str:
        """The receiver's base URL as the containers see it."""
        return f"http://{self.public_host}:{port}"


@dataclass(frozen=True, slots=True)
class ServedReceiver:
    """The running webhook receiver and its base URL as the stack's containers reach it."""

    receiver: WebhookReceiver
    url: str


@asynccontextmanager
async def served_receiver(receiver: WebhookReceiver, *, bind: str, port: int = 0) -> AsyncIterator[int]:
    """Serve ``receiver`` with real uvicorn on ``bind``:``port`` (0 = ephemeral); yields the bound port."""
    app = Starlette(routes=[Route("/{path:path}", receiver.handle, methods=["POST"])])
    async with serving(app, port=port, host=bind) as server:
        yield int(server.url.rsplit(":", 1)[1])


def hex_hmac(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


@dataclass(frozen=True, slots=True)
class ParsedSignature:
    timestamp: str
    versions: list[str] = field(default_factory=list)


def parse_signature_header(value: str) -> ParsedSignature | None:
    """``t=<unix>,v1=<hex>[,v1=<hex>...]`` → its parts in order; ``None`` when malformed."""
    timestamp: str | None = None
    versions: list[str] = []
    for item in value.split(","):
        key, sep, part = item.strip().partition("=")
        if not sep or not part:
            return None
        if key == "t" and timestamp is None:
            timestamp = part
        elif key == "v1":
            versions.append(part)
        else:
            return None
    if timestamp is None or not timestamp.isdigit() or not versions:
        return None
    return ParsedSignature(timestamp, versions)


def _identity_failures(headers: Mapping[str, str]) -> list[str]:
    failures: list[str] = []
    event_id = headers.get(EVENT_ID_HEADER, "")
    if not event_id.startswith(EVENT_ID_PREFIX):
        failures.append(f"{EVENT_ID_HEADER} {event_id!r} does not start with {EVENT_ID_PREFIX}")
    if headers.get(BLAND_EVENT_ID_HEADER) != event_id:
        failures.append(f"{BLAND_EVENT_ID_HEADER} differs from {EVENT_ID_HEADER}")
    if headers.get("user-agent") != USER_AGENT:
        failures.append(f"user-agent {headers.get('user-agent')!r}")
    if not headers.get("content-type", "").startswith("application/json"):
        failures.append(f"content-type {headers.get('content-type')!r}")
    if not headers.get(EVENT_TYPE_HEADER):
        failures.append(f"{EVENT_TYPE_HEADER} missing")
    return failures


def signature_failures(
    received: Received, secret: str, *, now: float, tolerance_s: float = SIGNATURE_TOLERANCE_S
) -> list[str]:
    """Why ``received`` does not verify with ``secret`` (empty = it does).

    ``X-VA-Signature`` holds one ``v1`` per active secret (current first); any match verifies. ``t`` must
    equal ``X-VA-Timestamp`` and lie within ``tolerance_s`` of ``now``; ``X-Webhook-Signature`` is the HMAC
    of the raw body with the current secret.
    """
    headers = received.headers
    failures = _identity_failures(headers)
    parsed = parse_signature_header(headers.get(SIGNATURE_HEADER, ""))
    if parsed is None:
        return [*failures, f"{SIGNATURE_HEADER} missing or malformed"]
    if parsed.timestamp != headers.get(TIMESTAMP_HEADER):
        failures.append(f"t={parsed.timestamp} differs from {TIMESTAMP_HEADER}")
    if abs(now - int(parsed.timestamp)) > tolerance_s:
        failures.append(f"t={parsed.timestamp} is more than {tolerance_s} s from {now:.0f}")
    expected = hex_hmac(secret, f"{parsed.timestamp}.".encode() + received.body)
    if not any(hmac.compare_digest(version, expected) for version in parsed.versions):
        failures.append(f"no v1 in {SIGNATURE_HEADER} matches the secret")
    bland = headers.get(BLAND_SIGNATURE_HEADER, "")
    if not hmac.compare_digest(bland, hex_hmac(secret, received.body)):
        failures.append(f"{BLAND_SIGNATURE_HEADER} does not match the secret")
    return failures
