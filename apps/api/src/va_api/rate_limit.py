"""Request rate limiting per principal (specs/04-auth-tenancy-rbac.md §11, specs/03-data-model.md §11).

Every authenticated request takes one token from the principal's ``default`` bucket; routes that send calls
also take one from ``calls_send``. Buckets are :class:`va_common.token_bucket.TokenBucket` hashes at
``rl:key:{api_key_id}:<bucket>`` (API keys) or ``rl:user:{user_id}:<bucket>`` (session principals), sized by
the org plan with capacity = 2 x rate. The decision of the last charged bucket is kept in the ASGI scope and
:class:`RateLimitHeadersMiddleware` turns it into ``X-RateLimit-*`` (and, when denied, ``Retry-After``)
headers on whatever response the request produces, error responses included. Redis being unavailable never
fails a request: it is allowed without headers and a warning is logged.

This module must not import ``va_api.auth.dependencies`` (which imports it); the FastAPI dependency
``rate_limit(...)`` lives there.
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Literal

from fastapi import Request
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from va_api.middleware import merge_headers
from va_common.errors import AppError
from va_common.logging import get_logger
from va_common.time import Clock
from va_common.token_bucket import BucketDecision, BucketLimit, TokenBucket

if TYPE_CHECKING:
    # Type-only: importing va_api.auth at runtime would cycle (va_api.auth.dependencies imports this module).
    from va_api.auth.context import RequestContext

__all__ = [
    "BUCKETS",
    "DEFAULT_PLAN",
    "LIMIT_HEADER",
    "PLAN_RATE_LIMITS",
    "RATE_LIMIT_SCOPE_KEY",
    "REMAINING_HEADER",
    "RESET_HEADER",
    "RETRY_AFTER_HEADER",
    "Bucket",
    "PlanRateLimits",
    "RateLimitHeadersMiddleware",
    "RateLimiter",
    "enforce",
    "limiter_of",
    "plan_rate_limits",
    "rate_limit_headers",
    "rate_limit_key",
    "rate_limited_message",
    "record_decision",
]

type Bucket = Literal["default", "calls_send"]

BUCKETS: Final[tuple[Bucket, ...]] = ("default", "calls_send")
DEFAULT_PLAN: Final = "start"
BURST_FACTOR: Final = 2
RATE_LIMIT_SCOPE_KEY: Final = "va.rate_limit"
LIMIT_HEADER: Final = "x-ratelimit-limit"
REMAINING_HEADER: Final = "x-ratelimit-remaining"
RESET_HEADER: Final = "x-ratelimit-reset"
RETRY_AFTER_HEADER: Final = "retry-after"
_HTTP: Final = "http"

_log = get_logger(__name__)


def _bucket(rate_per_s: int) -> BucketLimit:
    return BucketLimit(rate_per_s=float(rate_per_s), burst=BURST_FACTOR * rate_per_s)


@dataclass(frozen=True, slots=True)
class PlanRateLimits:
    """The two request buckets of one plan (or of one org with a ``rate_per_sec`` override)."""

    default: BucketLimit
    calls_send: BucketLimit

    def for_bucket(self, bucket: Bucket) -> BucketLimit:
        """The limit of ``bucket``."""
        return self.default if bucket == "default" else self.calls_send

    @classmethod
    def of(cls, default_rate: int, calls_send_rate: int) -> PlanRateLimits:
        """Both buckets from their rates (req/s), each with capacity 2 x rate."""
        return cls(default=_bucket(default_rate), calls_send=_bucket(calls_send_rate))


# specs/04 §11 table (rate req/s; the `burst` column is the `default` capacity, 2 x rate).
PLAN_RATE_LIMITS: Final[Mapping[str, PlanRateLimits]] = MappingProxyType(
    {
        "start": PlanRateLimits.of(10, 2),
        "build": PlanRateLimits.of(50, 10),
        "scale": PlanRateLimits.of(100, 25),
        "enterprise": PlanRateLimits.of(300, 100),
    }
)


def plan_rate_limits(plan_id: str, rate_per_sec: int | None = None) -> PlanRateLimits:
    """The limits of ``plan_id``; ``rate_per_sec`` (``orgs.limits``) replaces the ``default`` rate.

    An unknown plan gets the ``start`` limits and a ``rate_limit_unknown_plan`` warning.
    """
    limits = PLAN_RATE_LIMITS.get(plan_id)
    if limits is None:
        _log.warning("rate_limit_unknown_plan", plan_id=plan_id)
        limits = PLAN_RATE_LIMITS[DEFAULT_PLAN]
    if rate_per_sec is not None:
        limits = PlanRateLimits(default=_bucket(rate_per_sec), calls_send=limits.calls_send)
    return limits


def rate_limit_key(ctx: RequestContext, bucket: Bucket) -> str:
    """``rl:key:{api_key_id}:<bucket>`` or ``rl:user:{user_id}:<bucket>`` (the id is the hash tag)."""
    if ctx.principal_type == "api_key":
        return f"rl:key:{{{ctx.principal_id}}}:{bucket}"
    if ctx.principal_type in {"user", "platform_admin"}:
        return f"rl:user:{{{ctx.principal_id}}}:{bucket}"
    msg = f"no request rate bucket for principal type {ctx.principal_type!r}"
    raise ValueError(msg)


def rate_limited_message(retry_after_s: int) -> str:
    """The ``RATE_LIMITED`` message (specs/04 §11 Decision)."""
    unit = "second" if retry_after_s == 1 else "seconds"
    return f"Rate limit exceeded; retry after {retry_after_s} {unit}."


def record_decision(scope: MutableMapping[str, Any], decision: BucketDecision) -> None:
    """Keep ``decision`` for the response headers (a later bucket replaces an earlier one)."""
    scope[RATE_LIMIT_SCOPE_KEY] = decision


def rate_limit_headers(decision: BucketDecision) -> tuple[tuple[str, str], ...]:
    """``X-RateLimit-Limit/Remaining/Reset``, plus ``Retry-After`` when the request was denied."""
    headers = (
        (LIMIT_HEADER, str(decision.limit)),
        (REMAINING_HEADER, str(decision.remaining)),
        (RESET_HEADER, str(decision.reset_s)),
    )
    if decision.allowed:
        return headers
    return (*headers, (RETRY_AFTER_HEADER, str(decision.retry_after_s)))


class RateLimiter:
    """Charges request buckets in Redis with the time of an injected clock."""

    def __init__(self, redis: Redis, clock: Clock) -> None:
        self._buckets = TokenBucket(redis)
        self._clock = clock

    async def check(
        self, ctx: RequestContext, bucket: Bucket, limits: PlanRateLimits
    ) -> BucketDecision | None:
        """Take one token from ``bucket`` of ``ctx``'s principal; ``None`` when Redis is unavailable."""
        now_ms = int(self._clock.now().timestamp() * 1000)
        try:
            return await self._buckets.take(
                rate_limit_key(ctx, bucket), limits.for_bucket(bucket), now_ms=now_ms
            )
        except (RedisError, OSError) as exc:
            _log.warning(
                "rate_limit_unavailable",
                bucket=bucket,
                principal_type=ctx.principal_type,
                error=type(exc).__name__,
            )
            return None


def limiter_of(request: Request) -> RateLimiter:
    """The rate limiter of the container the lifespan stored on ``app.state``."""
    limiter: RateLimiter = request.app.state.container.rate_limiter
    return limiter


async def enforce(
    request: Request, limiter: RateLimiter, ctx: RequestContext, bucket: Bucket, limits: PlanRateLimits
) -> None:
    """Charge ``bucket``, record the decision for the headers, and raise ``RATE_LIMITED`` when denied."""
    decision = await limiter.check(ctx, bucket, limits)
    if decision is None:
        return
    record_decision(request.scope, decision)
    if not decision.allowed:
        raise AppError("RATE_LIMITED", rate_limited_message(decision.retry_after_s))


class RateLimitHeadersMiddleware:
    """Adds the recorded bucket decision as headers to the response of the request (errors included)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != _HTTP:
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            decision = scope.get(RATE_LIMIT_SCOPE_KEY)
            if message["type"] == "http.response.start" and isinstance(decision, BucketDecision):
                message["headers"] = merge_headers(message.get("headers", ()), rate_limit_headers(decision))
            await send(message)

        await self.app(scope, receive, send_with_headers)
