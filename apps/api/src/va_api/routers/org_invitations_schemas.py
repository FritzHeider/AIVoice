"""Request and response bodies of ``/v1/org/members/invitations`` (specs/04-auth-tenancy-rbac.md §6)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, field_validator

from va_db.models import Invitation, MemberRole

__all__ = [
    "INVALID_EMAIL_MESSAGE",
    "InvitationCreate",
    "InvitationRevoked",
    "InvitationStatus",
    "InvitationView",
    "invitation_view",
]

INVALID_EMAIL_MESSAGE: Final = "Enter a valid email address."

type InvitationStatus = Literal["pending", "expired"]

_INVITATION_ID_EXAMPLE: Final = "0192f1d2-9d5e-7c32-8e6f-7f8a9b0c1d2e"
_INVITATION_EXAMPLE: Final[dict[str, JsonValue]] = {
    "id": _INVITATION_ID_EXAMPLE,
    "email": "priya.nair@acme-dental.test",
    "role": "developer",
    "invited_by": "0192f1d2-8c4d-7b21-8d5f-6e7a8b9c0d1e",
    "status": "pending",
    "expires_at": "2026-11-05T09:12:40Z",
    "created_at": "2026-10-29T09:12:40Z",
}


class InvitationCreate(BaseModel):
    """``POST /v1/org/members/invitations``: who to invite and with which role."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"email": "priya.nair@acme-dental.test", "role": "developer"}]},
    )

    email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=320)]
    role: MemberRole

    @field_validator("email")
    @classmethod
    def _one_at_sign(cls, value: str) -> str:
        local, at, domain = value.partition("@")
        if not at or not local or not domain or "@" in domain:
            raise ValueError(INVALID_EMAIL_MESSAGE)
        return value


class InvitationView(BaseModel):
    """An unaccepted invitation; its token is never returned."""

    model_config = ConfigDict(json_schema_extra={"examples": [_INVITATION_EXAMPLE]})

    id: uuid.UUID
    email: str
    role: MemberRole
    invited_by: uuid.UUID = Field(description="The user who sent the invitation.")
    status: InvitationStatus = Field(description="`expired` once `expires_at` has passed.")
    expires_at: datetime
    created_at: datetime


def invitation_view(row: Invitation, now: datetime) -> InvitationView:
    """The response view of an invitation row at ``now``."""
    return InvitationView(
        id=row.id,
        email=row.email,
        role=row.role,
        invited_by=row.invited_by,
        status="pending" if row.expires_at > now else "expired",
        expires_at=row.expires_at,
        created_at=row.created_at,
    )


class InvitationRevoked(BaseModel):
    """``DELETE /v1/org/members/invitations/{invitation_id}`` result."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"id": _INVITATION_ID_EXAMPLE, "revoked": True}]}
    )

    id: uuid.UUID
    revoked: Literal[True] = True
