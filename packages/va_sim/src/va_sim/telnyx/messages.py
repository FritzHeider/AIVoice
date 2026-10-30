"""FakeTelnyx messaging (specs/30-testing-and-evals.md §3.2 ``POST /v2/messages``; specs/21-messaging-sms.md
§5).

An outbound message is answered ``queued``; 200 ms later (sim clock) ``message.sent`` is emitted and 400 ms
after that ``message.finalized`` with ``delivered`` (or ``delivery_failed`` for destinations listed in the
telnyx config ``failing_message_destinations``). Inbound messages are injected through the control API
(``message.received``). Segments are counted here, independently of the platform: GSM-7 (extension characters
count twice) 160/153 septets per part, otherwise UCS-2 70/67 UTF-16 code units per part.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final, Literal

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.clock import iso_z
from va_sim.telnyx.errors import invalid, invalid_from, not_found
from va_sim.telnyx.http import authenticated, data, parse_body
from va_sim.telnyx.models import MessageCreate
from va_sim.telnyx.store import Account, Record

if TYPE_CHECKING:
    from va_sim.settings import WebhookDefaults
    from va_sim.state import SimState

__all__ = [
    "FINALIZED_AFTER_S",
    "SENT_AFTER_S",
    "MessageParts",
    "delivery_failed_error",
    "message_record",
    "message_routes",
    "segment_count",
    "webhook_target",
]

SENT_AFTER_S: Final = 0.2
FINALIZED_AFTER_S: Final = 0.4
_VALIDITY: Final = timedelta(hours=24)

_GSM_BASIC: Final = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM_EXTENSION: Final = frozenset("\f^{}\\[~]|€")
_GSM_SINGLE, _GSM_MULTI = 160, 153
_UCS2_SINGLE, _UCS2_MULTI = 70, 67

type Encoding = Literal["GSM-7", "UCS-2"]


@dataclass(frozen=True, slots=True)
class MessageParts:
    encoding: Encoding
    parts: int


def _parts(units: int, single: int, multi: int) -> int:
    return 1 if units <= single else math.ceil(units / multi)


def segment_count(text: str) -> MessageParts:
    """The encoding and number of parts Telnyx would bill for ``text``."""
    if all(char in _GSM_BASIC or char in _GSM_EXTENSION for char in text):
        septets = sum(2 if char in _GSM_EXTENSION else 1 for char in text)
        return MessageParts("GSM-7", _parts(septets, _GSM_SINGLE, _GSM_MULTI))
    code_units = len(text.encode("utf-16-le")) // 2
    return MessageParts("UCS-2", _parts(code_units, _UCS2_SINGLE, _UCS2_MULTI))


def webhook_target(body: MessageCreate, profile: Record, defaults: WebhookDefaults) -> str:
    """Where the events of an outbound message go (specs/30 §3.2 Decision 2026-10-29, rule 3)."""
    profile_url: str | None = profile.get("webhook_url")
    if body.use_profile_webhooks and profile_url:
        return profile_url
    return body.webhook_url or defaults.messaging_url


def delivery_failed_error() -> dict[str, str]:
    return {
        "code": "40008",
        "title": "Undeliverable",
        "detail": "The destination carrier rejected the message.",
    }


def _party(phone_number: str, carrier: str) -> dict[str, str]:
    return {"phone_number": phone_number, "carrier": carrier, "line_type": "Wireless"}


def message_record(  # noqa: PLR0913 - every field of the Telnyx message record
    *,
    message_id: str,
    direction: Literal["inbound", "outbound"],
    from_number: str,
    to_number: str,
    to_status: str,
    text: str,
    media_urls: list[str],
    subject: str | None,
    messaging_profile_id: str | None,
    webhook_url: str | None,
    webhook_failover_url: str | None,
    received_at: str,
    valid_until: str | None,
) -> Record:
    """A Telnyx ``message`` record."""
    outbound = direction == "outbound"
    parts = segment_count(text)
    return {
        "record_type": "message",
        "direction": direction,
        "id": message_id,
        "type": "MMS" if media_urls else "SMS",
        "messaging_profile_id": messaging_profile_id,
        "organization_id": None,
        "from": _party(from_number, "Telnyx" if outbound else "SIM Wireless"),
        "to": [{**_party(to_number, "SIM Wireless" if outbound else "Telnyx"), "status": to_status}],
        "cc": [],
        "text": text,
        "subject": subject,
        "media": [{"url": url, "content_type": None, "sha256": None, "size": None} for url in media_urls],
        "webhook_url": webhook_url,
        "webhook_failover_url": webhook_failover_url,
        "encoding": parts.encoding,
        "parts": parts.parts,
        "tags": [],
        "cost": None,
        "received_at": received_at,
        "sent_at": None,
        "completed_at": None,
        "valid_until": valid_until,
        "errors": [],
    }


def message_routes(state: SimState) -> list[BaseRoute]:
    """``POST /v2/messages`` and ``GET /v2/messages/{id}``."""
    store = state.telnyx

    async def lifecycle(record: Record, url: str, *, fails: bool) -> None:
        await state.clock.sleep(SENT_AFTER_S)
        record["to"][0]["status"] = "sent"
        record["sent_at"] = iso_z(state.clock.now())
        await state.webhooks.emit(url, "message.sent", record)
        await state.clock.sleep(FINALIZED_AFTER_S)
        record["to"][0]["status"] = "delivery_failed" if fails else "delivered"
        if fails:
            record["errors"] = [delivery_failed_error()]
        record["completed_at"] = iso_z(state.clock.now())
        await state.webhooks.emit(url, "message.finalized", record)

    async def send(request: Request, account: Account) -> Response:
        body = await parse_body(request, MessageCreate)
        if isinstance(body, JSONResponse):
            return body
        if not body.text and not body.media_urls:
            return invalid("Either text or media_urls is required.", "/text")
        number = account.number_by_e164(body.from_)
        if number is None:
            return invalid_from(body.from_)
        profile_id = body.messaging_profile_id or number["messaging_profile_id"]
        if profile_id is None:
            return invalid(
                "is required (the 'from' number has no messaging profile)", "/messaging_profile_id"
            )
        profile = account.messaging_profiles.get(profile_id)
        if profile is None:
            return invalid(f"Messaging profile {profile_id!r} does not exist.", "/messaging_profile_id")
        url = webhook_target(body, profile, state.webhook_defaults)
        now = state.clock.now()
        record = message_record(
            message_id=state.ids.uuid(),
            direction="outbound",
            from_number=body.from_,
            to_number=body.to,
            to_status="queued",
            text=body.text or "",
            media_urls=body.media_urls,
            subject=body.subject,
            messaging_profile_id=profile_id,
            webhook_url=url,
            webhook_failover_url=body.webhook_failover_url,
            received_at=iso_z(now),
            valid_until=iso_z(now + _VALIDITY),
        )
        account.messages[record["id"]] = record
        fails = body.to in store.config.failing_message_destinations
        state.tasks.spawn(lifecycle(record, url, fails=fails), name=f"telnyx-message-{record['id']}")
        return data(record)

    async def detail(request: Request, account: Account) -> Response:
        message_id = request.path_params["message_id"]
        record = account.messages.get(message_id)
        return data(record) if record is not None else not_found("message", message_id)

    return [
        Route("/v2/messages", authenticated(store, send), methods=["POST"]),
        Route("/v2/messages/{message_id}", authenticated(store, detail), methods=["GET"]),
    ]
