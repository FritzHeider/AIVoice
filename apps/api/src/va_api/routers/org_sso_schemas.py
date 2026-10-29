"""Request and response bodies of ``/v1/org/sso`` (specs/04-auth-tenancy-rbac.md §5).

The client secret is accepted on input only; no response ever carries it in any form (specs/28 T8).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, SecretStr, field_validator

from va_api.auth.oidc_discovery import normalise_issuer
from va_api.auth.sso_urls import is_dns_domain

__all__ = [
    "MAX_ALLOWED_DOMAINS",
    "JitRole",
    "SsoConnectionDeleted",
    "SsoConnectionInput",
    "SsoConnectionView",
    "normalise_domains",
]

MAX_ALLOWED_DOMAINS: Final = 50

# JIT provisioning never mints owners (specs/04 §5 step 4).
type JitRole = Literal["admin", "developer", "analyst", "billing", "viewer"]

_ISSUER_EXAMPLE: Final = "https://login.acme-dental.test"
_VIEW_EXAMPLE: Final[dict[str, JsonValue]] = {
    "id": "0192f1d2-7b3c-7a10-9c4e-5d6f7a8b9c0d",
    "provider": "oidc",
    "issuer": _ISSUER_EXAMPLE,
    "client_id": "voiceagents-dashboard",
    "allowed_domains": ["acme-dental.test"],
    "default_role": "viewer",
    "enforce": False,
    "created_at": "2026-10-29T14:03:11Z",
    "updated_at": "2026-10-29T14:03:11Z",
}


def normalise_domains(values: list[str]) -> list[str]:
    """Stripped, lowercased, de-duplicated (first one wins) domains; ``ValueError`` on an invalid one."""
    result: list[str] = []
    for raw in values:
        domain = raw.strip().lower()
        if not is_dns_domain(domain):
            msg = f"{raw!r} is not a valid domain name"
            raise ValueError(msg)
        if domain not in result:
            result.append(domain)
    return result


class SsoConnectionInput(BaseModel):
    """``PUT /v1/org/sso``: the org's OIDC connection. ``client_secret`` may be omitted on update."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "issuer": _ISSUER_EXAMPLE,
                    "client_id": "voiceagents-dashboard",
                    "client_secret": "<client secret from your identity provider>",
                    "allowed_domains": ["acme-dental.test"],
                    "default_role": "viewer",
                    "enforce": False,
                }
            ]
        },
    )

    provider: Literal["oidc"] = "oidc"
    issuer: Annotated[str, Field(min_length=1, max_length=2048)]
    client_id: Annotated[str, Field(min_length=1, max_length=255)]
    client_secret: Annotated[SecretStr | None, Field(default=None, min_length=1, max_length=1024)] = None
    allowed_domains: Annotated[list[str], Field(min_length=1, max_length=MAX_ALLOWED_DOMAINS)]
    default_role: JitRole = "viewer"
    enforce: bool = False

    @field_validator("issuer")
    @classmethod
    def _issuer(cls, value: str) -> str:
        return normalise_issuer(value)

    @field_validator("allowed_domains")
    @classmethod
    def _domains(cls, value: list[str]) -> list[str]:
        return normalise_domains(value)


class SsoConnectionView(BaseModel):
    """The org's SSO connection without its client secret."""

    model_config = ConfigDict(json_schema_extra={"examples": [_VIEW_EXAMPLE]})

    id: uuid.UUID
    provider: Literal["oidc"]
    issuer: str
    client_id: str
    allowed_domains: list[str]
    default_role: str
    enforce: bool
    created_at: datetime
    updated_at: datetime


class SsoConnectionDeleted(BaseModel):
    """``DELETE /v1/org/sso`` result."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"id": "0192f1d2-7b3c-7a10-9c4e-5d6f7a8b9c0d", "deleted": True}]}
    )

    id: uuid.UUID
    deleted: Literal[True] = True
