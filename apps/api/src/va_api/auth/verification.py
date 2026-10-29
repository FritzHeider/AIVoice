"""Email verification links and the resend limit (specs/04-auth-tenancy-rbac.md §4.1).

The link is ``{DASHBOARD_BASE_URL}/verify-email/{token}`` (the dashboard route of specs/25). The email goes
out after the response; a delivery failure is logged without the address and never fails the request, because
the user can ask for another link. Resends are limited to 3 per user in a fixed one-hour window counted at
``auth:verify:resend:<user_id>``; rejected attempts count too. Redis being unavailable never blocks a resend.
"""

from __future__ import annotations

import uuid
from typing import Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_api.auth.login_throttle import seconds_phrase
from va_common.email import EmailDeliveryError, EmailMessage, EmailSender, verify_email
from va_common.errors import AppError
from va_common.logging import get_logger

__all__ = [
    "RESEND_LIMIT",
    "RESEND_WINDOW_S",
    "TOKEN_INVALID_MESSAGE",
    "ResendLimiter",
    "dashboard_link",
    "deliver",
    "resend_key",
    "resend_limited_message",
    "send_verification",
    "verify_link",
]

RESEND_LIMIT: Final = 3
RESEND_WINDOW_S: Final = 3600
TOKEN_INVALID_MESSAGE: Final = "Verification link is invalid or has expired."  # noqa: S105 - error text

_log = get_logger(__name__)


def resend_key(user_id: uuid.UUID) -> str:
    return f"auth:verify:resend:{user_id}"


def resend_limited_message(retry_after_s: int) -> str:
    return f"Too many verification emails; try again in {seconds_phrase(retry_after_s)}."


def dashboard_link(dashboard_base_url: str, page: str, token: str) -> str:
    """``{DASHBOARD_BASE_URL}/{page}/{token}`` without a doubled slash."""
    return f"{dashboard_base_url.rstrip('/')}/{page}/{token}"


def verify_link(dashboard_base_url: str, token: str) -> str:
    """The dashboard page that submits ``token`` to ``POST /internal/auth/verify-email``."""
    return dashboard_link(dashboard_base_url, "verify-email", token)


async def deliver(sender: EmailSender, message: EmailMessage, *, failure_event: str) -> None:
    """Send ``message``; a delivery failure is logged as ``failure_event`` (template and error type only)."""
    try:
        await sender.send(message)
    except (EmailDeliveryError, OSError) as exc:
        _log.warning(failure_event, template=message.template, error_type=type(exc).__name__)


async def send_verification(
    sender: EmailSender, *, to: str, name: str, token: str, dashboard_base_url: str
) -> None:
    """Send the ``verify_email`` template; failures are logged as ``verification_email_failed``."""
    message = verify_email(name=name, link=verify_link(dashboard_base_url, token), to=to)
    await deliver(sender, message, failure_event="verification_email_failed")


class ResendLimiter:
    """The per-user counter of ``POST /internal/auth/resend-verification``."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def hit(self, user_id: uuid.UUID) -> None:
        """Count one resend; ``RATE_LIMITED`` (with ``Retry-After``) beyond 3 in the current window."""
        key = resend_key(user_id)
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(key)
                pipe.expire(key, RESEND_WINDOW_S, nx=True)
                pipe.ttl(key)
                count, _, ttl = await pipe.execute()
        except (RedisError, OSError) as exc:
            _log.warning("resend_limit_unavailable", error=type(exc).__name__)
            return
        if int(count) > RESEND_LIMIT:
            retry_after = int(ttl) if int(ttl) > 0 else RESEND_WINDOW_S
            raise AppError(
                "RATE_LIMITED",
                resend_limited_message(retry_after),
                headers={"Retry-After": str(retry_after)},
            )
