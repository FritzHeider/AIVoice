"""CSRF defences for session-authenticated requests (specs/04-auth-tenancy-rbac.md §4.4).

Double submit: a write must echo the ``va_csrf`` cookie in ``X-CSRF-Token`` (compared in constant time). It
must also come from the dashboard: the ``Origin`` header, else the ``Referer``, must have the origin of
``DASHBOARD_BASE_URL``. Both checks fail with ``FORBIDDEN``. API-key requests are exempt (the caller decides).
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

from va_common.auth import tokens_equal
from va_common.errors import AppError

__all__ = ["CSRF_INVALID", "ORIGIN_NOT_ALLOWED", "check_csrf", "check_origin", "origin_of"]

CSRF_INVALID: Final = "CSRF token missing or invalid."
ORIGIN_NOT_ALLOWED: Final = "Request origin is not allowed."
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}


def origin_of(url: str) -> str | None:
    """``scheme://host[:port]`` of an http(s) URL, lower-cased, default port dropped; ``None`` otherwise.

    The ``Origin: null`` of sandboxed or privacy-sensitive contexts has no origin.
    """
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != _DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    return f"{scheme}://{netloc}"


def check_csrf(header: str | None, cookie: str | None) -> None:
    """``FORBIDDEN`` unless the ``X-CSRF-Token`` header equals the ``va_csrf`` cookie (both non-empty)."""
    if not header or not cookie or not tokens_equal(header, cookie):
        raise AppError("FORBIDDEN", CSRF_INVALID)


def check_origin(origin: str | None, referer: str | None, allowed: str, *, required: bool) -> None:
    """``FORBIDDEN`` unless ``Origin`` (else ``Referer``) has the origin of ``allowed``.

    With neither header the request passes only when ``required`` is false (e.g. login from a CLI client).
    """
    source = origin if origin is not None else referer
    if source is None:
        if required:
            raise AppError("FORBIDDEN", ORIGIN_NOT_ALLOWED)
        return
    presented = origin_of(source)
    if presented is None or presented != origin_of(allowed):
        raise AppError("FORBIDDEN", ORIGIN_NOT_ALLOWED)
