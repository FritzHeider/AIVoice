"""Response bodies of ``GET /v1/audit_logs`` (specs/04-auth-tenancy-rbac.md §13)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from va_db.models import AuditActorType, AuditLog

__all__ = ["AuditLogView", "audit_log_view"]

_VIEW_EXAMPLE: Final[dict[str, JsonValue]] = {
    "id": "0192f4c8-1d2e-7f30-8a41-2b3c4d5e6f70",
    "actor_type": "user",
    "actor_id": "0192f1d2-8c4d-7b21-8d5f-6e7a8b9c0d1e",
    "action": "member.role_change",
    "resource_type": "membership",
    "resource_id": "0192f3a0-4b5c-7d6e-8f70-1a2b3c4d5e6f",
    "ip": "203.0.113.24",
    "user_agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15",
    "changes": {"before": {"role": "viewer"}, "after": {"role": "developer"}},
    "created_at": "2026-10-29T14:03:11Z",
}


class AuditLogView(BaseModel):
    """One audit entry: who did what to which resource, from where, and the redacted before/after values."""

    model_config = ConfigDict(json_schema_extra={"examples": [_VIEW_EXAMPLE]})

    id: uuid.UUID
    actor_type: AuditActorType = Field(description="`user` (dashboard session), `api_key`, or `system`.")
    actor_id: uuid.UUID | None = Field(description="The user or API key id; null for system actions.")
    action: str = Field(description="One of the actions of specs/04 §13, e.g. `api_key.create`.")
    resource_type: str
    resource_id: str | None
    ip: str | None = Field(description="Client IP address of the request, when known.")
    user_agent: str | None
    changes: dict[str, Any] = Field(
        description=(
            '`{"before": ..., "after": ...}` with secrets replaced by "[REDACTED]"; '
            "`acting_parent_org_id` when a parent org acted on this sub-account."
        )
    )
    created_at: datetime


def audit_log_view(row: AuditLog) -> AuditLogView:
    """The response view of an ``audit_logs`` row."""
    return AuditLogView(
        id=row.id,
        actor_type=row.actor_type,
        actor_id=row.actor_id,
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        ip=None if row.ip is None else str(row.ip),
        user_agent=row.user_agent,
        changes=dict(row.changes),
        created_at=row.created_at,
    )
