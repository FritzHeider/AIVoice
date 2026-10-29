"""FastAPI dependencies resolving the principal and enforcing scopes (specs/04 §2.2, §2.4).

Routers declare ``dependencies=[Depends(require_scope("<resource>:<read|write>"))]``; the dependency it
returns carries ``required_scope`` so the route-coverage test can find it on ``route.dependant``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Final

import structlog
from fastapi import BackgroundTasks, Depends, Request

from va_api.auth.api_key_auth import ApiKeyAuthenticator
from va_api.auth.context import RequestContext
from va_api.auth.scopes import SCOPES
from va_api.middleware import request_id_of
from va_common.errors import AppError

__all__ = ["REQUIRED_SCOPE_ATTR", "authenticator_of", "get_principal", "require_scope"]

REQUIRED_SCOPE_ATTR: Final = "required_scope"
_AUTHORIZATION: Final = "authorization"
_USER_AGENT: Final = "user-agent"


def authenticator_of(request: Request) -> ApiKeyAuthenticator:
    """The authenticator of the container the lifespan stored on ``app.state``."""
    authenticator: ApiKeyAuthenticator = request.app.state.container.authenticator
    return authenticator


async def get_principal(request: Request, background: BackgroundTasks) -> RequestContext:
    """Authenticate the ``Authorization`` header; stores the context on ``request.state.ctx``.

    The ``last_used_at`` touch runs as a background task after the response; ``org_id`` is bound to every log
    line of the request.
    """
    authenticator = authenticator_of(request)
    ctx, record = await authenticator.authenticate(
        request.headers.get(_AUTHORIZATION),
        method=request.method,
        request_id=request_id_of(request),
        ip=request.client.host if request.client is not None else None,
        user_agent=request.headers.get(_USER_AGENT),
    )
    background.add_task(authenticator.touch_if_due, record.id)
    request.state.ctx = ctx
    structlog.contextvars.bind_contextvars(org_id=str(ctx.org_id))
    return ctx


def require_scope(scope: str) -> Callable[..., Awaitable[RequestContext]]:
    """A dependency granting access only to principals holding ``scope`` (or ``*``); else ``FORBIDDEN``.

    ``scope`` must be one of specs/04 §2.3, checked when the router is defined.
    """
    if scope not in SCOPES:
        msg = f"unknown scope: {scope!r}"
        raise ValueError(msg)

    async def check_scope(ctx: Annotated[RequestContext, Depends(get_principal)]) -> RequestContext:
        if not ctx.has_scope(scope):
            raise AppError("FORBIDDEN", f"Missing required scope '{scope}'.")
        return ctx

    setattr(check_scope, REQUIRED_SCOPE_ATTR, scope)
    return check_scope
