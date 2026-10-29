"""FastAPI dependencies: principal, scopes, request rates, idempotency (specs/04 §2.2, §2.4, §4.3, §11, §12).

Routers declare ``dependencies=[Depends(require_scope("<resource>:<read|write>"))]``; the dependency it
returns carries ``required_scope`` so the route-coverage test can find it on ``route.dependant``. Every
authenticated request is charged to the ``default`` rate bucket; routes that send calls add
``Depends(rate_limit("calls_send"))``, whose dependency carries ``rate_limit_bucket``. Once the scope check
passes, a POST carrying ``Idempotency-Key`` is claimed (``va_api.idempotency``), so requests failing
authentication, the org ``ip_allowlist``, the ``default`` bucket, or the scope check never create an
idempotency row.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Final, Literal

import structlog
from fastapi import BackgroundTasks, Depends, Request

from va_api.auth.api_key_auth import ApiKeyAuthenticator
from va_api.auth.context import RequestContext
from va_api.auth.ip_allowlist import enforce_ip_allowlist
from va_api.auth.scopes import SCOPES
from va_api.auth.session_auth import authenticate_session, client_ip, has_session_cookie
from va_api.auth.sub_accounts import act_on_sub_account
from va_api.idempotency import claim_idempotency
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


async def _api_key_principal(request: Request) -> tuple[RequestContext, int | None, uuid.UUID]:
    """The key's context, its ``rate_per_sec`` override, and its id (the caller schedules the touch)."""
    ctx, record = await authenticator_of(request).authenticate(
        request.headers.get(_AUTHORIZATION),
        method=request.method,
        request_id=request_id_of(request),
        ip=client_ip(request),
        user_agent=request.headers.get(_USER_AGENT),
    )
    return ctx, record.rate_per_sec, record.id


async def get_principal(request: Request, background: BackgroundTasks) -> RequestContext:
    """Authenticate the request; stores the context on ``request.state.ctx``.

    A non-blank ``Authorization`` header is always an API key (cookies ignored, no CSRF check); without one a
    ``va_session`` cookie authenticates a dashboard user (``va_api.auth.session_auth``); with neither the
    request fails as an invalid API key. The org's ``ip_allowlist`` (and the parent's while a session acts in
    a sub-account through it) is checked next (``va_api.auth.ip_allowlist``); only then is an API key's
    ``last_used_at`` touch scheduled as a background task after the response. ``X-VA-Org-Id`` then switches
    the context to a sub-account (``va_api.auth.sub_accounts``, specs/04 §8.3), whose allowlist is checked as
    well; ``org_id`` (and ``acting_parent_org_id`` while acting) is bound to every log line of the request.
    One token is then taken from the principal's ``default`` rate bucket (``RATE_LIMITED`` when
    empty), sized by the plan of the org acted in, before any scope check.
    """
    key_id: uuid.UUID | None = None
    if (request.headers.get(_AUTHORIZATION) or "").strip() or not has_session_cookie(request):
        ctx, rate_per_sec, key_id = await _api_key_principal(request)
    else:
        ctx, rate_per_sec = await authenticate_session(request)
    await enforce_ip_allowlist(request, ctx)
    if key_id is not None:
        background.add_task(authenticator_of(request).touch_if_due, key_id)
    acting, rate_per_sec = await act_on_sub_account(request, ctx, rate_per_sec)
    if acting.org_id != ctx.org_id:
        await enforce_ip_allowlist(request, acting)
    ctx = acting
    request.state.ctx = ctx
    structlog.contextvars.bind_contextvars(org_id=str(ctx.org_id))
    if ctx.acting_parent_org_id is not None:
        structlog.contextvars.bind_contextvars(acting_parent_org_id=str(ctx.acting_parent_org_id))
    limits = plan_rate_limits(ctx.plan_id, rate_per_sec)
    await enforce(request, limiter_of(request), ctx, "default", limits)
    return ctx


def require_scope(scope: str) -> Callable[..., Awaitable[RequestContext]]:
    """A dependency granting access only to principals holding ``scope`` (or ``*``); else ``FORBIDDEN``.

    ``scope`` must be one of specs/04 §2.3, checked when the router is defined. After the check it claims the
    request's ``Idempotency-Key`` (replay or ``IDEMPOTENCY_*`` errors are raised from here).
    """
    if scope not in SCOPES:
        msg = f"unknown scope: {scope!r}"
        raise ValueError(msg)

    async def check_scope(
        request: Request, ctx: Annotated[RequestContext, Depends(get_principal)]
    ) -> RequestContext:
        if not ctx.has_scope(scope):
            raise AppError("FORBIDDEN", f"Missing required scope '{scope}'.")
        await claim_idempotency(request, ctx)
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
