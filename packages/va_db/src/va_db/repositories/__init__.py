"""Repositories (specs/03-data-model.md §2): the generic :class:`TenantRepository`, the tenant-scoped
:class:`IdempotencyRepo` (specs/04 §12), :class:`MembershipRepo` and :class:`OrgDataKeyRepo`, the identity
:class:`IdentityRepo` (``users``, ``sessions``, ``email_tokens``; specs/04 §4) with :class:`MfaRepo` (MFA
enrolment on ``users``; specs/04 §4.5), :class:`SsoConnectionRepo` (an org's OIDC connection; specs/04 §5),
the envelope's :class:`OrgDataKeyStore` (specs/28 §3), :class:`OrgRepo` (``orgs`` on the global session;
specs/04 §8.1), the tenant :class:`ApiKeyRepo`, and the system-scope ones.

System repositories run on ``Database.system_session()`` (``va_system``, ``BYPASSRLS``); specs/03 §2 lists
every one of them, and only those may exist. :class:`IdentityRepo` works on ``Database.global_session()`` or
on a tenant session that writes tenant rows in the same transaction.
"""

from __future__ import annotations

from va_db.repositories.api_keys import ApiKeyLookupRepo, ApiKeyRecord, ApiKeyRepo
from va_db.repositories.base import (
    IMMUTABLE_COLUMNS,
    NotFoundError,
    SystemRepository,
    TenantRepository,
    conflict_savepoint,
    is_unique_violation,
    require_positive,
)
from va_db.repositories.billing_reconcile import BalanceDrift, BillingReconcileRepo
from va_db.repositories.data_keys import OrgDataKeyRepo, OrgDataKeyStore
from va_db.repositories.idempotency import (
    LOCK_TTL,
    REPLAY_WINDOW,
    ClaimOutcome,
    IdempotencyClaim,
    IdempotencyConflict,
    IdempotencyReplay,
    IdempotencyRepo,
    StoredRow,
    Takeover,
    resolve_existing,
)
from va_db.repositories.identity import (
    EMAIL_VERIFY_TTL,
    PASSWORD_RESET_TTL,
    IdentityRepo,
    IssuedSession,
    NewUser,
)
from va_db.repositories.identity_mfa import MfaRepo
from va_db.repositories.memberships import (
    LAST_OWNER_MESSAGE,
    NOT_OWNER_MESSAGE,
    MemberRecord,
    MembershipLookupRepo,
    MembershipRecord,
    MembershipRepo,
)
from va_db.repositories.orgs import OrgOverview, OrgRepo
from va_db.repositories.outbox import CLAIM_LIMIT, DEFAULT_LEASE, OutboxRepo
from va_db.repositories.retention import PURGE_RULES, PurgeRule, RetentionPolicy, RetentionRepo
from va_db.repositories.routing import PhoneNumberRoutingRepo
from va_db.repositories.scheduler import SchedulerRepo
from va_db.repositories.sso import SsoConnectionLookupRepo, SsoConnectionRepo, SsoDomainClaim
from va_db.repositories.telnyx_events import NewTelnyxEvent, TelnyxEventRepo

__all__ = [
    "CLAIM_LIMIT",
    "DEFAULT_LEASE",
    "EMAIL_VERIFY_TTL",
    "IMMUTABLE_COLUMNS",
    "LAST_OWNER_MESSAGE",
    "LOCK_TTL",
    "NOT_OWNER_MESSAGE",
    "PASSWORD_RESET_TTL",
    "PURGE_RULES",
    "REPLAY_WINDOW",
    "ApiKeyLookupRepo",
    "ApiKeyRecord",
    "ApiKeyRepo",
    "BalanceDrift",
    "BillingReconcileRepo",
    "ClaimOutcome",
    "IdempotencyClaim",
    "IdempotencyConflict",
    "IdempotencyReplay",
    "IdempotencyRepo",
    "IdentityRepo",
    "IssuedSession",
    "MemberRecord",
    "MembershipLookupRepo",
    "MembershipRecord",
    "MembershipRepo",
    "MfaRepo",
    "NewTelnyxEvent",
    "NewUser",
    "NotFoundError",
    "OrgDataKeyRepo",
    "OrgDataKeyStore",
    "OrgOverview",
    "OrgRepo",
    "OutboxRepo",
    "PhoneNumberRoutingRepo",
    "PurgeRule",
    "RetentionPolicy",
    "RetentionRepo",
    "SchedulerRepo",
    "SsoConnectionLookupRepo",
    "SsoConnectionRepo",
    "SsoDomainClaim",
    "StoredRow",
    "SystemRepository",
    "Takeover",
    "TelnyxEventRepo",
    "TenantRepository",
    "conflict_savepoint",
    "is_unique_violation",
    "require_positive",
    "resolve_existing",
]
