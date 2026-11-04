"""Telnyx messaging, recording download, recording lookup, and recording delete (specs/06-telephony-telnyx.md
§2.1, §11; specs/21-messaging-sms.md §2 step 6).

``send_message`` is not idempotent (Telnyx has no idempotency key): it is retried only before bytes were sent,
and the ``send_message`` activity records ``telnyx_message_id`` so a retried activity never sends twice.
``get_recording`` returns fresh ``download_urls`` (the webhook's presigned links expire);
``delete_recording`` is idempotent: a recording Telnyx no longer has (``404``) reports ``False``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from va_telnyx.errors import TelnyxApiError
from va_telnyx.message_models import Message, MessageCreate
from va_telnyx.models import ResponseModel
from va_telnyx.transport import Operation, TelnyxTransport, parse_data, segment

__all__ = ["MAX_DOWNLOAD_BYTES", "MessagingCommands", "Recording", "RecordingDownloadUrls"]

MAX_DOWNLOAD_BYTES: Final = 2 * 1024**3

_SEND: Final = Operation("send_message", "POST", "messaging", idempotent=False)
_DOWNLOAD: Final = Operation("download", "GET", None, idempotent=True)
_GET_RECORDING: Final = Operation("get_recording", "GET", None, idempotent=True)
_DELETE_RECORDING: Final = Operation("delete_recording", "DELETE", None, idempotent=True)
_HTTP_NOT_FOUND: Final = 404


class RecordingDownloadUrls(ResponseModel):
    """``download_urls`` of a recording: presigned links, valid for a few minutes."""

    wav: str | None = None
    mp3: str | None = None


class Recording(ResponseModel):
    """``data`` of ``GET /recordings/{id}`` (the fields the copy flow reads)."""

    id: str
    channels: str | None = None
    download_urls: RecordingDownloadUrls = RecordingDownloadUrls()


class MessagingCommands:
    """Outbound messages, presigned-URL downloads, recording lookups, and recording deletes."""

    _transport: TelnyxTransport

    async def send_message(self, message: MessageCreate) -> Message:
        envelope = await self._transport.request(_SEND, "/messages", json=message.body())
        return parse_data(_SEND, envelope, Message)

    async def download(self, url: str, destination: Path, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> int:
        """Stream the absolute presigned ``url`` (no auth header) to ``destination``; returns the bytes
        written. Raises :class:`va_telnyx.TelnyxDownloadTooLarge` past ``max_bytes`` (2 GiB by default)
        and removes ``destination`` whenever the download fails."""
        return await self._transport.download(_DOWNLOAD, url, destination, max_bytes=max_bytes)

    async def get_recording(self, recording_id: str) -> Recording:
        """``GET /recordings/{id}``: the recording with fresh ``download_urls`` (``404`` raises
        :class:`va_telnyx.TelnyxApiError`)."""
        envelope = await self._transport.request(_GET_RECORDING, f"/recordings/{segment(recording_id)}")
        return parse_data(_GET_RECORDING, envelope, Recording)

    async def delete_recording(self, recording_id: str) -> bool:
        """``DELETE /recordings/{id}``: ``True`` when Telnyx deleted it, ``False`` when it is already gone."""
        try:
            await self._transport.request(_DELETE_RECORDING, f"/recordings/{segment(recording_id)}")
        except TelnyxApiError as exc:
            if exc.status == _HTTP_NOT_FOUND:
                return False
            raise
        return True
