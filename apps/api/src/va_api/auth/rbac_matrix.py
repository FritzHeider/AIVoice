"""The RBAC matrix of specs/04-auth-tenancy-rbac.md §3.2 in code, and what each role derives from it (§3.3).

:data:`MATRIX` holds every §3.2 row (label exactly as in the spec) with one cell per role; a test parses
the markdown table and compares it cell for cell. :data:`ROW_GRANTS` maps each row to the scope resource it
belongs to and the permissions that guard its finer actions. A role holds ``<resource>:read`` when any row
of that resource is not denied for it and ``<resource>:write`` when any such row is ``W``, ``W(own)`` or
``X`` (only scopes that exist in §2.3); it holds a permission when any row granting it is not denied.
``va_api.auth.rbac`` and ``va_api.auth.permissions`` derive ``ROLE_SCOPES`` and ``ROLE_PERMISSIONS`` from
here, so editing §3.2 means editing this module.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

from va_api.auth.scopes import SCOPES
from va_db.models import MemberRole

__all__ = [
    "MATRIX",
    "PERMISSIONS",
    "ROLES",
    "ROW_GRANTS",
    "SESSION_ONLY_PERMISSIONS",
    "Cell",
    "Matrix",
    "RowGrant",
    "cell",
    "role_permissions",
    "role_scopes",
]

type Cell = Literal["R", "R*", "W", "W(own)", "X", "—"]
type Matrix = Mapping[str, Mapping[MemberRole, Cell]]

# The §3.2 column order.
ROLES: Final[tuple[MemberRole, ...]] = ("owner", "admin", "developer", "analyst", "billing", "viewer")

PERMISSIONS: Final[frozenset[str]] = frozenset(
    {
        "pathways.publish",
        "numbers.purchase",
        "numbers.release",
        "telnyx_accounts.manage",
        "tendlc.manage",
        "members.manage",
        "org.owner_transfer",
        "org.delete",
        "org.compliance",
        "sso.manage",
        "subaccounts.manage",
        "billing.plan_change",
        "billing.payment",
        "billing.credit_transfer",
        "webhooks.secret",
        "memory.erase",
        "audit.read",
        "voices.clone",
        "experiments.operate",
        "calls.operate",
    }
)
SESSION_ONLY_PERMISSIONS: Final[frozenset[str]] = frozenset(
    {"org.delete", "org.owner_transfer", "sso.manage"}
)

_READABLE: Final[frozenset[Cell]] = frozenset({"R", "R*", "W", "W(own)", "X"})
_WRITABLE: Final[frozenset[Cell]] = frozenset({"W", "W(own)", "X"})


@dataclass(frozen=True, slots=True)
class RowGrant:
    """The scope resource a §3.2 row belongs to (``None``: no scope) and the permissions it grants."""

    resource: str | None
    permissions: frozenset[str] = frozenset()


def _row(*cells: Cell) -> Mapping[MemberRole, Cell]:
    if len(cells) != len(ROLES):
        msg = f"a matrix row needs {len(ROLES)} cells, got {len(cells)}"
        raise ValueError(msg)
    return MappingProxyType(dict(zip(ROLES, cells, strict=True)))


def _grant(resource: str | None, *permissions: str) -> RowGrant:
    return RowGrant(resource, frozenset(permissions))


_MANAGE: Final = _row("W", "W", "W", "R", "—", "R")
_OPERATE: Final = _row("X", "X", "X", "—", "—", "—")
_OWNER_ADMIN_WRITE: Final = _row("W", "W", "—", "—", "—", "—")
_ANYONE_READ: Final = _row("R", "R", "R", "R", "R", "R")
_OWNER_ONLY: Final = _row("X", "—", "—", "—", "—", "—")

MATRIX: Final[Matrix] = MappingProxyType(
    {
        "Calls: list/view details & transcripts": _row("R", "R", "R", "R", "—", "R*"),
        "Calls: recordings download": _row("R", "R", "R", "R", "—", "R*"),
        "Calls: send / stop / live control / takeover": _OPERATE,
        "Calls: analyze (post-hoc)": _row("X", "X", "X", "X", "—", "—"),
        "Batches": _MANAGE,
        "Pathways / folders": _MANAGE,
        "Pathways: publish / promote production": _OPERATE,
        "Pathway tests / simulations": _row("X", "X", "X", "X", "—", "—"),
        "Personas": _MANAGE,
        "Agents / versions / env pins": _MANAGE,
        "Experiments start/stop": _OPERATE,
        "Tools (incl. secret headers view = masked always)": _MANAGE,
        "Knowledge bases": _MANAGE,
        "Voices: catalog / ratings": _MANAGE,
        "Voices: clone / delete clone": _row("W", "W", "W", "—", "—", "—"),
        "Phone numbers: view / inbound config": _MANAGE,
        "Phone numbers: purchase / release": _row("X", "X", "—", "—", "—", "—"),
        "BYO Telnyx accounts": _OWNER_ADMIN_WRITE,
        "Number pools": _MANAGE,
        "SMS conversations: view": _row("R", "R", "R", "R", "—", "R*"),
        "SMS: send / takeover": _OPERATE,
        "10DLC brand/campaign registration": _OWNER_ADMIN_WRITE,
        "Widgets": _MANAGE,
        "Memory / contacts": _row("W", "W", "W", "R", "—", "R*"),
        "Memory reset / erasure": _row("X", "X", "—", "—", "—", "—"),
        "Dispositions / citation schemas / eval setups": _row("W", "W", "W", "W", "—", "R"),
        "DNC list": _MANAGE,
        "Guard rail configuration (org defaults)": _MANAGE,
        "Webhook endpoints / deliveries / replay": _MANAGE,
        "Webhook signing secret view last4 / rotate": _OWNER_ADMIN_WRITE,
        "Analytics": _ANYONE_READ,
        "Prompts library": _MANAGE,
        "Billing: balance / usage / invoices": _row("R", "R", "R", "R", "R", "—"),
        "Billing: top-up / auto-recharge / payment method": _row("W", "W", "—", "—", "W", "—"),
        "Billing: plan change": _row("W", "—", "—", "—", "W", "—"),
        "Credit transfer to sub-account": _row("X", "X", "—", "—", "X", "—"),
        "API keys: list (prefix only)": _row("R", "R", "R", "—", "—", "—"),
        "API keys: create / revoke": _row("W", "W", "W(own)", "—", "—", "—"),
        "Members: list": _ANYONE_READ,
        "Members: invite / change role / remove": _OWNER_ADMIN_WRITE,
        "Assign `owner` role / transfer ownership": _OWNER_ONLY,
        "Org settings (general)": _row("W", "W", "—", "—", "—", "R"),
        "Org settings (compliance, retention, hipaa_mode, pii_redaction, mfa_required)": _OWNER_ADMIN_WRITE,
        "SSO connection": _OWNER_ADMIN_WRITE,
        "Sub-accounts: create / list / switch into": _OWNER_ADMIN_WRITE,
        "Audit logs": _row("R", "R", "—", "—", "—", "—"),
        "Delete org": _OWNER_ONLY,
    }
)

ROW_GRANTS: Final[Mapping[str, RowGrant]] = MappingProxyType(
    {
        "Calls: list/view details & transcripts": _grant("calls"),
        "Calls: recordings download": _grant("calls"),
        "Calls: send / stop / live control / takeover": _grant("calls", "calls.operate"),
        "Calls: analyze (post-hoc)": _grant("calls"),
        "Batches": _grant("batches"),
        "Pathways / folders": _grant("pathways"),
        "Pathways: publish / promote production": _grant("pathways", "pathways.publish"),
        "Pathway tests / simulations": _grant("pathways"),
        "Personas": _grant("personas"),
        "Agents / versions / env pins": _grant("agents"),
        "Experiments start/stop": _grant("agents", "experiments.operate"),
        "Tools (incl. secret headers view = masked always)": _grant("tools"),
        "Knowledge bases": _grant("knowledge"),
        "Voices: catalog / ratings": _grant("voices"),
        "Voices: clone / delete clone": _grant("voices", "voices.clone"),
        "Phone numbers: view / inbound config": _grant("numbers"),
        "Phone numbers: purchase / release": _grant("numbers", "numbers.purchase", "numbers.release"),
        "BYO Telnyx accounts": _grant("numbers", "telnyx_accounts.manage"),
        "Number pools": _grant("numbers"),
        "SMS conversations: view": _grant("sms"),
        "SMS: send / takeover": _grant("sms"),
        "10DLC brand/campaign registration": _grant("sms", "tendlc.manage"),
        "Widgets": _grant("widgets"),
        "Memory / contacts": _grant("memory"),
        "Memory reset / erasure": _grant("memory", "memory.erase"),
        "Dispositions / citation schemas / eval setups": _grant("analysis"),
        "DNC list": _grant("compliance"),
        "Guard rail configuration (org defaults)": _grant("compliance"),
        "Webhook endpoints / deliveries / replay": _grant("webhooks"),
        "Webhook signing secret view last4 / rotate": _grant("webhooks", "webhooks.secret"),
        "Analytics": _grant("analytics"),
        "Prompts library": _grant("prompts"),
        "Billing: balance / usage / invoices": _grant("billing"),
        "Billing: top-up / auto-recharge / payment method": _grant("billing", "billing.payment"),
        "Billing: plan change": _grant("billing", "billing.plan_change"),
        "Credit transfer to sub-account": _grant("billing", "billing.credit_transfer"),
        "API keys: list (prefix only)": _grant(None),
        "API keys: create / revoke": _grant("api_keys"),
        "Members: list": _grant("org"),
        "Members: invite / change role / remove": _grant("org", "members.manage"),
        "Assign `owner` role / transfer ownership": _grant("org", "org.owner_transfer"),
        "Org settings (general)": _grant("org"),
        "Org settings (compliance, retention, hipaa_mode, pii_redaction, mfa_required)": _grant(
            "org", "org.compliance"
        ),
        "SSO connection": _grant("org", "sso.manage"),
        "Sub-accounts: create / list / switch into": _grant("org", "subaccounts.manage"),
        "Audit logs": _grant("org", "audit.read"),
        "Delete org": _grant("org", "org.delete"),
    }
)


def _derive_scopes(matrix: Matrix, grants: Mapping[str, RowGrant], role: MemberRole) -> frozenset[str]:
    scopes: set[str] = set()
    for label, cells in matrix.items():
        resource = grants[label].resource
        if resource is None:
            continue
        value = cells[role]
        if value in _READABLE:
            scopes.add(f"{resource}:read")
        if value in _WRITABLE:
            scopes.add(f"{resource}:write")
    return frozenset(scope for scope in scopes if scope in SCOPES)


def _derive_permissions(matrix: Matrix, grants: Mapping[str, RowGrant], role: MemberRole) -> frozenset[str]:
    held: set[str] = set()
    for label, cells in matrix.items():
        if cells[role] in _READABLE:
            held.update(grants[label].permissions)
    return frozenset(held)


def role_scopes(role: MemberRole) -> frozenset[str]:
    """The scopes ``role`` derives from :data:`MATRIX`."""
    return _derive_scopes(MATRIX, ROW_GRANTS, role)


def role_permissions(role: MemberRole) -> frozenset[str]:
    """The permissions ``role`` derives from :data:`MATRIX`."""
    return _derive_permissions(MATRIX, ROW_GRANTS, role)


def cell(row: str, role: MemberRole) -> Cell:
    """The §3.2 cell of ``row`` for ``role`` (``KeyError`` for an unknown row label)."""
    return MATRIX[row][role]
