"""Password reset links, the reset email limit, and the recent-auth rule of password change (specs/04 §4.1).

The reset link is ``{DASHBOARD_BASE_URL}/reset-password/{token}`` (the dashboard route of specs/25) and lives
1 hour. At most 3 reset emails go to a user per rolling hour, counted from the ``reset_password`` rows of
``email_tokens`` (no Redis key). Like verification, the email goes out after the response and a delivery
failure is logged without the address. Changing a password needs a recent authentication: a session created,
or verified with MFA, less than 10 minutes ago.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Final

from va_api.auth.sessions import ActiveSession
from va_api.auth.verification import dashboard_link, deliver
from va_common.email import EmailSender, password_reset
from va_common.errors import AppError

__all__ = [
    "CURRENT_PASSWORD_MESSAGE",
    "REAUTH_MESSAGE",
    "REAUTH_WINDOW",
    "RESET_HOURLY_LIMIT",
    "RESET_LIMIT_WINDOW",
    "RESET_TOKEN_INVALID_MESSAGE",
    "require_recent_auth",
    "reset_link",
    "send_password_reset",
]

RESET_HOURLY_LIMIT: Final = 3
RESET_LIMIT_WINDOW: Final = timedelta(hours=1)
REAUTH_WINDOW: Final = timedelta(minutes=10)
RESET_TOKEN_INVALID_MESSAGE: Final = "Reset link is invalid or has expired."  # noqa: S105 - error text
REAUTH_MESSAGE: Final = "Sign in again to change your password."
CURRENT_PASSWORD_MESSAGE: Final = "Current password is incorrect."  # noqa: S105 - error text


def reset_link(dashboard_base_url: str, token: str) -> str:
    """The dashboard page that submits ``token`` with the new password to ``POST /password/reset``."""
    return dashboard_link(dashboard_base_url, "reset-password", token)


async def send_password_reset(
    sender: EmailSender, *, to: str, name: str, token: str, dashboard_base_url: str
) -> None:
    """Send the ``password_reset`` template; failures are logged as ``password_reset_email_failed``."""
    message = password_reset(name=name, link=reset_link(dashboard_base_url, token), to=to)
    await deliver(sender, message, failure_event="password_reset_email_failed")


def require_recent_auth(active: ActiveSession, now: datetime) -> None:
    """``REAUTH_REQUIRED`` 10 minutes after the later of the session's creation and its last MFA check."""
    authenticated_at = active.created_at
    if active.mfa_passed_at is not None:
        authenticated_at = max(authenticated_at, active.mfa_passed_at)
    if now - authenticated_at >= REAUTH_WINDOW:
        raise AppError("REAUTH_REQUIRED", REAUTH_MESSAGE)
