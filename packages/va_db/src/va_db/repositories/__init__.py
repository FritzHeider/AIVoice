"""Repositories (specs/03-data-model.md §2): the generic :class:`TenantRepository`, the tenant-scoped
:class:`IdempotencyRepo` (specs/04 §12), and the system-scope ones.

System repositories run on ``Database.system_session()`` (``va_system``, ``BYPASSRLS``); specs/03 §2 lists
every one of them, and only those may exist.
"""

from __future__ import annotations

from va_db.repositories.api_keys import ApiKeyLookupRepo, ApiKeyRecord
from va_db.repositories.base import (
    IMMUTABLE_COLUMNS,
    NotFoundError,
    SystemRepository,
    TenantRepository,
    is_unique_violation,
    require_positive,
)
from va_db.repositories.billing_reconcile import BalanceDrift, BillingReconcileRepo
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
from va_db.repositories.outbox import CLAIM_LIMIT, DEFAULT_LEASE, OutboxRepo
from va_db.repositories.retention import PURGE_RULES, PurgeRule, RetentionPolicy, RetentionRepo
from va_db.repositories.routing import PhoneNumberRoutingRepo
from va_db.repositories.scheduler import SchedulerRepo
from va_db.repositories.telnyx_events import NewTelnyxEvent, TelnyxEventRepo

__all__ = [
    "CLAIM_LIMIT",
    "DEFAULT_LEASE",
    "IMMUTABLE_COLUMNS",
    "LOCK_TTL",
    "PURGE_RULES",
    "REPLAY_WINDOW",
    "ApiKeyLookupRepo",
    "ApiKeyRecord",
    "BalanceDrift",
    "BillingReconcileRepo",
    "ClaimOutcome",
    "IdempotencyClaim",
    "IdempotencyConflict",
    "IdempotencyReplay",
    "IdempotencyRepo",
    "NewTelnyxEvent",
    "NotFoundError",
    "OutboxRepo",
    "PhoneNumberRoutingRepo",
    "PurgeRule",
    "RetentionPolicy",
    "RetentionRepo",
    "SchedulerRepo",
    "StoredRow",
    "SystemRepository",
    "Takeover",
    "TelnyxEventRepo",
    "TenantRepository",
    "is_unique_violation",
    "require_positive",
    "resolve_existing",
]
