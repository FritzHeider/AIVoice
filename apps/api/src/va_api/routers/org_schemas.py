"""Bodies of ``/v1/me`` and ``/v1/org`` (specs/04-auth-tenancy-rbac.md §8.1) and the effective org limits.

:func:`effective_limits` projects ``va_billing.plans.effective_limits`` (the plan's rates and caps with the
``orgs.limits`` overrides of specs/24-billing-and-limits.md §2) onto the public ``OrgLimits`` fields.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from va_api.auth.context import PrincipalType
from va_billing import plans as billing_plans
from va_db.models import MemberRole, Plan
from va_domain.org import OrgSettings

__all__ = [
    "MeApiKey",
    "MeUser",
    "MeView",
    "OrgLimits",
    "OrgPatch",
    "OrgSummary",
    "OrgView",
    "effective_limits",
]

_ORG_ID: Final = "0192f1d2-7b3c-7a10-9c4e-5d6f7a8b9c0d"
_SUMMARY_EXAMPLE: Final[dict[str, JsonValue]] = {
    "id": _ORG_ID,
    "name": "Acme Dental",
    "slug": "acme-dental-k3q9",
    "plan_id": "build",
    "status": "active",
}
_LIMITS_EXAMPLE: Final[dict[str, JsonValue]] = {
    "connected_rate_micros_per_min": 100000,
    "transfer_rate_micros_per_min": 40000,
    "sms_rate_micros": 20000,
    "widget_message_micros": 10000,
    "daily_calls": 2000,
    "hourly_calls": 1000,
    "concurrency": 50,
    "voice_clones": 5,
    "numbers": None,
    "international_enabled": None,
    "sms_per_day": None,
}
_ORG_EXAMPLE: Final[dict[str, JsonValue]] = {
    **_SUMMARY_EXAMPLE,
    "parent_org_id": None,
    "kyc_level": 0,
    "data_region": "us",
    "retention_days": 365,
    "recording_retention_days": 90,
    "credits_balance": 12.5,
    "limits": _LIMITS_EXAMPLE,
    "settings": {"timezone": "America/New_York", "default_record": True},
    "created_at": "2026-10-29T14:03:11Z",
    "updated_at": "2026-10-29T14:03:11Z",
}
_ME_EXAMPLE: Final[dict[str, JsonValue]] = {
    "principal_type": "user",
    "user": {
        "id": "0192f1d2-7b3c-7a10-9c4e-5d6f7a8b9c0e",
        "email": "jane@acme-dental.test",
        "name": "Jane Park",
        "mfa_enabled": True,
    },
    "api_key": None,
    "org": _SUMMARY_EXAMPLE,
    "role": "owner",
    "scopes": ["agents:read", "agents:write", "analytics:read", "org:read", "org:write"],
    "acting_parent_org_id": None,
}


class OrgSummary(BaseModel):
    """The org a principal acts in."""

    model_config = ConfigDict(json_schema_extra={"examples": [_SUMMARY_EXAMPLE]})

    id: uuid.UUID
    name: str
    slug: str
    plan_id: str
    status: str


class MeUser(BaseModel):
    """The signed-in dashboard user."""

    id: uuid.UUID
    email: str
    name: str
    mfa_enabled: bool


class MeApiKey(BaseModel):
    """The API key making the request (its prefix only, never the secret)."""

    id: uuid.UUID
    prefix: str = Field(description="First 12 characters of the key, e.g. `va_live_3kQ9`.")
    scopes: list[str]
    environment: str


class MeView(BaseModel):
    """``GET /v1/me``: who is calling. ``user`` is null for API keys and ``api_key`` null for sessions."""

    model_config = ConfigDict(json_schema_extra={"examples": [_ME_EXAMPLE]})

    principal_type: PrincipalType
    user: MeUser | None
    api_key: MeApiKey | None
    org: OrgSummary
    role: MemberRole | None = Field(description="The member role of a session principal; null for API keys.")
    scopes: list[str] = Field(description="Granted scopes, sorted.")
    acting_parent_org_id: uuid.UUID | None = Field(
        description="The parent org while a parent principal acts on this sub-account (X-VA-Org-Id or "
        "switch-org); null otherwise."
    )


class OrgLimits(BaseModel):
    """Effective limits: the plan's rates and caps overlaid by the org's overrides; null caps: unlimited."""

    model_config = ConfigDict(json_schema_extra={"examples": [_LIMITS_EXAMPLE]})

    connected_rate_micros_per_min: int | None
    transfer_rate_micros_per_min: int | None
    sms_rate_micros: int | None
    widget_message_micros: int | None
    daily_calls: int | None
    hourly_calls: int | None
    concurrency: int | None
    voice_clones: int | None
    numbers: int | None = Field(description="Phone number cap override; null when not overridden.")
    international_enabled: bool | None = Field(description="International calling override; null when unset.")
    sms_per_day: int | None = Field(description="Daily SMS cap override; null when not overridden.")


class OrgView(BaseModel):
    """``GET /v1/org``: the org the principal acts in."""

    model_config = ConfigDict(json_schema_extra={"examples": [_ORG_EXAMPLE]})

    id: uuid.UUID
    name: str
    slug: str
    parent_org_id: uuid.UUID | None
    plan_id: str
    kyc_level: int
    status: str
    data_region: str
    retention_days: int
    recording_retention_days: int
    credits_balance: float = Field(description="Prepaid balance in dollars.")
    limits: OrgLimits
    settings: OrgSettings = Field(description="Every org setting, defaults filled (specs/04 §7).")
    created_at: datetime
    updated_at: datetime


class OrgPatch(BaseModel):
    """``PATCH /v1/org``: omitted fields stay unchanged; the retention fields need ``org.compliance``."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "name": "Acme Dental West",
                    "retention_days": 180,
                    "settings": {"timezone": "America/Denver"},
                }
            ]
        },
    )

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)] | None = None
    retention_days: Annotated[
        int | None, Field(ge=1, le=3650, description="Days call data is kept (needs `org.compliance`).")
    ] = None
    recording_retention_days: Annotated[
        int | None, Field(ge=0, le=3650, description="Days recordings are kept (needs `org.compliance`).")
    ] = None
    settings: OrgSettings = Field(
        default_factory=OrgSettings,
        description="Partial OrgSettings; omitted keys stay unchanged; compliance settings need "
        "`org.compliance` (as `PATCH /v1/org/settings`).",
    )


def effective_limits(plan: Plan | None, overrides: Mapping[str, object]) -> OrgLimits:
    """The plan's values (all null without a plan) with every well-typed override of ``overrides`` applied."""
    limits = billing_plans.effective_limits(plan, overrides)
    return OrgLimits(**{name: getattr(limits, name) for name in OrgLimits.model_fields})
