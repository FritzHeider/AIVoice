"""FastAPI dependencies: principal, scopes, and request rates (specs/04 §2.2, §2.4, §11).

Routers declare ``dependencies=[Depends(require_scope("<resource>:<read|write>"))]``; the dependency it
returns carries ``required_scope`` so the route-coverage test can find it on ``route.dependant``. Every
authenticated request is charged to the ``default`` rate bucket; routes that send calls add
``Depends(rate_limit("calls_send"))``, whose dependency carries ``rate_limit_bucket``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Final, Literal

import structlog
from fastapi import BackgroundTasks, Depends, Request

from va_api.auth.api_key_auth import ApiKeyAuthenticator
from va_api.auth.context import RequestContext
from va_api.auth.scopes import SCOPES
from va_api.middleware import request_id_of
from va_api.rate_limit import enforce, limiter_of, plan_rate_limits
from va_common.errors import AppError

__all__ = [
    "RATE_LIMIT_BUCKET_ATTR",
    "REQUIRED_SCOPE_ATTR",
    "authenticator_of",
    "get_principal",
    "rate_limit",
    "require_scope",
]

REQUIRED_SCOPE_ATTR: Final = "required_scope"
RATE_LIMIT_BUCKET_ATTR: Final = "rate_limit_bucket"
_AUTHORIZATION: Final = "authorization"
_USER_AGENT: Final = "user-agent"


def authenticator_of(request: Request) -> ApiKeyAuthenticator:
    """The authenticator of the container the lifespan stored on ``app.state``."""
    authenticator: ApiKeyAuthenticator = request.app.state.container.authenticator
    return authenticator


async def get_principal(request: Request, background: BackgroundTasks) -> RequestContext:
    """Authenticate the ``Authorization`` header; stores the context on ``request.state.ctx``.

    The ``last_used_at`` touch runs as a background task after the response; ``org_id`` is bound to every log
    line of the request. One token is then taken from the principal's ``default`` rate bucket
    (``RATE_LIMITED`` when empty), before any scope check.
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
    limits = plan_rate_limits(ctx.plan_id, record.rate_per_sec)
    await enforce(request, limiter_of(request), ctx, "default", limits)
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


def rate_limit(bucket: Literal["calls_send"]) -> Callable[..., Awaitable[RequestContext]]:
    """A dependency taking one more token from ``bucket`` of the principal (after ``default``).

    It uses the plan limits; ``orgs.limits.rate_per_sec`` overrides only the ``default`` bucket.
    """

    async def check_rate(
        request: Request, ctx: Annotated[RequestContext, Depends(get_principal)]
    ) -> RequestContext:
        await enforce(request, limiter_of(request), ctx, bucket, plan_rate_limits(ctx.plan_id))
        return ctx

    setattr(check_rate, RATE_LIMIT_BUCKET_ATTR, bucket)
    return check_rate
