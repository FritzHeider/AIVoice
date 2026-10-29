"""Dashboard session and CSRF cookies (specs/04-auth-tenancy-rbac.md §4.3, §4.4; specs/28-security.md §5).

``va_session`` is HttpOnly; ``va_csrf`` is readable by the dashboard script so it can echo it in
``X-CSRF-Token``. Both are ``SameSite=Lax``, ``Path=/``, ``Secure`` everywhere but ``VA_ENV=local``, scoped to
``SESSION_COOKIE_DOMAIN`` when set, and live 90 days in the browser; the server enforces the real expiry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from starlette.responses import Response

from va_api.settings import ApiSettings

__all__ = [
    "COOKIE_MAX_AGE_S",
    "CSRF_COOKIE",
    "CSRF_HEADER",
    "SESSION_COOKIE",
    "CookiePolicy",
    "clear_auth_cookies",
    "set_csrf_cookie",
    "set_session_cookie",
]

SESSION_COOKIE: Final = "va_session"
CSRF_COOKIE: Final = "va_csrf"
CSRF_HEADER: Final = "x-csrf-token"
COOKIE_MAX_AGE_S: Final = 90 * 86400
_PATH: Final = "/"
_SAMESITE: Final = "lax"


@dataclass(frozen=True, slots=True)
class CookiePolicy:
    """The attributes shared by both auth cookies."""

    secure: bool
    domain: str | None

    @classmethod
    def from_settings(cls, settings: ApiSettings) -> CookiePolicy:
        domain = settings.session_cookie_domain.strip()
        return cls(secure=not settings.is_local, domain=domain or None)


def _set(response: Response, policy: CookiePolicy, name: str, value: str, *, httponly: bool) -> None:
    response.set_cookie(
        name,
        value,
        max_age=COOKIE_MAX_AGE_S,
        path=_PATH,
        domain=policy.domain,
        secure=policy.secure,
        httponly=httponly,
        samesite=_SAMESITE,
    )


def set_session_cookie(response: Response, policy: CookiePolicy, token: str) -> None:
    _set(response, policy, SESSION_COOKIE, token, httponly=True)


def set_csrf_cookie(response: Response, policy: CookiePolicy, token: str) -> None:
    _set(response, policy, CSRF_COOKIE, token, httponly=False)


def clear_auth_cookies(response: Response, policy: CookiePolicy) -> None:
    """Expire both cookies with the attributes they were set with (``Max-Age=0``)."""
    for name, httponly in ((SESSION_COOKIE, True), (CSRF_COOKIE, False)):
        response.delete_cookie(
            name,
            path=_PATH,
            domain=policy.domain,
            secure=policy.secure,
            httponly=httponly,
            samesite=_SAMESITE,
        )
