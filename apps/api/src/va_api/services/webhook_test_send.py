"""``POST /v1/webhooks/endpoints/{id}/test``: one synthetic, signed delivery (specs/19 §10 Decision P06-T04b).

The body is a §7 envelope with ``data = {"test": true, "message": ...}``, signed with the org's active secrets
exactly like the dispatcher signs (shared :class:`va_events.webhook_delivery.Deliverer`). The URL is vetted
first with the endpoint rules (a refusal is the result ``blocked_destination:<reason>`` and no request is
made) and again at connect time by the pinning client. One attempt, no ``webhook_deliveries`` row, no retry,
no call event; no database session is open while the request runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final

from va_api.services.webhook_policy import url_policy
from va_api.settings import ApiSettings
from va_common.crypto import Envelope
from va_common.ids import new_id
from va_common.net import HostResolver, UnsafeDestinationError, check_destination_url
from va_common.time import Clock
from va_db.repositories import NotFoundError, OrgRepo, WebhookEndpointRepo
from va_db.session import Database
from va_db.webhook_secrets import active_signing_secrets
from va_domain.org import load_settings
from va_events import EventType, new_event
from va_events.webhook_delivery import Deliverer, Outgoing, SendResult, blocked
from va_events.webhook_payloads import envelope_body

__all__ = [
    "TEST_MESSAGE",
    "TEST_SEND_TIMEOUT_S",
    "SyntheticDelivery",
    "SyntheticDeliveryDeps",
    "send_test_event",
]

TEST_SEND_TIMEOUT_S: Final = 10.0
TEST_MESSAGE: Final = "Test event from VoiceAgents."
_FORMAT: Final = "envelope"


@dataclass(frozen=True, slots=True)
class SyntheticDeliveryDeps:
    """What a test send needs from the container."""

    database: Database
    envelope: Envelope
    deliverer: Deliverer
    settings: ApiSettings
    resolver: HostResolver
    clock: Clock


@dataclass(frozen=True, slots=True)
class SyntheticDelivery:
    """The synthetic event's id and type and what its single attempt produced."""

    event_id: str
    event_type: str
    result: SendResult


async def send_test_event(
    deps: SyntheticDeliveryDeps, *, org_id: uuid.UUID, endpoint_id: uuid.UUID, event_type: EventType
) -> SyntheticDelivery:
    """Send one signed test event to the org's endpoint ``endpoint_id`` (``NOT_FOUND`` when not the org's)."""
    async with deps.database.tenant_session(org_id) as session:
        endpoint = await WebhookEndpointRepo(session, org_id).get(endpoint_id)
        url = endpoint.url
    async with deps.database.global_session() as session:
        org = await OrgRepo(session).get(org_id)
        if org is None:
            raise NotFoundError("Org")
        org_settings = load_settings(org.settings)
        active = await active_signing_secrets(deps.envelope, org, now=deps.clock.now())
    event = new_event(event_type, org_id=org_id, clock=deps.clock)
    outgoing = Outgoing(
        id=new_id(),
        org_id=org_id,
        event_id=event.id,
        event_type=event_type.value,
        url=url,
        endpoint_id=endpoint_id,
        payload=envelope_body(event, {"test": True, "message": TEST_MESSAGE}),
        format=_FORMAT,
        attempts=0,
        call_id=None,
    )
    allow_http, allow_private = url_policy(org=org_settings, settings=deps.settings)
    try:
        await check_destination_url(
            url, allow_http=allow_http, allow_private=allow_private, resolver=deps.resolver
        )
    except UnsafeDestinationError as exc:
        return SyntheticDelivery(event.id, event_type.value, SendResult(None, blocked(exc.reason), None))
    result = await deps.deliverer.send(outgoing, active.secrets)
    return SyntheticDelivery(event.id, event_type.value, result)
