"""Request and response bodies of ``/v1/api_keys`` (specs/04-auth-tenancy-rbac.md §9).

A key is shown by its 12-character prefix; the peppered hash never leaves the database and the plaintext
appears only in the create and roll responses (``key``), ``null`` on an ``Idempotency-Key`` replay.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from va_db.models import ApiKey, ApiKeyEnvironment

__all__ = [
    "DEFAULT_EXPIRE_OLD_IN_HOURS",
    "MAX_EXPIRE_OLD_IN_HOURS",
    "MAX_SCOPES",
    "ApiKeyCreate",
    "ApiKeyCreated",
    "ApiKeyPatch",
    "ApiKeyRoll",
    "ApiKeyView",
    "api_key_created",
    "api_key_view",
]

MAX_SCOPES: Final = 50
DEFAULT_EXPIRE_OLD_IN_HOURS: Final = 24
MAX_EXPIRE_OLD_IN_HOURS: Final = 168

_KEY_EXAMPLE: Final[dict[str, JsonValue]] = {
    "id": "0192f4b1-7a2c-7d3e-9f40-5a6b7c8d9e0f",
    "name": "Zapier integration",
    "prefix": "va_live_3kQ9",
    "scopes": ["calls:read", "calls:write"],
    "environment": "live",
    "created_by": "0192f1d2-8c4d-7b21-8d5f-6e7a8b9c0d1e",
    "last_used_at": "2026-10-29T15:42:08Z",
    "expires_at": "2027-04-29T00:00:00Z",
    "revoked_at": "2026-10-30T08:15:00Z",
    "created_at": "2026-10-29T14:03:11Z",
}
_CREATED_EXAMPLE: Final[dict[str, JsonValue]] = {
    **{name: value for name, value in _KEY_EXAMPLE.items() if name not in {"last_used_at", "revoked_at"}},
    "key": "va_live_3kQ9xT2mLpR7vB1nZc8dHf4sWq6yJa0e",
}

KeyName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class ApiKeyCreate(BaseModel):
    """``POST /v1/api_keys``: name, scopes (default every scope), environment, and an optional expiry."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "Zapier integration",
                    "scopes": ["calls:read", "calls:write"],
                    "environment": "live",
                    "expires_at": "2027-04-29T00:00:00Z",
                }
            ]
        },
    )

    name: KeyName = Field(description="Label shown in the dashboard (1-100 characters).")
    scopes: list[str] = Field(
        default_factory=lambda: ["*"],
        min_length=1,
        max_length=MAX_SCOPES,
        description=(
            "Scopes of the new key (specs/04 §2.3); each must be one the caller holds. "
            'A list containing "*" is stored as ["*"].'
        ),
    )
    environment: ApiKeyEnvironment | None = Field(
        default=None, description="`live` or `test`; default: the caller's environment."
    )
    expires_at: AwareDatetime | None = Field(
        default=None,
        description="When the key stops working (with a timezone, in the future); default: never.",
    )


class ApiKeyPatch(BaseModel):
    """``PATCH /v1/api_keys/{key_id}``: a new name; an empty body changes nothing."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"name": "Zapier (prod)"}]})

    name: KeyName | None = Field(default=None, description="New label (1-100 characters).")


class ApiKeyRoll(BaseModel):
    """``POST /v1/api_keys/{key_id}/roll``: how long the old key keeps working."""

    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"expire_old_in_hours": 24}]})

    expire_old_in_hours: int = Field(
        default=DEFAULT_EXPIRE_OLD_IN_HOURS,
        ge=0,
        le=MAX_EXPIRE_OLD_IN_HOURS,
        description="Hours until the old key expires (0 = at once); an earlier existing expiry is kept.",
    )


class ApiKeyView(BaseModel):
    """An API key as listed: its prefix, never its hash or plaintext."""

    model_config = ConfigDict(json_schema_extra={"examples": [_KEY_EXAMPLE]})

    id: uuid.UUID
    name: str
    prefix: str = Field(description="First 12 characters of the key, e.g. `va_live_3kQ9`.")
    scopes: list[str]
    environment: ApiKeyEnvironment
    created_by: uuid.UUID | None = Field(description="The user who created the key, when known.")
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class ApiKeyCreated(ApiKeyView):
    """A key just created or rolled, with its plaintext ``key`` shown only in this response."""

    model_config = ConfigDict(json_schema_extra={"examples": [_CREATED_EXAMPLE]})

    key: str | None = Field(
        description=(
            "The API key, shown only in this response; store it now. "
            "null on an Idempotency-Key replay: the key is never stored, so a lost key must be rolled."
        )
    )


def api_key_view(row: ApiKey) -> ApiKeyView:
    """The response view of an ``api_keys`` row."""
    return ApiKeyView(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        scopes=list(row.scopes),
        environment=row.environment,
        created_by=row.created_by,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        created_at=row.created_at,
    )


def api_key_created(row: ApiKey, plaintext: str) -> ApiKeyCreated:
    """The response view of a freshly minted key with its plaintext."""
    return ApiKeyCreated(**api_key_view(row).model_dump(), key=plaintext)
