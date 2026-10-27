"""Tenancy models against Postgres: ``create_all`` inside one transaction that is rolled back at teardown."""

from __future__ import annotations

import os
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import Any, get_args

import pytest
from service_helpers import settings_of
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.ids import new_id
from va_common.settings import DatabaseSettings
from va_db.base import Base
from va_db.engine import create_engine
from va_db.models import (
    ApiKey,
    ApiKeyEnvironment,
    AuditActorType,
    AuditLog,
    DataRegion,
    EmailToken,
    EmailTokenPurpose,
    IdempotencyKey,
    Invitation,
    MemberRole,
    Membership,
    Org,
    OrgStatus,
    Session,
    SsoConnection,
    SsoProvider,
    User,
    UserStatus,
)

pytestmark = pytest.mark.integration

_EXPIRES = datetime(2030, 1, 1, tzinfo=UTC)

type _Builder = Callable[[AsyncSession], Awaitable[Base]]


@pytest.fixture
async def session(pg_url: str) -> AsyncIterator[AsyncSession]:
    engine = create_engine(settings_of(DatabaseSettings, database_url=pg_url, database_pool_size=1), None)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS citext"))
            await connection.run_sync(Base.metadata.create_all)
            async with AsyncSession(bind=connection, join_transaction_mode="create_savepoint") as scratch:
                yield scratch
            await transaction.rollback()
    finally:
        await engine.dispose()


def _hash() -> str:
    return secrets.token_hex(32)


async def _flushed[M: Base](session: AsyncSession, model: M) -> M:
    session.add(model)
    await session.flush()
    await session.refresh(model)
    return model


async def _org(session: AsyncSession, **values: Any) -> Org:
    org = Org(
        name="Acme Dental",
        slug=f"acme-{secrets.token_hex(4)}",
        webhook_signing_secret_enc=os.urandom(32),
        **values,
    )
    return await _flushed(session, org)


async def _user(session: AsyncSession, email: str | None = None) -> User:
    return await _flushed(session, User(email=email or f"{secrets.token_hex(4)}@example.test"))


async def _build_org(session: AsyncSession) -> Base:
    return Org(
        name="Acme Dental", slug=f"acme-{secrets.token_hex(4)}", webhook_signing_secret_enc=os.urandom(32)
    )


async def _build_user(session: AsyncSession) -> Base:
    return User(email=f"{secrets.token_hex(4)}@example.test")


async def _build_membership(session: AsyncSession) -> Base:
    org, user = await _org(session), await _user(session)
    return Membership(org_id=org.id, user_id=user.id, role="viewer")


async def _build_invitation(session: AsyncSession) -> Base:
    org, user = await _org(session), await _user(session)
    return Invitation(
        org_id=org.id,
        email="invitee@example.test",
        role="viewer",
        token_hash=_hash(),
        invited_by=user.id,
        expires_at=_EXPIRES,
    )


async def _build_email_token(session: AsyncSession) -> Base:
    user = await _user(session)
    return EmailToken(user_id=user.id, purpose="verify_email", token_hash=_hash(), expires_at=_EXPIRES)


async def _build_api_key(session: AsyncSession) -> Base:
    org = await _org(session)
    return ApiKey(org_id=org.id, name="ci", prefix=f"va_live_{secrets.token_hex(2)}", secret_hash=_hash())


async def _build_sso(session: AsyncSession) -> Base:
    org = await _org(session)
    return SsoConnection(
        org_id=org.id,
        provider="oidc",
        issuer=f"https://idp.example.test/{secrets.token_hex(4)}",
        client_id="dashboard",
        client_secret_enc=os.urandom(32),
    )


async def _build_audit_log(session: AsyncSession) -> Base:
    org = await _org(session)
    return AuditLog(org_id=org.id, actor_type="system", action="api_key.create", resource_type="api_key")


async def test_org_defaults(session: AsyncSession) -> None:
    org = await _org(session)
    assert org.parent_org_id is None
    assert org.plan_id == "start"
    assert org.kyc_level == 0
    assert org.status == "active"
    assert org.credits_balance_micros == 0
    assert org.credit_limit_micros == 0
    assert org.auto_recharge_enabled is False
    assert org.auto_recharge_threshold_micros == 5_000_000
    assert org.auto_recharge_amount_micros == 25_000_000
    assert org.stripe_customer_id is None
    assert org.limits == {}
    assert org.settings == {}
    assert org.data_region == "us"
    assert org.retention_days == 365
    assert org.recording_retention_days == 90
    assert org.id.version == 7
    assert org.created_at.utcoffset() == timedelta(0)
    assert org.updated_at.utcoffset() == timedelta(0)


async def test_user_api_key_sso_defaults(session: AsyncSession) -> None:
    user = await _user(session)
    assert (user.name, user.password_hash, user.mfa_enabled) == ("", None, False)
    assert user.mfa_recovery_codes_hash == []
    assert (user.is_platform_admin, user.status) == (False, "active")

    api_key = await _flushed(session, await _build_api_key(session))
    assert isinstance(api_key, ApiKey)
    assert (api_key.scopes, api_key.pepper_version, api_key.environment) == (["*"], 1, "live")

    sso = await _flushed(session, await _build_sso(session))
    assert isinstance(sso, SsoConnection)
    assert (sso.allowed_domains, sso.default_role, sso.enforce) == ([], "viewer", False)

    audit = await _flushed(session, await _build_audit_log(session))
    assert isinstance(audit, AuditLog)
    assert audit.changes == {}

    login = await _flushed(session, Session(user_id=user.id, token_hash=_hash(), expires_at=_EXPIRES))
    assert login.mfa_passed is False


