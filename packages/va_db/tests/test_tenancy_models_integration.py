"""Tenancy models against Postgres: ``create_all`` inside one transaction that is rolled back at teardown."""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address
from typing import Any

import pytest
from model_factories import ModelBuilder, assert_enum_check_enforced, flushed, make_org, make_user
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from va_common.ids import new_id
from va_db.base import Base
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


def _hash() -> str:
    return secrets.token_hex(32)


async def _build_org(session: AsyncSession) -> Base:
    return Org(
        name="Acme Dental", slug=f"acme-{secrets.token_hex(4)}", webhook_signing_secret_enc=os.urandom(32)
    )


async def _build_user(session: AsyncSession) -> Base:
    return User(email=f"{secrets.token_hex(4)}@example.test")


async def _build_membership(session: AsyncSession) -> Base:
    org, user = await make_org(session), await make_user(session)
    return Membership(org_id=org.id, user_id=user.id, role="viewer")


async def _build_invitation(session: AsyncSession) -> Base:
    org, user = await make_org(session), await make_user(session)
    return Invitation(
        org_id=org.id,
        email="invitee@example.test",
        role="viewer",
        token_hash=_hash(),
        invited_by=user.id,
        expires_at=_EXPIRES,
    )


async def _build_email_token(session: AsyncSession) -> Base:
    user = await make_user(session)
    return EmailToken(user_id=user.id, purpose="verify_email", token_hash=_hash(), expires_at=_EXPIRES)


async def _build_api_key(session: AsyncSession) -> Base:
    org = await make_org(session)
    return ApiKey(org_id=org.id, name="ci", prefix=f"va_live_{secrets.token_hex(2)}", secret_hash=_hash())


async def _build_sso(session: AsyncSession) -> Base:
    org = await make_org(session)
    return SsoConnection(
        org_id=org.id,
        provider="oidc",
        issuer=f"https://idp.example.test/{secrets.token_hex(4)}",
        client_id="dashboard",
        client_secret_enc=os.urandom(32),
    )


async def _build_audit_log(session: AsyncSession) -> Base:
    org = await make_org(session)
    return AuditLog(org_id=org.id, actor_type="system", action="api_key.create", resource_type="api_key")


async def test_org_defaults(scratch_session: AsyncSession) -> None:
    org = await make_org(scratch_session)
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


async def test_user_api_key_sso_defaults(scratch_session: AsyncSession) -> None:
    user = await make_user(scratch_session)
    assert (user.name, user.password_hash, user.mfa_enabled) == ("", None, False)
    assert user.mfa_recovery_codes_hash == []
    assert (user.is_platform_admin, user.status) == (False, "active")

    api_key = await flushed(scratch_session, await _build_api_key(scratch_session))
    assert isinstance(api_key, ApiKey)
    assert (api_key.scopes, api_key.pepper_version, api_key.environment) == (["*"], 1, "live")

    sso = await flushed(scratch_session, await _build_sso(scratch_session))
    assert isinstance(sso, SsoConnection)
    assert (sso.allowed_domains, sso.default_role, sso.enforce) == ([], "viewer", False)

    audit = await flushed(scratch_session, await _build_audit_log(scratch_session))
    assert isinstance(audit, AuditLog)
    assert audit.changes == {}

    login = await flushed(scratch_session, Session(user_id=user.id, token_hash=_hash(), expires_at=_EXPIRES))
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
    scratch_session: AsyncSession, builder: ModelBuilder, column: str, literal: Any
) -> None:
    await assert_enum_check_enforced(scratch_session, builder, column, literal)


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
async def test_range_checks_enforced(
    scratch_session: AsyncSession, column: str, value: int, accepted: bool
) -> None:
    if accepted:
        org = await make_org(scratch_session, **{column: value})
        assert getattr(org, column) == value
        return
    with pytest.raises(IntegrityError, match=f"orgs_{column}_check"):
        async with scratch_session.begin_nested():
            await make_org(scratch_session, **{column: value})


