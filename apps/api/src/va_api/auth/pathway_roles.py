"""Role checks of the pathways API beyond scopes (specs/12-pathways-api.md §10).

Rules: specs/12 §3 Decision P07-T09a.

Analysts hold ``pathways:write`` (specs/04 §3.2 "Pathway tests / simulations") but may not edit graphs: the
editing routes add :func:`require_pathway_editor`, which refuses session principals whose specs/04 §3.2
"Pathways / folders" cell is not ``W``. API keys pass (their scopes decide). Force delete needs an owner or
admin session, or an API key with ``*`` (:func:`can_force_delete`).
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import Depends

from va_api.auth.context import RequestContext
from va_api.auth.dependencies import get_principal
from va_api.auth.rbac_matrix import cell
from va_api.auth.scopes import ALL_SCOPES
from va_common.errors import AppError

__all__ = [
    "EDITOR_ROW",
    "FORCE_DELETE_MESSAGE",
    "FORCE_DELETE_ROLES",
    "NOT_EDITOR_MESSAGE",
    "can_force_delete",
    "is_pathway_editor",
    "require_pathway_editor",
]

EDITOR_ROW: Final = "Pathways / folders"
NOT_EDITOR_MESSAGE: Final = "Your role cannot edit pathways."
FORCE_DELETE_MESSAGE: Final = "Force delete requires an owner or admin."
FORCE_DELETE_ROLES: Final = frozenset({"owner", "admin"})

_SESSION_PRINCIPALS: Final = frozenset({"user", "platform_admin"})


def is_pathway_editor(ctx: RequestContext) -> bool:
    """Whether the principal may change pathway graphs and folders."""
    if ctx.principal_type not in _SESSION_PRINCIPALS:
        return True
    return ctx.role is not None and cell(EDITOR_ROW, ctx.role) == "W"


async def require_pathway_editor(ctx: Annotated[RequestContext, Depends(get_principal)]) -> RequestContext:
    """``FORBIDDEN`` for session roles that may only read pathways (analyst, viewer, billing)."""
    if not is_pathway_editor(ctx):
        raise AppError("FORBIDDEN", NOT_EDITOR_MESSAGE)
    return ctx


def can_force_delete(ctx: RequestContext) -> bool:
    """Owner/admin sessions and ``*`` API keys may delete a pathway that is still referenced."""
    if ctx.principal_type in _SESSION_PRINCIPALS:
        return ctx.role in FORCE_DELETE_ROLES
    return ctx.principal_type == "api_key" and ALL_SCOPES in ctx.scopes
