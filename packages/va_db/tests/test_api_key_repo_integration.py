"""``ApiKeyRepo`` key management on a tenant session (specs/04-auth-tenancy-rbac.md §9)."""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from identity_db import IdentityDb
from model_factories import make_org

from va_common.ids import new_id
from va_db.models import ApiKey
from va_db.repositories import ApiKeyRepo

pytestmark = pytest.mark.integration

NOW = datetime(2026, 10, 29, 9, 30, tzinfo=UTC)


async def org_with_keys(identity_db: IdentityDb, count: int) -> tuple[uuid.UUID, list[ApiKey]]:
    """A new org and ``count`` live keys added one transaction each (so ``created_at`` increases)."""
    async with identity_db.database.system_session() as session:
        org_id = (await make_org(session)).id
    keys = []
    for index in range(count):
        async with identity_db.database.tenant_session(org_id) as session:
            keys.append(
                await ApiKeyRepo(session, org_id).add(
                    name=f"server {index}",
                    prefix=f"va_live_{secrets.token_hex(2)}",
                    secret_hash="ef" * 32,
                    pepper_version=1,
                    scopes=["calls:read"],
                    environment="live",
                    created_by=None,
                    expires_at=NOW + timedelta(days=index + 1) if index else None,
                )
            )
    return org_id, keys


async def two_keyed_orgs(identity_db: IdentityDb) -> tuple[uuid.UUID, ApiKey, uuid.UUID, ApiKey]:
    """Two orgs with one key each: ``(org_id, key, other_org_id, other_key)``."""
    org_id, [key] = await org_with_keys(identity_db, 1)
    other_id, [other_key] = await org_with_keys(identity_db, 1)
    return org_id, key, other_id, other_key


async def reread(identity_db: IdentityDb, org_id: uuid.UUID, key_id: uuid.UUID) -> ApiKey:
    """The key ``key_id`` read again in a fresh tenant session of ``org_id``."""
    async with identity_db.database.tenant_session(org_id) as session:
        key = await ApiKeyRepo(session, org_id).get(key_id)
    assert key is not None
    return key


async def test_add_stores_expires_at_and_list_is_oldest_first_per_org(identity_db: IdentityDb) -> None:
    org_id, keys = await org_with_keys(identity_db, 3)
    other_id, other_keys = await org_with_keys(identity_db, 1)
    assert keys[0].expires_at is None
    assert keys[1].expires_at == NOW + timedelta(days=2)
    async with identity_db.database.tenant_session(org_id) as session:
        listed = await ApiKeyRepo(session, org_id).list_keys()
    assert [key.id for key in listed] == [key.id for key in keys]
    async with identity_db.database.tenant_session(other_id) as session:
        assert [key.id for key in await ApiKeyRepo(session, other_id).list_keys()] == [other_keys[0].id]


async def test_rename_and_expire_update_only_the_orgs_key(identity_db: IdentityDb) -> None:
    org_id, key, other_id, other_key = await two_keyed_orgs(identity_db)
    async with identity_db.database.tenant_session(org_id) as session:
        repo = ApiKeyRepo(session, org_id)
        renamed = await repo.rename(key.id, "Zapier")
        expired = await repo.expire(key.id, NOW)
        assert await repo.rename(other_key.id, "stolen") is None
        assert await repo.expire(other_key.id, NOW) is None
        assert await repo.rename(new_id(), "nobody") is None
    assert renamed is not None
    assert expired is not None
    assert (expired.name, expired.expires_at) == ("Zapier", NOW)
    assert expired.updated_at >= key.updated_at
    untouched = await reread(identity_db, other_id, other_key.id)
    assert (untouched.name, untouched.expires_at) == ("server 0", None)


async def test_revoke_keeps_the_first_revoked_at(identity_db: IdentityDb) -> None:
    org_id, key, other_id, other_key = await two_keyed_orgs(identity_db)
    async with identity_db.database.tenant_session(org_id) as session:
        repo = ApiKeyRepo(session, org_id)
        first = await repo.revoke(key.id, NOW)
        assert first is not None
        assert first.revoked_at == NOW
    async with identity_db.database.tenant_session(org_id) as session:
        repo = ApiKeyRepo(session, org_id)
        again = await repo.revoke(key.id, NOW + timedelta(hours=1))
        assert await repo.revoke(other_key.id, NOW) is None
        assert await repo.revoke(new_id(), NOW) is None
    assert again is not None
    assert again.revoked_at == NOW
    assert (await reread(identity_db, other_id, other_key.id)).revoked_at is None
