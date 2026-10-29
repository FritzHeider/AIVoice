"""The complete API key scope list (specs/04-auth-tenancy-rbac.md §2.3).

``SCOPES`` mirrors the §2.3 table, each ``a`` / ``b`` row expanded into both scopes; a test parses the table
and asserts parity.
"""

from __future__ import annotations

from typing import Final

__all__ = ["ALL_SCOPES", "READ_WRITE_RESOURCES", "SCOPES"]

ALL_SCOPES: Final = "*"
# Resources with both a `:read` and a `:write` scope.
READ_WRITE_RESOURCES: Final = (
    "calls",
    "batches",
    "pathways",
    "personas",
    "agents",
    "tools",
    "knowledge",
    "voices",
    "numbers",
    "sms",
    "widgets",
    "memory",
    "analysis",
    "compliance",
    "webhooks",
    "billing",
    "org",
    "prompts",
)
_SINGLE_SCOPES: Final = ("api_keys:write", "analytics:read", "mcp")

SCOPES: Final[frozenset[str]] = frozenset(
    {
        ALL_SCOPES,
        *(f"{resource}:{access}" for resource in READ_WRITE_RESOURCES for access in ("read", "write")),
        *_SINGLE_SCOPES,
    }
)
