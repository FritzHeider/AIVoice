"""Tenancy, identity, and access models (specs/03-data-model.md §3).

Every column passes an explicit SQL type: SQLAlchemy would otherwise map ``Literal`` annotations to a
native ``Enum``, while the spec uses ``text`` with ``CHECK`` constraints built by :func:`check_in` from the
aliases below.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address
from typing import Any, Literal, get_args

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from va_db.base import Base, IdMixin, TimestampMixin
from va_db.models._checks import check_in
from va_db.models._columns import (
    bool_column,
    fk_column,
    int_column,
    jsonb_column,
    org_fk,
    org_id_column,
    text_array_column,
    text_column,
    tz,
)

__all__ = [
    "ApiKey",
    "ApiKeyEnvironment",
    "AuditActorType",
    "AuditLog",
    "DataRegion",
    "EmailToken",
    "EmailTokenPurpose",
    "IdempotencyKey",
    "Invitation",
    "IpAddress",
    "MemberRole",
    "Membership",
    "Org",
    "OrgStatus",
    "Session",
    "SsoConnection",
    "SsoProvider",
    "User",
    "UserStatus",
]

OrgStatus = Literal["active", "suspended", "closed"]
DataRegion = Literal["us", "eu"]
UserStatus = Literal["active", "disabled"]
MemberRole = Literal["owner", "admin", "developer", "analyst", "billing", "viewer"]
EmailTokenPurpose = Literal["verify_email", "reset_password"]
ApiKeyEnvironment = Literal["live", "test"]
SsoProvider = Literal["oidc"]
AuditActorType = Literal["user", "api_key", "system"]

# ``inet`` values: the asyncpg dialect returns ``ipaddress`` objects (SQLAlchemy 2.1 default).
type IpAddress = IPv4Address | IPv6Address


class Org(IdMixin, TimestampMixin, Base):
    """``orgs``: a tenant; ``limits`` is an ``OrgLimitsOverride``, ``settings`` an ``OrgSettings``."""

    __tablename__ = "orgs"
    __table_args__ = (
        CheckConstraint("kyc_level BETWEEN 0 AND 3", name="kyc_level_check"),
        check_in("status", get_args(OrgStatus)),
        check_in("data_region", get_args(DataRegion)),
        CheckConstraint("retention_days BETWEEN 1 AND 3650", name="retention_days_check"),
        CheckConstraint("recording_retention_days BETWEEN 0 AND 3650", name="recording_retention_days_check"),
        Index("orgs_parent_idx", "parent_org_id"),
    )

    parent_org_id: Mapped[uuid.UUID | None] = fk_column("orgs.id", "RESTRICT")
    name: Mapped[str] = mapped_column(Text, nullable=False)
    slug: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    plan_id: Mapped[str] = text_column("start")
    kyc_level: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    status: Mapped[OrgStatus] = text_column("active")
    credits_balance_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    # How far negative the balance may go.
    credit_limit_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    auto_recharge_enabled: Mapped[bool] = bool_column(default=False)
    auto_recharge_threshold_micros: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("5000000")
    )
    auto_recharge_amount_micros: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default=text("25000000")
    )
    stripe_customer_id: Mapped[str | None] = mapped_column(Text, unique=True)
    limits: Mapped[dict[str, Any]] = jsonb_column("{}")
    settings: Mapped[dict[str, Any]] = jsonb_column("{}")
    data_region: Mapped[DataRegion] = text_column("us")
    retention_days: Mapped[int] = int_column(365)
    recording_retention_days: Mapped[int] = int_column(90)
    # Envelope-encrypted.
    webhook_signing_secret_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)


class User(IdMixin, TimestampMixin, Base):
    """``users``: a person who can belong to several orgs."""

    __tablename__ = "users"
    __table_args__ = (check_in("status", get_args(UserStatus)),)

    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    name: Mapped[str] = text_column("")
    # argon2id; NULL for SSO-only users.
    password_hash: Mapped[str | None] = mapped_column(Text)
    email_verified_at: Mapped[datetime | None] = mapped_column(tz())
    mfa_totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    mfa_enabled: Mapped[bool] = bool_column(default=False)
    mfa_recovery_codes_hash: Mapped[list[str]] = text_array_column("{}")
    is_platform_admin: Mapped[bool] = bool_column(default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(tz())
    status: Mapped[UserStatus] = text_column("active")


class Membership(IdMixin, TimestampMixin, Base):
    """``memberships``: a user's role in one org."""

    __tablename__ = "memberships"
    __table_args__ = (
        check_in("role", get_args(MemberRole)),
        UniqueConstraint("org_id", "user_id"),
        Index("memberships_user_idx", "user_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    user_id: Mapped[uuid.UUID] = fk_column("users.id", "CASCADE", nullable=False)
    role: Mapped[MemberRole] = mapped_column(Text, nullable=False)


class Invitation(IdMixin, TimestampMixin, Base):
    """``invitations``: a pending invite of an email address into an org."""

    __tablename__ = "invitations"
    __table_args__ = (
        check_in("role", get_args(MemberRole)),
        Index("invitations_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    role: Mapped[MemberRole] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    invited_by: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("users.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(tz(), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(tz())


class Session(IdMixin, TimestampMixin, Base):
    """``sessions``: a dashboard login session (not :class:`sqlalchemy.orm.Session`)."""

    __tablename__ = "sessions"
    __table_args__ = (Index("sessions_user_idx", "user_id"),)

    user_id: Mapped[uuid.UUID] = fk_column("users.id", "CASCADE", nullable=False)
    # sha256 of the opaque cookie value.
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    active_org_id: Mapped[uuid.UUID | None] = fk_column("orgs.id", "SET NULL")
    ip: Mapped[IpAddress | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    mfa_passed: Mapped[bool] = bool_column(default=False)
    expires_at: Mapped[datetime] = mapped_column(tz(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(tz())


class EmailToken(IdMixin, TimestampMixin, Base):
    """``email_tokens``: single-use email verification and password reset tokens."""

    __tablename__ = "email_tokens"
    __table_args__ = (check_in("purpose", get_args(EmailTokenPurpose)),)

    user_id: Mapped[uuid.UUID] = fk_column("users.id", "CASCADE", nullable=False)
    purpose: Mapped[EmailTokenPurpose] = mapped_column(Text, nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(tz(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(tz())


class ApiKey(IdMixin, TimestampMixin, Base):
    """``api_keys``: org-scoped API credentials."""

    __tablename__ = "api_keys"
    __table_args__ = (
        check_in("environment", get_args(ApiKeyEnvironment)),
        Index("api_keys_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # First 12 characters shown in the UI, e.g. "va_live_3kQ9".
    prefix: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    # sha256(pepper || secret).
    secret_hash: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list[str]] = text_array_column("{*}")
    pepper_version: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("1"))
    environment: Mapped[ApiKeyEnvironment] = text_column("live")
    created_by: Mapped[uuid.UUID | None] = fk_column("users.id", "SET NULL")
    last_used_at: Mapped[datetime | None] = mapped_column(tz())
    expires_at: Mapped[datetime | None] = mapped_column(tz())
    revoked_at: Mapped[datetime | None] = mapped_column(tz())


class SsoConnection(IdMixin, TimestampMixin, Base):
    """``sso_connections``: an org's OIDC identity provider. ``default_role`` has no CHECK in the spec."""

    __tablename__ = "sso_connections"
    __table_args__ = (
        check_in("provider", get_args(SsoProvider)),
        UniqueConstraint("org_id", "issuer"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    provider: Mapped[SsoProvider] = mapped_column(Text, nullable=False)
    issuer: Mapped[str] = mapped_column(Text, nullable=False)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    client_secret_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    allowed_domains: Mapped[list[str]] = text_array_column("{}")
    default_role: Mapped[MemberRole] = text_column("viewer")
    # Password login disabled for org members when true.
    enforce: Mapped[bool] = bool_column(default=False)


class AuditLog(IdMixin, TimestampMixin, Base):
    """``audit_logs``: who changed what; ``changes`` is ``{"before", "after"}`` with secrets redacted."""

    __tablename__ = "audit_logs"
    __table_args__ = (check_in("actor_type", get_args(AuditActorType)),)

    org_id: Mapped[uuid.UUID] = org_id_column()
    actor_type: Mapped[AuditActorType] = mapped_column(Text, nullable=False)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    # e.g. "pathway.publish", "api_key.create".
    action: Mapped[str] = mapped_column(Text, nullable=False)
    resource_type: Mapped[str] = mapped_column(Text, nullable=False)
    resource_id: Mapped[str | None] = mapped_column(Text)
    ip: Mapped[IpAddress | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(Text)
    changes: Mapped[dict[str, Any]] = jsonb_column("{}")


Index("audit_logs_org_created_idx", AuditLog.org_id, AuditLog.created_at.desc())


class IdempotencyKey(TimestampMixin, Base):
    """``idempotency_keys``: stored responses for ``Idempotency-Key`` replays, keyed by ``(org_id, key)``."""

    __tablename__ = "idempotency_keys"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), org_fk(), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    response_status: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    locked_until: Mapped[datetime | None] = mapped_column(tz())
