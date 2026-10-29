"""Request and response bodies of ``/v1/org/members`` and ``/v1/org/transfer-ownership`` (specs/04 §6)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from va_db.models import MemberRole
from va_db.repositories import MemberRecord

__all__ = [
    "MemberRemoved",
    "MemberRolePatch",
    "MemberView",
    "OwnershipTransferred",
    "TransferOwnershipRequest",
    "member_view",
]

_USER_ID_EXAMPLE: Final = "0192f1d2-7b3c-7a10-9c4e-5d6f7a8b9c0d"
_MEMBER_EXAMPLE: Final[dict[str, JsonValue]] = {
    "user_id": _USER_ID_EXAMPLE,
    "email": "jane.park@acme-dental.test",
    "name": "Jane Park",
    "role": "admin",
    "mfa_enabled": True,
    "last_login_at": "2026-10-29T09:12:40Z",
    "joined_at": "2026-03-02T16:45:00Z",
}
_OWNER_EXAMPLE: Final[dict[str, JsonValue]] = {
    **_MEMBER_EXAMPLE,
    "user_id": "0192f1d2-8c4d-7b21-8d5f-6e7a8b9c0d1e",
    "email": "omar.haddad@acme-dental.test",
    "name": "Omar Haddad",
    "role": "owner",
}


class MemberView(BaseModel):
    """A member of the org."""

    model_config = ConfigDict(json_schema_extra={"examples": [_MEMBER_EXAMPLE]})

    user_id: uuid.UUID
    email: str
    name: str
    role: MemberRole
    mfa_enabled: bool
    last_login_at: datetime | None = Field(description="The user's last sign-in; null before the first one.")
    joined_at: datetime = Field(description="When the user became a member of this org.")


def member_view(record: MemberRecord) -> MemberView:
    """The response view of a repository record."""
    return MemberView(
        user_id=record.user_id,
        email=record.email,
        name=record.name,
        role=record.role,
        mfa_enabled=record.mfa_enabled,
        last_login_at=record.last_login_at,
        joined_at=record.joined_at,
    )


class MemberRolePatch(BaseModel):
    """``PATCH /v1/org/members/{user_id}``: the member's new role."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"role": "developer"}]})

    role: MemberRole


class MemberRemoved(BaseModel):
    """``DELETE /v1/org/members/{user_id}`` result."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"user_id": _USER_ID_EXAMPLE, "removed": True}]}
    )

    user_id: uuid.UUID
    removed: Literal[True] = True


class TransferOwnershipRequest(BaseModel):
    """``POST /v1/org/transfer-ownership``: the member who becomes an owner."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"user_id": _USER_ID_EXAMPLE}]})

    user_id: uuid.UUID


class OwnershipTransferred(BaseModel):
    """``POST /v1/org/transfer-ownership`` result: the caller (now an admin) and the new owner."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "previous_owner": {**_OWNER_EXAMPLE, "role": "admin"},
                    "owner": {**_MEMBER_EXAMPLE, "role": "owner"},
                }
            ]
        }
    )

    previous_owner: MemberView
    owner: MemberView
