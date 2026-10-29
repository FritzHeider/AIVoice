"""``SsoConnectionRepo`` and the system-scope ``SsoConnectionLookupRepo`` (specs/03 §2; specs/04 §5)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from identity_db import IdentityDb
from model_factories import make_org

from va_common.ids import new_id
from va_db.models import SsoConnection
from va_db.repositories import SsoConnectionLookupRepo, SsoConnectionRepo, SsoDomainClaim

pytestmark = pytest.mark.integration

ISSUER = "https://login.acme-dental.test"


def domain() -> str:
    """A domain no other test of the module (sharing one database) lists."""
    return f"d{uuid.uuid4().hex[:10]}.acme-dental.test"


def fields(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "issuer": ISSUER,
        "client_id": "voiceagents-dashboard",
        "client_secret_enc": b"\x01ciphertext",
        "allowed_domains": [domain()],
        "default_role": "viewer",
        "enforce": False,
    }
    return values | overrides


async def create(identity_db: IdentityDb, org_id: uuid.UUID, **overrides: Any) -> SsoConnection:
    async with identity_db.database.tenant_session(org_id) as session:
        return await SsoConnectionRepo(session, org_id).upsert(connection_id=new_id(), **fields(**overrides))


async def test_upsert_creates_then_updates_in_place(identity_db: IdentityDb) -> None:
    database = identity_db.database
    async with database.system_session() as session:
        org = await make_org(session)
    connection_id = new_id()
    async with database.tenant_session(org.id) as session:
        repo = SsoConnectionRepo(session, org.id)
        assert await repo.get() is None
        with pytest.raises(ValueError, match="client_secret_enc is required"):
            await repo.upsert(connection_id=connection_id, **fields(client_secret_enc=None))
        created = await repo.upsert(connection_id=connection_id, **fields())
    assert (created.id, created.org_id, created.provider) == (connection_id, org.id, "oidc")
    assert created.client_secret_enc == b"\x01ciphertext"

    new_domains = [domain(), domain()]
    async with database.tenant_session(org.id) as session:
        updated = await SsoConnectionRepo(session, org.id).upsert(
            connection_id=new_id(),
            **fields(
                issuer="https://sso.acme-dental.test",
                client_secret_enc=None,
                allowed_domains=new_domains,
                default_role="developer",
                enforce=True,
            ),
        )
    assert updated.id == connection_id
    assert updated.issuer == "https://sso.acme-dental.test"
    assert updated.client_secret_enc == b"\x01ciphertext"
    assert (updated.allowed_domains, updated.default_role, updated.enforce) == (
        new_domains,
        "developer",
        True,
    )

    async with database.tenant_session(org.id) as session:
        rotated = await SsoConnectionRepo(session, org.id).upsert(
            connection_id=new_id(), **fields(client_secret_enc=b"\x02rotated")
        )
    assert rotated.client_secret_enc == b"\x02rotated"


async def test_delete(identity_db: IdentityDb) -> None:
    database = identity_db.database
    async with database.system_session() as session:
        org = await make_org(session)
    created = await create(identity_db, org.id)
    async with database.tenant_session(org.id) as session:
        repo = SsoConnectionRepo(session, org.id)
        assert await repo.delete() == created.id
        assert await repo.delete() is None
        assert await repo.get() is None


async def test_rls_hides_other_orgs(identity_db: IdentityDb) -> None:
    database = identity_db.database
    async with database.system_session() as session:
        org, other = await make_org(session), await make_org(session)
    await create(identity_db, org.id)
    async with database.tenant_session(other.id) as session:
        assert await SsoConnectionRepo(session, other.id).get() is None
        assert await SsoConnectionRepo(session, org.id).get() is None
        assert await SsoConnectionRepo(session, org.id).delete() is None
    async with database.tenant_session(org.id) as session:
        assert await SsoConnectionRepo(session, org.id).get() is not None


async def test_claims_and_for_domain_span_orgs_oldest_first(identity_db: IdentityDb) -> None:
    database = identity_db.database
    async with database.system_session() as session:
        first_org, second_org = await make_org(session), await make_org(session)
    shared, only_first, only_second, unclaimed = domain(), domain(), domain(), domain()
    first = await create(identity_db, first_org.id, allowed_domains=[only_first, shared])
    second = await create(identity_db, second_org.id, allowed_domains=[shared, only_second])
    async with database.system_session() as session:
        lookup = SsoConnectionLookupRepo(session)
        claims = await lookup.claims([shared, only_second, unclaimed, shared])
        assert await lookup.claims([]) == []
        assert await lookup.claims([unclaimed]) == []
        by_shared = await lookup.for_domain(shared)
        by_second = await lookup.for_domain(only_second)
        assert await lookup.for_domain(unclaimed) is None
    assert claims == [
        SsoDomainClaim(org_id=first_org.id, connection_id=first.id, domain=shared),
        *sorted(
            [
                SsoDomainClaim(org_id=second_org.id, connection_id=second.id, domain=shared),
                SsoDomainClaim(org_id=second_org.id, connection_id=second.id, domain=only_second),
            ],
            key=lambda claim: claim.domain,
        ),
    ]
    assert by_shared is not None
    assert by_shared.id == first.id
    assert by_second is not None
    assert by_second.id == second.id
