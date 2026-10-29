"""``OrgProvisioner`` against a migrated database (specs/04-auth-tenancy-rbac.md §4.1; specs/28 §3).

Every provisioning writes the org, its active data key, its encrypted webhook secret and the owner in one
transaction, so each failure case asserts that no row of any of those tables was left behind.
"""

from __future__ import annotations

import re
import uuid

import pytest
from identity_db import IdentityDb, row_counts, unique_email
from model_factories import make_org
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from va_common.crypto import DecryptionError
from va_common.errors import AppError
from va_common.ids import new_id
from va_db import provisioning
from va_db.models import Membership, Org, OrgDataKey
from va_db.provisioning import ProvisionedOrg, webhook_secret_aad
from va_db.repositories import IdentityRepo, MembershipRepo, NewUser, OrgDataKeyStore

pytestmark = pytest.mark.integration

_SLUG = re.compile(r"[a-z0-9-]+-[a-z0-9]{4}")


class _BoomError(Exception):
    """Raised inside a caller's transaction to force a rollback."""


async def _provision_new_owner(db: IdentityDb, name: str = "Acme Dental") -> ProvisionedOrg:
    return await db.provisioner.provision(name=name, owner=NewUser(email=unique_email("owner"), name="Owner"))


async def test_provision_with_new_owner_creates_all_rows(identity_db: IdentityDb) -> None:
    result = await _provision_new_owner(identity_db, "  Acme Dental  ")
    org = result.org
    assert result.owner is not None
    assert result.membership is not None
    assert (org.name, org.plan_id, org.status, org.parent_org_id) == ("Acme Dental", "start", "active", None)
    assert _SLUG.fullmatch(org.slug)
    assert org.slug.startswith("acme-dental-")
    async with identity_db.database.system_session() as session:
        keys = (await session.scalars(select(OrgDataKey).where(OrgDataKey.org_id == org.id))).all()
        memberships = (await session.scalars(select(Membership).where(Membership.org_id == org.id))).all()
    assert [key.status for key in keys] == ["active"]
    assert [(m.user_id, m.role) for m in memberships] == [(result.owner.id, "owner")]
    async with identity_db.database.global_session() as session:
        owner = await IdentityRepo(session).get_user(result.owner.id)
    assert owner is not None
    assert (owner.name, owner.email_verified_at) == ("Owner", None)
    assert await OrgDataKeyStore(identity_db.database).active_data_key(org.id) is not None
    assert result.webhook_signing_secret not in repr(result)


async def test_provision_without_owner(identity_db: IdentityDb) -> None:
    result = await identity_db.provisioner.provision(name="Ownerless", owner=None, plan_id="build")
    assert (result.owner, result.membership, result.org.plan_id) == (None, None, "build")


async def test_webhook_secret_decrypts_with_its_aad(identity_db: IdentityDb) -> None:
    result = await _provision_new_owner(identity_db)
    other = await _provision_new_owner(identity_db, "Other Org")
    secret = result.webhook_signing_secret
    assert secret.startswith("whsec_")
    assert len(secret) == 49
    async with identity_db.database.global_session() as session:
        org = (await session.execute(select(Org).where(Org.id == result.org.id))).scalar_one()
    assert secret.encode() not in org.webhook_signing_secret_enc
    assert await identity_db.provisioner.webhook_signing_secret(org) == secret
    with pytest.raises(DecryptionError):
        await identity_db.envelope.decrypt(
            org.id, org.webhook_signing_secret_enc, webhook_secret_aad(other.org.id)
        )
    assert other.webhook_signing_secret != secret


async def test_empty_name_is_invalid_input(identity_db: IdentityDb) -> None:
    before = await row_counts(identity_db.database)
    with pytest.raises(AppError) as info:
        await identity_db.provisioner.provision(name="   ", owner=None)
    assert (info.value.code, info.value.field) == ("INVALID_INPUT", "org_name")
    assert await row_counts(identity_db.database) == before


