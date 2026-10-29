"""``SMTP_URL`` parsing (specs/02-repo-layout-and-conventions.md §6).

Schemes: ``smtp://`` (plain; STARTTLS when the server offers it; local/test only), ``smtp+starttls://``
(STARTTLS required), ``smtps://`` (implicit TLS). Percent-decoded userinfo is the login. Default ports
25 / 587 / 465.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final
from urllib.parse import unquote, urlsplit

__all__ = ["PLAIN_SCHEME", "SMTP_DEFAULT_PORTS", "SmtpConfig", "parse_smtp_url"]

PLAIN_SCHEME: Final = "smtp"
SMTP_DEFAULT_PORTS: Final = {"smtp": 25, "smtp+starttls": 587, "smtps": 465}


@dataclass(frozen=True, slots=True)
class SmtpConfig:
    """Connection settings for one SMTP relay; ``repr`` never shows the password.

    ``start_tls`` follows aiosmtplib: ``None`` upgrades when the server offers STARTTLS, ``True`` requires it,
    ``False`` never sends it (implicit TLS already encrypts the connection).
    """

    scheme: str
    hostname: str
    port: int
    username: str | None
    password: str | None = field(repr=False)
    use_tls: bool
    start_tls: bool | None


def parse_smtp_url(url: str) -> SmtpConfig:
    """Parse ``SMTP_URL``; ``ValueError`` for an unknown scheme, a missing host, or an invalid port."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in SMTP_DEFAULT_PORTS:
        msg = f"SMTP_URL scheme must be one of {sorted(SMTP_DEFAULT_PORTS)}"
        raise ValueError(msg)
    if not parts.hostname:
        msg = "SMTP_URL must include a host"
        raise ValueError(msg)
    try:
        port = parts.port
    except ValueError:
        msg = "SMTP_URL port must be an integer between 0 and 65535"
        raise ValueError(msg) from None
    return SmtpConfig(
        scheme=scheme,
        hostname=parts.hostname,
        port=SMTP_DEFAULT_PORTS[scheme] if port is None else port,
        username=unquote(parts.username) if parts.username else None,
        password=unquote(parts.password) if parts.password is not None else None,
        use_tls=scheme == "smtps",
        start_tls={"smtp": None, "smtp+starttls": True, "smtps": False}[scheme],
    )
