"""Email domains and the URLs of the OIDC login (specs/04-auth-tenancy-rbac.md §5).

The domain rule is shared with ``allowed_domains`` of ``/v1/org/sso``: a lowercase DNS name with at least two
labels and an alphabetic TLD, no wildcards. The login's browser-facing URLs are built here so the login
router and the password login's ``SSO_REQUIRED`` details agree.
"""

from __future__ import annotations

import re
from typing import Final
from urllib.parse import urlencode

from va_api.settings import ApiSettings

__all__ = [
    "CALLBACK_PATH",
    "MAX_EMAIL_LENGTH",
    "MIN_EMAIL_LENGTH",
    "START_PATH",
    "callback_url",
    "dashboard_home_url",
    "email_domain",
    "is_dns_domain",
    "login_error_url",
    "sso_start_url",
]

START_PATH: Final = "/internal/auth/sso/start"
CALLBACK_PATH: Final = "/internal/auth/sso/callback"
MIN_EMAIL_LENGTH: Final = 3
MAX_EMAIL_LENGTH: Final = 320
_DOMAIN: Final = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def is_dns_domain(value: str) -> bool:
    """Whether ``value`` (already lowercased) is a DNS name of at least two labels with an alphabetic TLD."""
    return _DOMAIN.fullmatch(value) is not None


def email_domain(email: str) -> str | None:
    """The lowercased domain of ``email`` (3 to 320 characters after stripping, exactly one ``@``, a non-empty
    local part), or ``None`` when the address or its domain is not acceptable."""
    value = email.strip()
    if not MIN_EMAIL_LENGTH <= len(value) <= MAX_EMAIL_LENGTH or value.count("@") != 1:
        return None
    local, domain = value.split("@")
    domain = domain.lower()
    if not local or not is_dns_domain(domain):
        return None
    return domain


def _base(url: str) -> str:
    return url.rstrip("/")


def sso_start_url(settings: ApiSettings, email: str) -> str:
    """Where the dashboard sends a user whose org enforces SSO."""
    return f"{_base(settings.public_api_base_url)}{START_PATH}?{urlencode({'email': email.strip()})}"


def callback_url(settings: ApiSettings) -> str:
    """The ``redirect_uri`` registered with the identity provider."""
    return f"{_base(settings.public_api_base_url)}{CALLBACK_PATH}"


def login_error_url(settings: ApiSettings, reason: str) -> str:
    """The dashboard login page showing an SSO failure ``reason``."""
    return f"{_base(settings.dashboard_base_url)}/login?{urlencode({'sso_error': reason})}"


def dashboard_home_url(settings: ApiSettings) -> str:
    return f"{_base(settings.dashboard_base_url)}/"