async def test_citext_unique_email_case_insensitive(scratch_session: AsyncSession) -> None:
    await make_user(scratch_session, "Ada@Example.test")
    with pytest.raises(IntegrityError, match="users_email_key"):
        async with scratch_session.begin_nested():
            await make_user(scratch_session, "ada@example.test")
    found = (
        await scratch_session.execute(select(User.email).where(User.email == "ADA@EXAMPLE.TEST"))
    ).scalar_one()
    assert found == "Ada@Example.test"


@pytest.mark.parametrize("address", ["203.0.113.7", "2001:db8::7"])
async def test_inet_round_trips_as_ip_address(scratch_session: AsyncSession, address: str) -> None:
    user = await make_user(scratch_session)
    login = await flushed(
        scratch_session,
        Session(
            user_id=user.id,
            token_hash=_hash(),
            expires_at=_EXPIRES,
            ip=ip_address(address),
            user_agent="pytest",
        ),
    )
    assert login.ip == ip_address(address)
    audit = await flushed(
        scratch_session,
        AuditLog(
            org_id=(await make_org(scratch_session)).id,
            actor_type="user",
            actor_id=user.id,
            action="scratch_session.create",
            resource_type="scratch_session",
            ip=ip_address(address),
        ),
    )
    assert audit.ip == ip_address(address)


async def test_idempotency_key_composite_pk(scratch_session: AsyncSession) -> None:
    org, other = await make_org(scratch_session), await make_org(scratch_session)
    row = {"key": "order-42", "method": "POST", "path": "/v1/calls", "request_hash": _hash()}
    await scratch_session.execute(insert(IdempotencyKey).values(org_id=org.id, **row))
    with pytest.raises(IntegrityError, match="idempotency_keys_pkey"):
        async with scratch_session.begin_nested():
            await scratch_session.execute(insert(IdempotencyKey).values(org_id=org.id, **row))
    await scratch_session.execute(insert(IdempotencyKey).values(org_id=other.id, **row))
    stored = await scratch_session.execute(
        select(IdempotencyKey.org_id, IdempotencyKey.response_status).where(IdempotencyKey.key == "order-42")
    )
    assert sorted(stored.all()) == sorted([(org.id, None), (other.id, None)])


async def _count(session: AsyncSession, model: type[Membership | ApiKey], org_id: uuid.UUID) -> int:
    result: int = (
        await session.execute(select(func.count()).select_from(model).where(model.org_id == org_id))
    ).scalar_one()
    return result


async def test_org_delete_cascades_and_parent_restricts(scratch_session: AsyncSession) -> None:
    org, user = await make_org(scratch_session), await make_user(scratch_session)
    await flushed(scratch_session, Membership(org_id=org.id, user_id=user.id, role="owner"))
    await flushed(
        scratch_session,
        ApiKey(org_id=org.id, name="ci", prefix=f"va_test_{new_id().hex[:4]}", secret_hash=_hash()),
    )
    login = await flushed(
        scratch_session,
        Session(user_id=user.id, token_hash=_hash(), expires_at=_EXPIRES, active_org_id=org.id),
    )
    scratch_session.expunge_all()

    await scratch_session.execute(delete(Org).where(Org.id == org.id))
    assert await _count(scratch_session, Membership, org.id) == 0
    assert await _count(scratch_session, ApiKey, org.id) == 0
    active = (
        await scratch_session.execute(select(Session.active_org_id).where(Session.id == login.id))
    ).scalar_one()
    assert active is None

    parent = await make_org(scratch_session)
    await make_org(scratch_session, parent_org_id=parent.id)
    with pytest.raises(IntegrityError, match="orgs_parent_org_id_fkey"):
        async with scratch_session.begin_nested():
            await scratch_session.execute(delete(Org).where(Org.id == parent.id))
