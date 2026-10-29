"""``OrgDataKeyRepo`` / ``OrgDataKeyStore`` over ``org_data_keys`` (specs/28-security.md §3 Decision (h))."""

from __future__ import annotations

import os

import pytest
from identity_db import IdentityDb
from model_factories import make_org

from va_common.crypto import NIL_ORG_ID, DataKeyStore, WrappedDataKey
from va_common.errors import AppError
from va_db.repositories import OrgDataKeyRepo, OrgDataKeyStore

pytestmark = pytest.mark.integration


def _wrapped() -> WrappedDataKey:
    return WrappedDataKey(kek_id="local:v1", encrypted_data_key=os.urandom(60))


async def test_store_returns_active_key_only_for_its_org(identity_db: IdentityDb) -> None:
    database = identity_db.database
    async with database.system_session() as session:
        org, keyless = await make_org(session), await make_org(session)
    wrapped = _wrapped()
    async with database.tenant_session(org.id) as session:
        row = await OrgDataKeyRepo(session).add_active(org.id, wrapped)
        assert row.status == "active"
    store: DataKeyStore = OrgDataKeyStore(database)
    assert await store.active_data_key(org.id) == wrapped
    assert await store.active_data_key(keyless.id) is None
    assert await store.active_data_key(NIL_ORG_ID) is None
    async with database.tenant_session(keyless.id) as session:
        assert await OrgDataKeyRepo(session).active(org.id) is None


async def test_second_active_key_conflicts_and_session_stays_usable(identity_db: IdentityDb) -> None:
    database = identity_db.database
    async with database.system_session() as session:
        org = await make_org(session)
    first = _wrapped()
    async with database.tenant_session(org.id) as session:
        repo = OrgDataKeyRepo(session)
        await repo.add_active(org.id, first)
        with pytest.raises(AppError) as info:
            await repo.add_active(org.id, _wrapped())
        assert info.value.code == "CONFLICT"
        assert await repo.active(org.id) == first
    assert await OrgDataKeyStore(database).active_data_key(org.id) == first
