"""Invitation links and email (specs/04-auth-tenancy-rbac.md §6).

The link is ``{DASHBOARD_BASE_URL}/invite/{token}`` (the dashboard route of specs/25) and lives 7 days. Like
verification, the email goes out after the response and a delivery failure is logged without the address; the
inviter can re-invite, which replaces the invitation with a fresh token.
"""

from __future__ import annotations

from typing import Final

from va_api.auth.verification import dashboard_link, deliver
from va_common.email import EmailSender, invitation

__all__ = [
    "ALREADY_MEMBER_MESSAGE",
    "INVITATION_INVALID_MESSAGE",
    "invite_link",
    "send_invitation",
]

INVITATION_INVALID_MESSAGE: Final = "Invitation link is invalid or has expired."
ALREADY_MEMBER_MESSAGE: Final = "You are already a member of this organization."


def invite_link(dashboard_base_url: str, token: str) -> str:
    """The dashboard page that shows the invitation and submits ``token`` to the accept endpoint."""
    return dashboard_link(dashboard_base_url, "invite", token)


async def send_invitation(  # noqa: PLR0913 - keyword-only email fields
    sender: EmailSender,
    *,
    to: str,
    org_name: str,
    inviter_name: str,
    role: str,
    token: str,
    dashboard_base_url: str,
) -> None:
    """Send the ``invitation`` template; failures are logged as ``invitation_email_failed``."""
    message = invitation(
        org_name=org_name,
        inviter_name=inviter_name,
        role=role,
        link=invite_link(dashboard_base_url, token),
        to=to,
    )
    await deliver(sender, message, failure_event="invitation_email_failed")
