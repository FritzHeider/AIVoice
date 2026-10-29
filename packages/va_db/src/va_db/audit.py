"""Audit log writer (specs/04-auth-tenancy-rbac.md §13; specs/28-security.md §6.3, §10).

:func:`record` adds one ``audit_logs`` row to the caller's open transaction, so the entry commits or rolls
back with the change it describes. ``changes`` is ``{"before": ..., "after": ...}`` after :func:`redact`;
acting on a sub-account adds ``acting_parent_org_id`` and a platform admin adds ``platform_admin: true``.
``va_app`` may only INSERT and SELECT ``audit_logs`` (migration ``0004``), so rows are append-only.

The spec's ``record(ctx, ...)`` takes an :class:`AuditActor` instead of the API request context: ``va_db``
must not import ``va_api``, which builds the actor (``va_api.audit.actor_of``).
"""

from __future__ import annotations

import enum
import ipaddress
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from va_common.auth import API_KEY_RE
from va_db.models import AuditActorType, AuditLog, IpAddress
from va_db.session import use_org

__all__ = [
    "AUDIT_ACTIONS",
    "REDACTED",
    "REDACTED_KEYS",
    "AuditActor",
    "Changes",
    "ResourceId",
    "parse_ip",
    "record",
    "redact",
]

REDACTED: Final = "[REDACTED]"

type ResourceId = str | uuid.UUID | None
type Changes = Mapping[str, Any] | None

# specs/04 §13 names plus `secret_hash` (a stored key hash) and `key` (the plaintext field of a created key).
REDACTED_KEYS: Final = frozenset(
    {
        "password",
        "secret",
        "token",
        "api_key",
        "client_secret",
        "authorization",
        "secret_headers",
        "secret_hash",
        "key",
    }
)
_ENCRYPTED_SUFFIX: Final = "_enc"

# specs/04 §13 "Actions logged (complete list)", in its order.
AUDIT_ACTIONS: Final = frozenset(
    {
        "auth.login",
        "auth.login_failed",
        "auth.logout",
        "auth.mfa_enabled",
        "auth.mfa_disabled",
        "auth.password_changed",
        "auth.password_reset",
        "auth.sso_login",
        "org.update",
        "org.delete",
        "org.ownership_transfer",
        "org.sso_update",
        "org.secret_set",
        "org.secret_delete",
        "subaccount.create",
        "subaccount.suspend",
        "subaccount.reactivate",
        "member.invite",
        "member.role_change",
        "member.remove",
        "invitation.revoke",
        "invitation.accept",
        "api_key.create",
        "api_key.update",
        "api_key.revoke",
        "api_key.roll",
        "pathway.create",
        "pathway.update",
        "pathway.delete",
        "pathway.publish",
        "pathway.promote",
        "persona.publish",
        "agent.version_create",
        "agent.env_pin",
        "experiment.start",
        "experiment.stop",
        "tool.create",
        "tool.update",
        "tool.delete",
        "kb.create",
        "kb.delete",
        "voice.clone",
        "voice.delete",
        "number.purchase",
        "number.release",
        "number.inbound_update",
        "telnyx_account.create",
        "telnyx_account.delete",
        "tendlc.brand_submit",
        "tendlc.campaign_submit",
        "widget.create",
        "widget.update",
        "widget.delete",
        "webhook_endpoint.create",
        "webhook_endpoint.update",
        "webhook_endpoint.delete",
        "webhook.secret_rotate",
        "dnc.add",
        "dnc.remove",
        "dnc.import",
        "memory.reset",
        "memory.erase",
        "contact.erase",
        "billing.topup",
        "billing.auto_recharge_update",
        "billing.plan_change",
        "billing.credit_transfer",
        "call.stop",
        "call.takeover",
        "call.recording_download",
        "batch.create",
        "batch.stop",
        "data.export",
    }
)


@dataclass(frozen=True, slots=True)
class AuditActor:
    """Who acted, in which org, from where; ``org_id`` is the org whose log receives the row."""

    org_id: uuid.UUID
    actor_type: AuditActorType
    actor_id: uuid.UUID | None
    ip: str | None = None
    user_agent: str | None = None
    acting_parent_org_id: uuid.UUID | None = None
    platform_admin: bool = False


def _redacted_key(name: str) -> bool:
    lowered = name.lower()
    return lowered in REDACTED_KEYS or lowered.endswith(_ENCRYPTED_SUFFIX)


def _scalar(value: object) -> object:
    """A JSON-safe form of a non-container value: strings holding an API key are redacted."""
    if isinstance(value, str):
        return REDACTED if API_KEY_RE.search(value) else value
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, enum.Enum):
        return _scalar(value.value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, uuid.UUID | Decimal | ipaddress.IPv4Address | ipaddress.IPv6Address):
        return str(value)
    msg = f"audit changes cannot hold {type(value).__name__}"
    raise TypeError(msg)


def redact(value: object) -> object:
    """A JSON-safe copy of ``value`` with secrets replaced by ``"[REDACTED]"``; ``value`` is not changed.

    Mapping entries whose key is a secret name or ends in ``_enc`` lose their whole value, strings holding an
    API key are replaced, and ids, datetimes, decimals, IP addresses and enums become strings.
    """
    if isinstance(value, Mapping):
        return {
            str(name): REDACTED if _redacted_key(str(name)) else redact(item) for name, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(item) for item in value]
    return _scalar(value)


def parse_ip(value: str | None) -> IpAddress | None:
    """``value`` as an IP address, or ``None`` when absent or not an address (e.g. a test client name)."""
    if value is None:
        return None
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _changes(actor: AuditActor, before: Changes, after: Changes) -> dict[str, Any]:
    changes: dict[str, Any] = {"before": redact(before), "after": redact(after)}
    if actor.acting_parent_org_id is not None:
        changes["acting_parent_org_id"] = str(actor.acting_parent_org_id)
    if actor.platform_admin:
        changes["platform_admin"] = True
    return changes


async def record(  # noqa: PLR0913 - the argument list of specs/04 §13 record()
    session: AsyncSession,
    actor: AuditActor,
    action: str,
    resource_type: str,
    resource_id: ResourceId,
    *,
    before: Changes = None,
    after: Changes = None,
) -> AuditLog:
    """Add an audit row for ``actor.org_id`` to ``session``'s transaction and flush it.

    Works on tenant and global ``va_app`` sessions alike: the insert runs with ``app.org_id`` set to the
    actor's org, which may differ from the session's (a parent's row written in a sub-account's transaction).
    An action outside :data:`AUDIT_ACTIONS` is a programming error (``ValueError``).
    """
    if action not in AUDIT_ACTIONS:
        msg = f"unknown audit action: {action!r}"
        raise ValueError(msg)
    row = AuditLog(
        org_id=actor.org_id,
        actor_type=actor.actor_type,
        actor_id=actor.actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=None if resource_id is None else str(resource_id),
        ip=parse_ip(actor.ip),
        user_agent=actor.user_agent,
        changes=_changes(actor, before, after),
    )
    async with use_org(session, actor.org_id):
        session.add(row)
        await session.flush()
    return row
