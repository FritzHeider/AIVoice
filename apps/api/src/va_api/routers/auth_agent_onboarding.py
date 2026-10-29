"""Agent key approval under ``/internal/auth/agent-onboarding/{token}`` (specs/04-auth-tenancy-rbac.md §10).

The dashboard page ``/agent-onboarding/[token]`` (specs/25) reads, approves, or denies the request an emailed
link addresses. The routes attach to :data:`va_api.routers.auth.internal_auth_router`; ``va_api.main`` imports
this module so they exist before the router is included. Every route needs a session whose user owns the
mailbox the request was sent to (verified email, same address); the link works for one hour and once.

Approving claims the request (compare-and-set ``pending_verification`` → ``approving``), then mints a
``test`` key named ``agent:<agent_name>`` that expires in 30 days in an org where the user is owner, admin, or
developer (MFA gate, org ``ip_allowlist``, and a developer grants only its role's scopes), writes it with the
``api_key.create`` audit row (``after.agent_name``) in one transaction, stores the key encrypted under the
org's data key for 15 minutes, and marks the request ``approved``. A failure before the commit hands the
claim back, so the link still works.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Final

from fastapi import Request

from va_api.audit import audit, audit_exempt, audited
from va_api.auth.agent_onboarding import (
    AGENT_KEY_LIFETIME,
    KEY_TTL,
    LINK_INVALID_MESSAGE,
    AgentRequest,
    StoredStatus,
    email_hash,
    encode_key_blob,
    key_aad,
)
from va_api.auth.api_keys import check_grantable, mint_api_key
from va_api.auth.context import RequestContext
from va_api.auth.ip_allowlist import enforce_ip_allowlist
from va_api.auth.rbac import scopes_for_role
from va_api.auth.session_auth import (
    MFA_REQUIRED_MESSAGE,
    agent_requests_of,
    api_key_hasher_of,
    client_ip,
    clock_of,
    database_of,
    envelope_of,
    memberships_of_user,
    mfa_checked_user,
    mfa_due,
)
from va_api.auth.sessions import ActiveSession
from va_api.middleware import request_id_of
from va_api.responses import DataResponse
from va_api.routers.agent_onboarding_schemas import (
    AgentApproval,
    AgentApproveRequest,
    AgentDenial,
    AgentRequestDetails,
    AgentRequestOrg,
)
from va_api.routers.api_keys_schemas import api_key_view
from va_api.routers.auth import PUBLIC_MARKER, CurrentSession, OptionalEmptyBody, internal_auth_router
from va_common.auth import looks_like_token
from va_common.errors import AppError
from va_db.models import User
from va_db.repositories import ApiKeyRepo, MembershipRecord

__all__ = [
    "APPROVER_ROLES",
    "NOT_ELIGIBLE_MESSAGE",
    "OTHER_EMAIL_MESSAGE",
    "SCOPES_NOT_REQUESTED_MESSAGE",
    "UNVERIFIED_EMAIL_MESSAGE",
    "agent_request_approve",
    "agent_request_deny",
    "agent_request_details",
]

# Roles that may approve an agent key into their org (specs/04 §10 step 2).
APPROVER_ROLES: Final = frozenset({"owner", "admin", "developer"})
OTHER_EMAIL_MESSAGE: Final = "This request was sent to a different email address."
UNVERIFIED_EMAIL_MESSAGE: Final = "Verify your email address before approving agent keys."
NOT_ELIGIBLE_MESSAGE: Final = "You cannot create API keys in this organization."
SCOPES_NOT_REQUESTED_MESSAGE: Final = "Scopes must be a subset of the requested scopes."

_PATH: Final = "/agent-onboarding/{token}"
_APPROVE_EXTRA: Final = {**PUBLIC_MARKER, **audited("api_key.create")}
_DENY_EXTRA: Final = {**PUBLIC_MARKER, **audit_exempt("no org is chosen; the request lives only in Redis")}


def _link_invalid() -> AppError:
    return AppError("TOKEN_EXPIRED", LINK_INVALID_MESSAGE, field="token")


async def _usable(request: Request, token: str, user: User) -> AgentRequest:
    """The pending request of ``token`` addressed to ``user``; ``TOKEN_EXPIRED``, ``FORBIDDEN``, or
    ``EMAIL_NOT_VERIFIED`` otherwise."""
    if not looks_like_token(token):
        raise _link_invalid()
    stored = await agent_requests_of(request).by_token(token)
    if stored is None or not stored.approvable(clock_of(request).now()):
        raise _link_invalid()
    if email_hash(user.email) != stored.email_hash:
        raise AppError("FORBIDDEN", OTHER_EMAIL_MESSAGE)
    if user.email_verified_at is None:
        raise AppError("EMAIL_NOT_VERIFIED", UNVERIFIED_EMAIL_MESSAGE)
    return stored


def _eligible(records: list[MembershipRecord]) -> list[MembershipRecord]:
    """Direct memberships with an approving role in an active org."""
    return [
        record
        for record in records
        if record.role in APPROVER_ROLES
        and record.gate_status == "active"
        and record.acting_parent_org_id is None
    ]


@internal_auth_router.get(_PATH, operation_id="auth_agent_request_get", openapi_extra=PUBLIC_MARKER)
async def agent_request_details(
    token: str, request: Request, active: CurrentSession
) -> DataResponse[AgentRequestDetails]:
    """The agent, its purpose and scopes, and the orgs the signed-in user may approve the key into."""
    user = await mfa_checked_user(request, active)
    stored = await _usable(request, token, user)
    orgs = [
        AgentRequestOrg(org_id=record.org_id, name=record.org_name, role=record.role)
        for record in _eligible(await memberships_of_user(request, user.id))
    ]
    details = AgentRequestDetails(
        request_id=stored.request_id,
        agent_name=stored.agent_name,
        purpose=stored.purpose,
        requested_scopes=list(stored.scopes),
        expires_at=stored.expires_at,
        orgs=orgs,
    )
    return DataResponse(data=details)


def _granted_scopes(stored: AgentRequest, chosen: list[str] | None) -> list[str]:
    """The requested scopes, or the chosen non-empty subset of them (``INVALID_INPUT`` on ``scopes``)."""
    if chosen is None:
        return list(stored.scopes)
    unique = set(chosen)
    if not unique <= set(stored.scopes):
        raise AppError("INVALID_INPUT", SCOPES_NOT_REQUESTED_MESSAGE, field="scopes")
    return sorted(unique)


async def _approver_context(
    request: Request, active: ActiveSession, user: User, org_id: uuid.UUID
) -> RequestContext:
    """The user principal in ``org_id`` after the eligibility, MFA, and IP allowlist gates."""
    record = next(
        (item for item in _eligible(await memberships_of_user(request, user.id)) if item.org_id == org_id),
        None,
    )
    if record is None:
        raise AppError("FORBIDDEN", NOT_ELIGIBLE_MESSAGE)
    if mfa_due(org_requires=record.mfa_required, user_enabled=user.mfa_enabled, passed=active.mfa_passed):
        raise AppError("MFA_REQUIRED", MFA_REQUIRED_MESSAGE)
    ctx = RequestContext(
        request_id=request_id_of(request),
        org_id=record.org_id,
        principal_type="user",
        principal_id=user.id,
        role=record.role,
        scopes=scopes_for_role(record.role),
        environment="live",
        plan_id=record.plan_id,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
        acting_parent_org_id=None,
    )
    await enforce_ip_allowlist(request, ctx)
    return ctx


@dataclass(frozen=True, slots=True)
class _Minted:
    """A committed agent key: the request as it reads once approved, and the encrypted plaintext."""

    approved: AgentRequest
    key_id: uuid.UUID
    expires_at: datetime
    blob: bytes = field(repr=False)


async def _mint(request: Request, ctx: RequestContext, stored: AgentRequest, scopes: list[str]) -> _Minted:
    """Create the key and its ``api_key.create`` audit row in one transaction; encrypt its plaintext."""
    now = clock_of(request).now()
    expires_at = now + AGENT_KEY_LIFETIME
    minted = mint_api_key(api_key_hasher_of(request), "test")
    blob = await envelope_of(request).encrypt_str(ctx.org_id, minted.plaintext, key_aad(stored.request_id))
    async with database_of(request).tenant_session(ctx.org_id) as session:
        row = await ApiKeyRepo(session, ctx.org_id).add(
            name=f"agent:{stored.agent_name}",
            prefix=minted.prefix,
            secret_hash=minted.secret_hash,
            pepper_version=minted.pepper_version,
            scopes=scopes,
            environment="test",
            created_by=ctx.principal_id,
            expires_at=expires_at,
        )
        after = {**api_key_view(row).model_dump(), "agent_name": stored.agent_name}
        await audit(session, ctx, "api_key.create", "api_key", row.id, after=after)
        key_id = row.id
    approved = replace(
        stored,
        status="approved",
        approver_user_id=ctx.principal_id,
        org_id=ctx.org_id,
        key_id=key_id,
        scopes=tuple(scopes),
        key_expires_at=now + KEY_TTL,
    )
    return _Minted(approved=approved, key_id=key_id, expires_at=expires_at, blob=blob)


def _with_status(status: StoredStatus) -> Callable[[AgentRequest], AgentRequest]:
    return lambda current: replace(current, status=status)


@internal_auth_router.post(
    f"{_PATH}/approve", operation_id="auth_agent_request_approve", openapi_extra=_APPROVE_EXTRA
)
async def agent_request_approve(
    token: str, body: AgentApproveRequest, request: Request, active: CurrentSession
) -> DataResponse[AgentApproval]:
    """Create the agent's test key in ``org_id``; the agent receives it on its next poll."""
    user = await mfa_checked_user(request, active)
    stored = await _usable(request, token, user)
    scopes = _granted_scopes(stored, body.scopes)
    ctx = await _approver_context(request, active, user, body.org_id)
    check_grantable(ctx, scopes)
    store = agent_requests_of(request)
    claimed = await store.transition(stored.request_id, "pending_verification", _with_status("approving"))
    if claimed is None:
        raise _link_invalid()
    try:
        minted = await _mint(request, ctx, claimed, scopes)
    except BaseException:
        await store.transition(stored.request_id, "approving", _with_status("pending_verification"))
        raise
    await store.put_key(stored.request_id, encode_key_blob(minted.blob))
    await store.transition(stored.request_id, "approving", lambda _current: minted.approved)
    await store.drop_token(stored.token_hash)
    result = AgentApproval(
        request_id=stored.request_id,
        status="approved",
        org_id=ctx.org_id,
        key_id=minted.key_id,
        scopes=scopes,
        expires_at=minted.expires_at,
    )
    return DataResponse(data=result)


@internal_auth_router.post(f"{_PATH}/deny", operation_id="auth_agent_request_deny", openapi_extra=_DENY_EXTRA)
async def agent_request_deny(
    token: str, request: Request, active: CurrentSession, _body: OptionalEmptyBody = None
) -> DataResponse[AgentDenial]:
    """Refuse the request; the agent's next poll reads ``denied``."""
    user = await mfa_checked_user(request, active)
    stored = await _usable(request, token, user)
    store = agent_requests_of(request)
    denied = await store.transition(stored.request_id, "pending_verification", _with_status("denied"))
    if denied is None:
        raise _link_invalid()
    await store.drop_token(stored.token_hash)
    return DataResponse(data=AgentDenial(request_id=stored.request_id, status="denied"))
