"""Request and response bodies of the agent onboarding API (specs/04-auth-tenancy-rbac.md §10).

The public side (``/v1/agent-onboarding/keys``) is what an agent calls: create a request, then poll it with
``X-Poll-Secret``. The dashboard side (``/internal/auth/agent-onboarding/{token}``) is what the human who got
the email uses to see, approve, or deny it. The plaintext key appears only in the one poll that delivers it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from va_api.auth.agent_onboarding import PollStatus
from va_db.models import MemberRole

__all__ = [
    "MAX_AGENT_NAME",
    "MAX_PURPOSE",
    "MAX_REQUESTED_SCOPES",
    "AgentApproval",
    "AgentApproveRequest",
    "AgentDenial",
    "AgentKeyPoll",
    "AgentKeyRequestCreate",
    "AgentKeyRequestCreated",
    "AgentRequestDetails",
    "AgentRequestOrg",
]

MAX_AGENT_NAME: Final = 100
MAX_PURPOSE: Final = 500
MAX_REQUESTED_SCOPES: Final = 50

_REQUEST_ID: Final = "0192f5c3-1b2d-7e4f-8a90-b1c2d3e4f5a6"
_ORG_ID: Final = "0192f1d2-8c4d-7b21-8d5f-6e7a8b9c0d1e"
_KEY_ID: Final = "0192f5c9-4d1e-7a2b-9c3d-e4f5a6b7c8d9"
_SCOPES_EXAMPLE: Final[list[JsonValue]] = ["calls:read", "calls:write", "pathways:write"]

AgentEmail = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    ),
]
AgentName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_AGENT_NAME)]
Purpose = Annotated[str, StringConstraints(strip_whitespace=True, max_length=MAX_PURPOSE)]


class AgentKeyRequestCreate(BaseModel):
    """``POST /v1/agent-onboarding/keys``: whose approval to ask for, and what for."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "email": "jane@acme-dental.test",
                    "agent_name": "Claude Code",
                    "requested_scopes": _SCOPES_EXAMPLE,
                    "purpose": "Build the appointment reminder pathway for Acme Dental.",
                }
            ]
        },
    )

    email: AgentEmail = Field(description="Address of the human who approves the key.")
    agent_name: AgentName = Field(description="Name of the agent, shown in the email (1-100 characters).")
    requested_scopes: list[str] = Field(
        min_length=1,
        max_length=MAX_REQUESTED_SCOPES,
        description=(
            "Scopes the key should hold (specs/04 §2.3), excluding `*`, `billing:read`, `billing:write`, and "
            "`api_keys:write`."
        ),
    )
    purpose: Purpose = Field(default="", description="Why the agent needs the key (at most 500 characters).")


class AgentKeyRequestCreated(BaseModel):
    """A new request: poll ``poll_url`` with the ``poll_secret`` (shown only here) as ``X-Poll-Secret``."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "request_id": _REQUEST_ID,
                    "status": "pending_verification",
                    "poll_url": f"/v1/agent-onboarding/keys/{_REQUEST_ID}",
                    "poll_secret": "Qm9sZC1wb2xsLXNlY3JldC1leGFtcGxlLXZhbHVlLTE",
                    "expires_at": "2026-10-29T15:03:11Z",
                }
            ]
        },
    )

    request_id: uuid.UUID
    status: Literal["pending_verification"]
    poll_url: str
    poll_secret: str = Field(description="Send as `X-Poll-Secret` when polling; returned only once.")
    expires_at: datetime = Field(description="Deadline for the human's approval (one hour).")


class AgentKeyPoll(BaseModel):
    """The state of a request; ``key`` is set in exactly one ``approved`` response, then the status reads
    ``delivered``."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "request_id": _REQUEST_ID,
                    "status": "approved",
                    "expires_at": "2026-10-29T15:03:11Z",
                    "scopes": ["calls:read", "calls:write"],
                    "org_id": _ORG_ID,
                    "key": "va_test_7HcQ2nLpR4vB9mZx1dKf8sWq3yJa6eTg",
                },
                {
                    "request_id": _REQUEST_ID,
                    "status": "pending_verification",
                    "expires_at": "2026-10-29T15:03:11Z",
                    "scopes": _SCOPES_EXAMPLE,
                    "org_id": None,
                    "key": None,
                },
            ]
        },
    )

    request_id: uuid.UUID
    status: PollStatus
    expires_at: datetime
    scopes: list[str] = Field(description="Requested scopes, or the granted ones once approved.")
    org_id: uuid.UUID | None = Field(description="The org the key belongs to, once approved.")
    key: str | None = Field(description="The plaintext test key, in the delivering response only.")


class AgentRequestOrg(BaseModel):
    """An org the signed-in user may approve the key into."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"org_id": _ORG_ID, "name": "Acme Dental", "role": "owner"}]}
    )

    org_id: uuid.UUID
    name: str
    role: MemberRole


class AgentRequestDetails(BaseModel):
    """What the approval page shows."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "request_id": _REQUEST_ID,
                    "agent_name": "Claude Code",
                    "purpose": "Build the appointment reminder pathway for Acme Dental.",
                    "requested_scopes": _SCOPES_EXAMPLE,
                    "expires_at": "2026-10-29T15:03:11Z",
                    "orgs": [{"org_id": _ORG_ID, "name": "Acme Dental", "role": "owner"}],
                }
            ]
        },
    )

    request_id: uuid.UUID
    agent_name: str
    purpose: str
    requested_scopes: list[str]
    expires_at: AwareDatetime
    orgs: list[AgentRequestOrg] = Field(
        description="Active orgs where the user is owner, admin, or developer."
    )


class AgentApproveRequest(BaseModel):
    """``POST …/approve``: the org to create the key in and, optionally, fewer scopes."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [{"org_id": _ORG_ID, "scopes": ["calls:read", "calls:write"]}]},
    )

    org_id: uuid.UUID
    scopes: list[str] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_REQUESTED_SCOPES,
        description="A non-empty subset of the requested scopes; default: all of them.",
    )


class AgentApproval(BaseModel):
    """An approved request; the key itself goes only to the polling agent."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "request_id": _REQUEST_ID,
                    "status": "approved",
                    "org_id": _ORG_ID,
                    "key_id": _KEY_ID,
                    "scopes": ["calls:read", "calls:write"],
                    "expires_at": "2026-11-28T14:10:00Z",
                }
            ]
        },
    )

    request_id: uuid.UUID
    status: Literal["approved"]
    org_id: uuid.UUID
    key_id: uuid.UUID
    scopes: list[str]
    expires_at: datetime = Field(description="When the key stops working (30 days after approval).")


class AgentDenial(BaseModel):
    """A denied request."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"request_id": _REQUEST_ID, "status": "denied"}]}
    )

    request_id: uuid.UUID
    status: Literal["denied"]
