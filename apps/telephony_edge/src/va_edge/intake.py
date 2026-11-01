"""Telnyx webhook intake (specs/06-telephony-telnyx.md §4, §4.1; Decision P05-T01).

``POST /telnyx/voice``, ``/telnyx/messaging`` (platform key) and ``/telnyx/voice/{telnyx_account_id}``,
``/telnyx/messaging/{telnyx_account_id}`` (BYO account key). Steps: refuse a body over ``MAX_BODY_BYTES``
before buffering it (``413``), refuse while shutting down (``503``),
select the key from the route, verify the signature on the raw bytes (``401``), parse the envelope (``400``),
attribute the event (org, call, call control id), insert it into ``telnyx_events`` with dedupe on
``telnyx_event_id`` (a duplicate answers ``200`` at once), offer it to the processor, and answer ``200``.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal

import orjson
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from va_common.logging import get_logger
from va_common.metrics import ServiceMetrics
from va_common.time import Clock
from va_db.repositories import NewTelnyxEvent, TelnyxAccountLookupRepo, TelnyxEventRepo, TelnyxWebhookKey
from va_db.session import Database
from va_edge.keys import KeyLookup, UnknownAccountError, WebhookKeys
from va_edge.processor import WEBHOOKS_TOTAL, EventProcessor, StoredEvent
from va_telnyx.client_state import ClientState, ClientStateError, decode
from va_telnyx.signatures import WebhookVerificationError, verify_webhook
from va_telnyx.webhooks import WebhookEvent, WebhookParseError, parse_envelope

__all__ = [
    "CHANNELS",
    "INVALID_EVENT_TYPE",
    "MAX_BODY_BYTES",
    "UNVERIFIED_EVENT_TYPE",
    "Channel",
    "Reply",
    "WebhookIntake",
    "attribute",
    "db_key_lookup",
    "read_capped",
    "telnyx_routes",
]

type Channel = Literal["voice", "messaging"]

CHANNELS: Final[tuple[Channel, ...]] = ("voice", "messaging")
UNVERIFIED_EVENT_TYPE: Final = "unverified"
INVALID_EVENT_TYPE: Final = "invalid"
MAX_BODY_BYTES: Final = 1024 * 1024

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    body: dict[str, str]


_OK: Final = Reply(200, {"status": "ok"})
_DUPLICATE: Final = Reply(200, {"status": "duplicate"})
_UNAUTHORIZED: Final = Reply(401, {"error": "invalid_signature"})
_INVALID: Final = Reply(400, {"error": "invalid_payload"})
_UNAVAILABLE: Final = Reply(503, {"error": "unavailable"})
_TOO_LARGE: Final = Reply(413, {"error": "payload_too_large"})


def _text(payload: Mapping[str, Any], name: str) -> str | None:
    value = payload.get(name)
    return value if isinstance(value, str) and value else None


def _client_state(payload: Mapping[str, Any]) -> ClientState | None:
    value = _text(payload, "client_state")
    if value is None:
        return None
    try:
        return decode(value)
    except ClientStateError:
        return None


def attribute(event: WebhookEvent, body: dict[str, Any], account_org: uuid.UUID | None) -> NewTelnyxEvent:
    """The row of a verified event. A BYO route's account owns the event; elsewhere ``client_state`` names the
    org. A ``client_state`` of another org on a BYO route is ignored (its call id too)."""
    state = _client_state(event.payload)
    if state is not None and account_org is not None and state.org_id != account_org:
        state = None
    org_id = account_org if account_org is not None else (state.org_id if state is not None else None)
    return NewTelnyxEvent(
        telnyx_event_id=event.id,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        payload=body,
        org_id=org_id,
        call_id=state.call_id if state is not None else None,
        call_control_id=_text(event.payload, "call_control_id"),
    )


def db_key_lookup(database: Database) -> KeyLookup:
    """``WebhookKeys`` lookup through ``TelnyxAccountLookupRepo`` (system scope, specs/03 §2)."""

    async def lookup(account_id: uuid.UUID) -> TelnyxWebhookKey | None:
        async with database.system_session() as session:
            return await TelnyxAccountLookupRepo(session).webhook_key(account_id)

    return lookup


class WebhookIntake:
    """Verifies, stores, and queues inbound Telnyx webhooks."""

    def __init__(
        self,
        database: Database,
        keys: WebhookKeys,
        processor: EventProcessor,
        *,
        clock: Clock,
        metrics: ServiceMetrics,
    ) -> None:
        self._database = database
        self._keys = keys
        self._processor = processor
        self._clock = clock
        self._total = metrics.counter(WEBHOOKS_TOTAL)

    def too_large(self, channel: Channel) -> Reply:
        """The answer to a body over ``MAX_BODY_BYTES`` (nothing read past the cap, nothing stored)."""
        _log.warning("telnyx.webhook_too_large", channel=channel)
        self._total.labels(event_type=UNVERIFIED_EVENT_TYPE, result="error").inc()
        return _TOO_LARGE

    async def receive(
        self, raw: bytes, headers: Mapping[str, str], channel: Channel, account_id: str | None
    ) -> Reply:
        if self._processor.closed:
            return _UNAVAILABLE
        try:
            key = await self._keys.resolve(account_id)
            verify_webhook(raw, headers, key.key, self._clock.now())
        except (UnknownAccountError, WebhookVerificationError) as exc:
            reason = exc.reason if isinstance(exc, WebhookVerificationError) else "unknown_account"
            _log.warning("telnyx.webhook_rejected", channel=channel, reason=reason)
            self._total.labels(event_type=UNVERIFIED_EVENT_TYPE, result="invalid_signature").inc()
            return _UNAUTHORIZED
        except Exception as exc:  # the key lookup's database is down: Telnyx retries
            _log.error("telnyx.webhook_unavailable", channel=channel, error_type=type(exc).__name__)
            return _UNAVAILABLE
        try:
            envelope = parse_envelope(raw)
        except WebhookParseError:
            _log.warning("telnyx.webhook_invalid", channel=channel)
            self._total.labels(event_type=INVALID_EVENT_TYPE, result="error").inc()
            return _INVALID
        return await self._store(attribute(envelope.data, orjson.loads(raw), key.org_id), channel)

    async def _store(self, event: NewTelnyxEvent, channel: Channel) -> Reply:
        try:
            async with self._database.system_session() as session:
                row = await TelnyxEventRepo(session).record(event)
                stored = StoredEvent.from_row(row) if row is not None else None
        except Exception as exc:  # nothing was stored: Telnyx retries the delivery
            _log.error(
                "telnyx.webhook_store_failed",
                channel=channel,
                event_type=event.event_type,
                error_type=type(exc).__name__,
            )
            return _UNAVAILABLE
        if stored is None:
            self._total.labels(event_type=event.event_type, result="duplicate").inc()
            return _DUPLICATE
        self._processor.offer(stored)
        return _OK


def _intake(request: Request) -> WebhookIntake:
    intake: WebhookIntake = request.app.state.intake
    return intake


async def read_capped(request: Request, limit: int = MAX_BODY_BYTES) -> bytes | None:
    """The raw body, or ``None`` once it exceeds ``limit`` bytes: a larger ``Content-Length`` is refused
    before reading, and a chunked or understated body stops being read at the first chunk past the cap."""
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit:
            return None
    return bytes(body)


def _endpoint(channel: Channel, *, byo: bool) -> Callable[[Request], Awaitable[Response]]:
    async def endpoint(request: Request) -> Response:
        account_id = request.path_params["telnyx_account_id"] if byo else None
        intake = _intake(request)
        raw = await read_capped(request)
        if raw is None:
            reply = intake.too_large(channel)
        else:
            reply = await intake.receive(raw, request.headers, channel, account_id)
        return JSONResponse(reply.body, status_code=reply.status)

    return endpoint


def telnyx_routes() -> list[Route]:
    """The four webhook routes of specs/06 §4."""
    routes: list[Route] = []
    for channel in CHANNELS:
        routes.append(Route(f"/telnyx/{channel}", _endpoint(channel, byo=False), methods=["POST"]))
        routes.append(
            Route(f"/telnyx/{channel}/{{telnyx_account_id}}", _endpoint(channel, byo=True), methods=["POST"])
        )
    return routes
