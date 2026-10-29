"""Request and response bodies of ``/internal/auth/invitations`` (specs/04-auth-tenancy-rbac.md §4.1, §6)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from va_api.routers.auth_schemas import DisplayName, EmailLinkToken, SessionUser
from va_db.models import MemberRole

__all__ = ["AcceptInvitationRequest", "InvitationAccepted", "InvitationDetails"]

_TOKEN_EXAMPLE: Final = "Vq3nT8bLw0eZ5yRk2mXc7hJd4sGa9uPf1oNi6tWe8Bq"  # noqa: S105 - documentation example
_ORG_ID_EXAMPLE: Final = "0192f3c4-5a6b-7c8d-9e0f-2b3c4d5e6f70"


class InvitationDetails(BaseModel):
    """What the invitation page shows before accepting."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "org_name": "Acme Dental",
                    "role": "developer",
                    "email": "priya.nair@acme-dental.test",
                    "inviter_name": "Jane Park",
                    "expires_at": "2026-11-05T09:12:40Z",
                    "account_exists": False,
                }
            ]
        }
    )

    org_name: str
    role: MemberRole
    email: str
    inviter_name: str
    expires_at: datetime
    account_exists: bool = Field(
        description="Whether a user with the email exists: sign in to accept, otherwise create the account."
    )


class AcceptInvitationRequest(BaseModel):
    """``POST /internal/auth/invitations/accept``; ``name`` and ``password`` create a new account."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {"token": _TOKEN_EXAMPLE, "name": "Priya Nair", "password": "quiet harbor lantern 7"},
                {"token": _TOKEN_EXAMPLE},
            ]
        },
    )

    token: EmailLinkToken
    name: DisplayName | None = None
    password: str | None = Field(default=None, min_length=1, max_length=1024, repr=False)


class InvitationAccepted(BaseModel):
    """The user who accepted, and the org they joined (now the session's active org)."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "user": {
                        "id": "0192f3c4-5a6b-7c8d-9e0f-1a2b3c4d5e6f",
                        "email": "priya.nair@acme-dental.test",
                        "name": "Priya Nair",
                        "email_verified": True,
                        "mfa_enabled": False,
                        "is_platform_admin": False,
                        "created_at": "2026-10-29T09:30:00Z",
                    },
                    "org_id": _ORG_ID_EXAMPLE,
                    "org_name": "Acme Dental",
                    "role": "developer",
                }
            ]
        }
    )

    user: SessionUser
    org_id: uuid.UUID
    org_name: str
    role: MemberRole
