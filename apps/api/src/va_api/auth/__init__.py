"""Request authentication and authorization (specs/04-auth-tenancy-rbac.md §1, §2, §3.3)."""

from __future__ import annotations

from va_api.auth.api_key_auth import ApiKeyAuthenticator, invalidate_api_key_cache
from va_api.auth.context import RequestContext
from va_api.auth.dependencies import get_principal, rate_limit, require_scope
from va_api.auth.permissions import PERMISSIONS, require_permission
from va_api.auth.scopes import SCOPES

__all__ = [
    "PERMISSIONS",
    "SCOPES",
    "ApiKeyAuthenticator",
    "RequestContext",
    "get_principal",
    "invalidate_api_key_cache",
    "rate_limit",
    "require_permission",
    "require_scope",
]
