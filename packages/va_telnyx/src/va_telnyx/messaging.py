"""Telnyx messaging and recording download (specs/06-telephony-telnyx.md §2.1, §11; specs/21-messaging-sms.md
§2 step 6).

``send_message`` is not idempotent (Telnyx has no idempotency key): it is retried only before bytes were sent,
and the ``send_message`` activity records ``telnyx_message_id`` so a retried activity never sends twice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from va_telnyx.message_models import Message, MessageCreate
from va_telnyx.transport import Operation, TelnyxTransport, parse_data

__all__ = ["MAX_DOWNLOAD_BYTES", "MessagingCommands"]

MAX_DOWNLOAD_BYTES: Final = 2 * 1024**3

_SEND: Final = Operation("send_message", "POST", "messaging", idempotent=False)
_DOWNLOAD: Final = Operation("download", "GET", None, idempotent=True)


class MessagingCommands:
    """Outbound messages and presigned-URL downloads."""

    _transport: TelnyxTransport

    async def send_message(self, message: MessageCreate) -> Message:
        envelope = await self._transport.request(_SEND, "/messages", json=message.body())
        return parse_data(_SEND, envelope, Message)

    async def download(self, url: str, destination: Path, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> int:
        """Stream the absolute presigned ``url`` (no auth header) to ``destination``; returns the bytes
        written. Raises :class:`va_telnyx.TelnyxDownloadTooLarge` past ``max_bytes`` (2 GiB by default)
        and removes ``destination`` whenever the download fails."""
        return await self._transport.download(_DOWNLOAD, url, destination, max_bytes=max_bytes)
