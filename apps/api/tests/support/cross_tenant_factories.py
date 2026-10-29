"""Resource factories of the cross-tenant route suite (specs/04 §14 decision, P02-T15).

Each factory creates one resource in the factory context's tenant and returns the id that fills its path
parameter. A later phase whose routes take a new path parameter registers its factory here (or in a module
imported from here); the suite reports any path parameter without one.
"""

from __future__ import annotations

from cross_tenant import REGISTRY, FactoryContext
from identity_db import unique_email
from session_helpers import add_member, store_api_key, sub_account

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


REGISTRY.register_resource_factory("user_id", _member)
REGISTRY.register_resource_factory("invitation_id", _invitation)
REGISTRY.register_resource_factory("key_id", _api_key)
REGISTRY.register_resource_factory("org_id", _sub_account)
