"""Resource factories of the cross-tenant route suite (specs/04 §14 decision, P02-T15).

Each factory creates one resource in the factory context's tenant and returns the id that fills its path
parameter. A later phase whose routes take a new path parameter registers its factory here (or in a module
imported from here); the suite reports any path parameter without one.
"""

from __future__ import annotations

from call_rows import seed_call
from cross_tenant import REGISTRY, FactoryContext
from identity_db import unique_email
from session_helpers import add_member, store_api_key, sub_account

from va_common.ids import new_id
from va_db.models import WebhookDelivery, WebhookEndpoint
from va_db.repositories import InvitationRepo

__all__ = ["REGISTRY"]


async def _member(context: FactoryContext) -> str:
    """A developer member of the tenant's org."""
    user = await add_member(
        context.identity, context.container.password_hasher, context.tenant.org_id, "developer"
    )
    return str(user.id)


async def _invitation(context: FactoryContext) -> str:
    """An open developer invitation sent by the tenant's owner."""
    org_id = context.tenant.org_id
    async with context.identity.database.tenant_session(org_id) as session:
        row, _ = await InvitationRepo(session, org_id).create(
            email=unique_email("invitee"),
            role="developer",
            invited_by=context.tenant.owner.user.id,
            now=context.container.clock.now(),
        )
    return str(row.id)


async def _api_key(context: FactoryContext) -> str:
    """A second ``*`` key of the tenant's org."""
    _, row = await store_api_key(context.container, context.tenant.org_id)
    return str(row.id)


async def _sub_account(context: FactoryContext) -> str:
    """A sub-account created through ``POST /v1/orgs/create`` with the tenant's key."""
    child_id, _ = await sub_account(context.client, Authorization=context.tenant.api_key)
    return str(child_id)


async def _call(context: FactoryContext) -> str:
    """An ``in_progress`` call of the tenant's org (stopping it publishes a control message)."""
    call = await seed_call(context.identity.database, status="in_progress", org_id=context.tenant.org_id)
    return str(call.call_id)


# A closed port on an IP literal: no DNS, never leaves the host, and the owner's test send fails fast.
_CLOSED_HOOK = "http://127.0.0.1:9/hook"


async def _webhook_endpoint(context: FactoryContext) -> str:
    """An enabled webhook endpoint of the tenant's org."""
    org_id = context.tenant.org_id
    async with context.identity.database.tenant_session(org_id) as session:
        endpoint = WebhookEndpoint(org_id=org_id, url=_CLOSED_HOOK)
        session.add(endpoint)
        await session.flush()
        return str(endpoint.id)


async def _webhook_delivery(context: FactoryContext) -> str:
    """A ``dead`` delivery of the tenant's org (no endpoint), so the owner's replay succeeds."""
    org_id = context.tenant.org_id
    async with context.identity.database.tenant_session(org_id) as session:
        delivery = WebhookDelivery(
            org_id=org_id,
            event_id=f"evt_{new_id().hex}",
            event_type="call.completed",
            url=_CLOSED_HOOK,
            payload={"status": "completed"},
            status="dead",
            attempts=9,
            last_error="http_status",
        )
        session.add(delivery)
        await session.flush()
        return str(delivery.id)


REGISTRY.register_resource_factory("user_id", _member)
REGISTRY.register_resource_factory("invitation_id", _invitation)
REGISTRY.register_resource_factory("key_id", _api_key)
REGISTRY.register_resource_factory("org_id", _sub_account)
REGISTRY.register_resource_factory("call_id", _call)
REGISTRY.register_resource_factory("endpoint_id", _webhook_endpoint)
REGISTRY.register_resource_factory("delivery_id", _webhook_delivery)
# ``GET /v1/calls`` takes its filters as one query model, which the route walker sees as a required parameter;
# the unfiltered listing must hold none of org A's calls.
REGISTRY.register_request("calls_list", params={"limit": "1000"})
# The call artifact routes take one query model each (seen as required); the owner's seeded call has no
# recording, so its recording control is ``404 NOT_FOUND`` while org B gets ``CALL_NOT_FOUND``.
REGISTRY.register_request("calls_recording", params={"format": "json"})
REGISTRY.register_request("calls_transcript", params={"format": "json"})
REGISTRY.register_request("calls_events", params={"limit": "1000"})
REGISTRY.register_owner_status("calls_recording", 404)
