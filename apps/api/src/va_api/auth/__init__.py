"""Request authentication and authorization (specs/04-auth-tenancy-rbac.md §1, §2)."""

from __future__ import annotations

from va_api.auth.api_key_auth import ApiKeyAuthenticator, invalidate_api_key_cache
from va_api.auth.context import RequestContext
from va_api.auth.dependencies import get_principal, require_scope
from va_api.auth.scopes import SCOPES

__all__ = [
    "SCOPES",
    "ApiKeyAuthenticator",
    "RequestContext",
    "get_principal",
    "invalidate_api_key_cache",
    "require_scope",
]
