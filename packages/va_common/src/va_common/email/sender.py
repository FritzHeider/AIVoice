"""The ``EmailSender`` interface with SMTP (aiosmtplib) and in-memory implementations.

The SMTP sender connects once per message with a 10 s connect and command timeout. Failures surface as
:class:`EmailDeliveryError`, whose message names the template and the error type but never the recipient or
the body; logs carry the masked recipient only. The in-memory sender records messages; the api uses it when
``VA_ENV=test``, and tests read what was "sent".
"""

from __future__ import annotations

from typing import Final, Protocol

import aiosmtplib

from va_common.email.config import SmtpConfig
from va_common.email.message import EmailMessage, to_mime
from va_common.logging import get_logger
from va_common.masking import mask_email
from va_common.time import Clock, SystemClock

__all__ = ["SMTP_TIMEOUT_S", "EmailDeliveryError", "EmailSender", "InMemoryEmailSender", "SmtpEmailSender"]

SMTP_TIMEOUT_S: Final = 10.0

_log = get_logger(__name__)


class EmailSender(Protocol):
    """Delivers rendered messages."""

    async def send(self, message: EmailMessage) -> None:
        """Hand ``message`` to the transport; :class:`EmailDeliveryError` when it is not accepted."""


class EmailDeliveryError(Exception):
    """The message was not accepted for delivery (the text holds no recipient address or body)."""


class SmtpEmailSender:
    """Sends through the relay in ``SMTP_URL`` with ``EMAIL_FROM`` as the sender."""

    def __init__(self, config: SmtpConfig, *, sender: str, clock: Clock | None = None) -> None:
        self._config = config
        self._sender = sender
        self._clock: Clock = clock if clock is not None else SystemClock()

    async def send(self, message: EmailMessage) -> None:
        mime = to_mime(message, sender=self._sender, now=self._clock.now())
        config = self._config
        try:
            await aiosmtplib.send(
                mime,
                hostname=config.hostname,
                port=config.port,
                username=config.username,
                password=config.password,
                use_tls=config.use_tls,
                start_tls=config.start_tls,
                timeout=SMTP_TIMEOUT_S,
            )
        except (aiosmtplib.SMTPException, OSError, TimeoutError) as exc:
            error_type = type(exc).__name__
            code = getattr(exc, "code", None)
            _log.warning(
                "email_send_failed",
                template=message.template,
                to=mask_email(message.to),
                error_type=error_type,
                smtp_code=code,
            )
            suffix = f" (SMTP {code})" if isinstance(code, int) else ""
            msg = f"delivery of {message.template!r} failed: {error_type}{suffix}"
            raise EmailDeliveryError(msg) from None
        _log.info("email_sent", template=message.template, to=mask_email(message.to))


class InMemoryEmailSender:
    """Records every message in :attr:`sent` (oldest first) instead of delivering it."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)

    def to(self, address: str) -> list[EmailMessage]:
        """Messages sent to ``address`` (case-insensitive), oldest first."""
        wanted = address.casefold()
        return [message for message in self.sent if message.to.casefold() == wanted]

    def clear(self) -> None:
        self.sent.clear()