async def test_provision_is_atomic_on_duplicate_owner_email(identity_db: IdentityDb) -> None:
    email = unique_email("taken")
    async with identity_db.database.global_session() as session:
        await IdentityRepo(session).create_user(NewUser(email=email))
    before = await row_counts(identity_db.database)
    with pytest.raises(AppError) as info:
        await identity_db.provisioner.provision(name="Dup", owner=NewUser(email=email.upper()))
    assert (info.value.code, info.value.field) == ("CONFLICT", "email")
    assert await row_counts(identity_db.database) == before


async def test_provision_is_atomic_when_membership_fails(
    identity_db: IdentityDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fail(*_: object) -> Membership:
        raise _BoomError

    monkeypatch.setattr(MembershipRepo, "add", fail)
    before = await row_counts(identity_db.database)
    with pytest.raises(_BoomError):
        await _provision_new_owner(identity_db)
    assert await row_counts(identity_db.database) == before


async def test_provision_existing_owner_and_parent(identity_db: IdentityDb) -> None:
    parent = await _provision_new_owner(identity_db, "Parent")
    assert parent.owner is not None
    child = await identity_db.provisioner.provision(
        name="Child", owner=parent.owner.id, parent_org_id=parent.org.id
    )
    assert child.org.parent_org_id == parent.org.id
    assert child.owner is not None
    assert child.owner.id == parent.owner.id
    assert child.membership is not None
    assert child.membership.role == "owner"
    before = await row_counts(identity_db.database)
    with pytest.raises(AppError) as info:
        await identity_db.provisioner.provision(name="Orphan", owner=uuid.uuid4())
    assert info.value.code == "NOT_FOUND"
    assert await row_counts(identity_db.database) == before


async def test_slug_collision_retries(identity_db: IdentityDb, monkeypatch: pytest.MonkeyPatch) -> None:
    async with identity_db.database.system_session() as session:
        await make_org(session, slug="clash-co-aaaa")
    suffixes = iter(["aaaa", "bbbb"])
    monkeypatch.setattr(provisioning, "random_slug_suffix", lambda: next(suffixes))
    result = await identity_db.provisioner.provision(name="Clash Co", owner=None)
    assert result.org.slug == "clash-co-bbbb"


async def test_slug_attempts_exhausted_conflict(
    identity_db: IdentityDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with identity_db.database.system_session() as session:
        await make_org(session, slug="stuck-co-aaaa")
    monkeypatch.setattr(provisioning, "random_slug_suffix", lambda: "aaaa")
    before = await row_counts(identity_db.database)
    with pytest.raises(AppError) as info:
        await identity_db.provisioner.provision(name="Stuck Co", owner=None)
    assert (info.value.code, info.value.message) == ("CONFLICT", "Could not allocate a unique org slug.")
    assert await row_counts(identity_db.database) == before


async def test_provision_in_shares_caller_transaction(identity_db: IdentityDb) -> None:
    before = await row_counts(identity_db.database)
    org_id = new_id()
    with pytest.raises(_BoomError):
        async with identity_db.database.tenant_session(org_id) as session:
            result = await identity_db.provisioner.provision_in(
                session, org_id, name="Shared", owner=NewUser(email=unique_email())
            )
            assert result.owner is not None
            await IdentityRepo(session).create_session(
                user_id=result.owner.id,
                active_org_id=org_id,
                expires_at=result.org.created_at,
                ip=None,
                user_agent=None,
            )
            raise _BoomError
    assert await row_counts(identity_db.database) == before


async def test_provision_in_refuses_mismatched_tenant_session(identity_db: IdentityDb) -> None:
    before = await row_counts(identity_db.database)
    with pytest.raises(DBAPIError, match="row-level security"):
        async with identity_db.database.tenant_session(new_id()) as session:
            await identity_db.provisioner.provision_in(session, new_id(), name="Forged", owner=None)
    assert await row_counts(identity_db.database) == before
