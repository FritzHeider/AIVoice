"""Request and response bodies of the dashboard session endpoints (specs/04-auth-tenancy-rbac.md §4.1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from va_db.models import MemberRole, Org, OrgStatus, User
from va_db.repositories import MembershipRecord

__all__ = [
    "EmptyBody",
    "LoginRequest",
    "LoginResult",
    "LogoutResult",
    "ResendResult",
    "SessionInfo",
    "SessionMembership",
    "SessionUser",
    "SignupOrg",
    "SignupRequest",
    "SignupResult",
    "VerifyEmailRequest",
    "VerifyEmailResult",
]

EmailField = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=320)]
PasswordField = Annotated[str, Field(min_length=1, max_length=1024, repr=False)]
DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]

_USER_EXAMPLE: Final[dict[str, JsonValue]] = {
    "id": "0192f3c4-5a6b-7c8d-9e0f-1a2b3c4d5e6f",
    "email": "jane@acme-dental.test",
    "name": "Jane Park",
    "email_verified": True,
    "mfa_enabled": False,
    "is_platform_admin": False,
    "created_at": "2026-10-29T09:30:00Z",
}
_UNVERIFIED_USER_EXAMPLE: Final[dict[str, JsonValue]] = {**_USER_EXAMPLE, "email_verified": False}
_ORG_EXAMPLE: Final[dict[str, JsonValue]] = {
    "id": "0192f3c4-5a6b-7c8d-9e0f-2b3c4d5e6f70",
    "name": "Acme Dental",
    "slug": "acme-dental-k3x9",
    "plan_id": "start",
    "status": "active",
    "created_at": "2026-10-29T09:30:00Z",
}
_EMAIL_TOKEN_EXAMPLE: Final = "Xb4LZ0c9qkV2mR7tWn1sYe8pHu3aGj6dKf5oQi2NwTs"  # noqa: S105 - documentation example
_MEMBERSHIP_EXAMPLE: Final[dict[str, JsonValue]] = {
    "org_id": "0192f3c4-5a6b-7c8d-9e0f-2b3c4d5e6f70",
    "org_name": "Acme Dental",
    "org_slug": "acme-dental-k3x9",
    "role": "owner",
    "plan_id": "start",
    "org_status": "active",
    "parent_org_id": None,
}


class EmptyBody(BaseModel):
    """The optional ``{}`` body of logout endpoints."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{}]})


class LoginRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [{"email": "jane@acme-dental.test", "password": "correct horse battery"}]
        },
    )

    email: EmailField
    password: PasswordField


class SignupRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "email": "jane@acme-dental.test",
                    "password": "correct horse battery",
                    "name": "Jane Park",
                    "org_name": "Acme Dental",
                }
            ]
        },
    )

    email: EmailField
    password: PasswordField
    name: DisplayName
    org_name: DisplayName


class VerifyEmailRequest(BaseModel):
    model_config = ConfigDict(
        extra="forbid", json_schema_extra={"examples": [{"token": _EMAIL_TOKEN_EXAMPLE}]}
    )

    token: str = Field(min_length=1, max_length=256, repr=False)


class SessionUser(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [_USER_EXAMPLE]})

    id: uuid.UUID
    email: str
    name: str
    email_verified: bool
    mfa_enabled: bool
    is_platform_admin: bool
    created_at: datetime

    @classmethod
    def of(cls, user: User) -> SessionUser:
        return cls(
            id=user.id,
            email=user.email,
            name=user.name,
            email_verified=user.email_verified_at is not None,
            mfa_enabled=user.mfa_enabled,
            is_platform_admin=user.is_platform_admin,
            created_at=user.created_at,
        )


class SessionMembership(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [_MEMBERSHIP_EXAMPLE]})

    org_id: uuid.UUID
    org_name: str
    org_slug: str
    role: MemberRole
    plan_id: str
    org_status: OrgStatus
    parent_org_id: uuid.UUID | None

    @classmethod
    def of(cls, record: MembershipRecord) -> SessionMembership:
        return cls(
            org_id=record.org_id,
            org_name=record.org_name,
            org_slug=record.org_slug,
            role=record.role,
            plan_id=record.plan_id,
            org_status=record.org_status,
            parent_org_id=record.parent_org_id,
        )


class LoginResult(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"mfa_required": False, "user": _USER_EXAMPLE}]}
    )

    mfa_required: bool
    user: SessionUser


class LogoutResult(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"revoked_sessions": 1}]})

    revoked_sessions: int


class SessionInfo(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "user": _USER_EXAMPLE,
                    "memberships": [_MEMBERSHIP_EXAMPLE],
                    "active_org_id": _MEMBERSHIP_EXAMPLE["org_id"],
                    "mfa_passed": False,
                    "mfa_required": False,
                    "csrf_token": "q0Jm2pQe8v1XcT7bYk3sLw9RfH5nZa4dGu6iEo2MjNc",
                    "expires_at": "2026-11-12T09:30:00Z",
                }
            ]
        }
    )

    user: SessionUser
    memberships: list[SessionMembership]
    active_org_id: uuid.UUID | None
    mfa_passed: bool
    mfa_required: bool
    csrf_token: str
    expires_at: datetime


class SignupOrg(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [_ORG_EXAMPLE]})

    id: uuid.UUID
    name: str
    slug: str
    plan_id: str
    status: OrgStatus
    created_at: datetime

    @classmethod
    def of(cls, org: Org) -> SignupOrg:
        return cls(
            id=org.id,
            name=org.name,
            slug=org.slug,
            plan_id=org.plan_id,
            status=org.status,
            created_at=org.created_at,
        )


class SignupResult(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={"examples": [{"user": _UNVERIFIED_USER_EXAMPLE, "org": _ORG_EXAMPLE}]}
    )

    user: SessionUser
    org: SignupOrg


class VerifyEmailResult(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"user": _USER_EXAMPLE}]})

    user: SessionUser


class ResendResult(BaseModel):
    model_config = ConfigDict(json_schema_extra={"examples": [{"sent": True, "email_verified": False}]})

    sent: bool
    email_verified: bool