@pytest.mark.parametrize(
    ("builder", "column", "literal"),
    [
        (_build_org, "status", OrgStatus),
        (_build_org, "data_region", DataRegion),
        (_build_user, "status", UserStatus),
        (_build_membership, "role", MemberRole),
        (_build_invitation, "role", MemberRole),
        (_build_email_token, "purpose", EmailTokenPurpose),
        (_build_api_key, "environment", ApiKeyEnvironment),
        (_build_sso, "provider", SsoProvider),
        (_build_audit_log, "actor_type", AuditActorType),
    ],
)
async def test_enum_values_accepted_and_rejected(
    session: AsyncSession, builder: _Builder, column: str, literal: Any
) -> None:
    for value in get_args(literal):
        model = await builder(session)
        setattr(model, column, value)
        async with session.begin_nested():
            await _flushed(session, model)
        assert getattr(model, column) == value
    model = await builder(session)
    setattr(model, column, "bogus")
    with pytest.raises(IntegrityError, match=f"{model.__tablename__}_{column}_check"):
        async with session.begin_nested():
            await _flushed(session, model)


@pytest.mark.parametrize(
    ("column", "value", "accepted"),
    [
        ("kyc_level", 0, True),
        ("kyc_level", 3, True),
        ("kyc_level", 4, False),
        ("kyc_level", -1, False),
        ("retention_days", 1, True),
        ("retention_days", 3650, True),
        ("retention_days", 0, False),
        ("retention_days", 3651, False),
        ("recording_retention_days", 0, True),
        ("recording_retention_days", 3650, True),
        ("recording_retention_days", -1, False),
        ("recording_retention_days", 3651, False),
    ],
)
async def test_range_checks_enforced(session: AsyncSession, column: str, value: int, accepted: bool) -> None:
    if accepted:
        org = await _org(session, **{column: value})
        assert getattr(org, column) == value
        return
    with pytest.raises(IntegrityError, match=f"orgs_{column}_check"):
        async with session.begin_nested():
            await _org(session, **{column: value})


async def test_citext_unique_email_case_insensitive(session: AsyncSession) -> None:
    await _user(session, "Ada@Example.test")
    with pytest.raises(IntegrityError, match="users_email_key"):
        async with session.begin_nested():
            await _user(session, "ada@example.test")
    found = (await session.execute(select(User.email).where(User.email == "ADA@EXAMPLE.TEST"))).scalar_one()
    assert found == "Ada@Example.test"


@pytest.mark.parametrize("address", ["203.0.113.7", "2001:db8::7"])
async def test_inet_round_trips_as_ip_address(session: AsyncSession, address: str) -> None:
    user = await _user(session)
    login = await _flushed(
        session,
        Session(
            user_id=user.id,
            token_hash=_hash(),
            expires_at=_EXPIRES,
            ip=ip_address(address),
            user_agent="pytest",
        ),
    )
    assert login.ip == ip_address(address)
    audit = await _flushed(
        session,
        AuditLog(
            org_id=(await _org(session)).id,
            actor_type="user",
            actor_id=user.id,
            action="session.create",
            resource_type="session",
            ip=ip_address(address),
        ),
    )
    assert audit.ip == ip_address(address)


async def test_idempotency_key_composite_pk(session: AsyncSession) -> None:
    org, other = await _org(session), await _org(session)
    row = {"key": "order-42", "method": "POST", "path": "/v1/calls", "request_hash": _hash()}
    await session.execute(insert(IdempotencyKey).values(org_id=org.id, **row))
    with pytest.raises(IntegrityError, match="idempotency_keys_pkey"):
        async with session.begin_nested():
            await session.execute(insert(IdempotencyKey).values(org_id=org.id, **row))
    await session.execute(insert(IdempotencyKey).values(org_id=other.id, **row))
    stored = await session.execute(
        select(IdempotencyKey.org_id, IdempotencyKey.response_status).where(IdempotencyKey.key == "order-42")
    )
    assert sorted(stored.all()) == sorted([(org.id, None), (other.id, None)])


async def _count(session: AsyncSession, model: type[Membership | ApiKey], org_id: uuid.UUID) -> int:
    result: int = (
        await session.execute(select(func.count()).select_from(model).where(model.org_id == org_id))
    ).scalar_one()
    return result


async def test_org_delete_cascades_and_parent_restricts(session: AsyncSession) -> None:
    org, user = await _org(session), await _user(session)
    await _flushed(session, Membership(org_id=org.id, user_id=user.id, role="owner"))
    await _flushed(
        session, ApiKey(org_id=org.id, name="ci", prefix=f"va_test_{new_id().hex[:4]}", secret_hash=_hash())
    )
    login = await _flushed(
        session, Session(user_id=user.id, token_hash=_hash(), expires_at=_EXPIRES, active_org_id=org.id)
    )
    session.expunge_all()

    await session.execute(delete(Org).where(Org.id == org.id))
    assert await _count(session, Membership, org.id) == 0
    assert await _count(session, ApiKey, org.id) == 0
    active = (await session.execute(select(Session.active_org_id).where(Session.id == login.id))).scalar_one()
    assert active is None

    parent = await _org(session)
    await _org(session, parent_org_id=parent.id)
    with pytest.raises(IntegrityError, match="orgs_parent_org_id_fkey"):
        async with session.begin_nested():
            await session.execute(delete(Org).where(Org.id == parent.id))
