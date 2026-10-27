"""Repositories (specs/03-data-model.md §2): the generic :class:`TenantRepository` and the system-scope ones.

System repositories run on ``Database.system_session()`` (``va_system``, ``BYPASSRLS``); specs/03 §2 lists
every one of them, and only those may exist.
"""

from __future__ import annotations

from va_db.repositories.base import (
    IMMUTABLE_COLUMNS,
    NotFoundError,
    SystemRepository,
    TenantRepository,
    is_unique_violation,
    require_positive,
)
from va_db.repositories.billing_reconcile import BalanceDrift, BillingReconcileRepo
from va_db.repositories.outbox import CLAIM_LIMIT, DEFAULT_LEASE, OutboxRepo
from va_db.repositories.retention import PURGE_RULES, PurgeRule, RetentionPolicy, RetentionRepo
from va_db.repositories.routing import PhoneNumberRoutingRepo
from va_db.repositories.scheduler import SchedulerRepo
from va_db.repositories.telnyx_events import NewTelnyxEvent, TelnyxEventRepo

__all__ = [
    "CLAIM_LIMIT",
    "DEFAULT_LEASE",
    "IMMUTABLE_COLUMNS",
    "PURGE_RULES",
    "BalanceDrift",
    "BillingReconcileRepo",
    "NewTelnyxEvent",
    "NotFoundError",
    "OutboxRepo",
    "PhoneNumberRoutingRepo",
    "PurgeRule",
    "RetentionPolicy",
    "RetentionRepo",
    "SchedulerRepo",
    "SystemRepository",
    "TelnyxEventRepo",
    "TenantRepository",
    "is_unique_violation",
    "require_positive",
]
