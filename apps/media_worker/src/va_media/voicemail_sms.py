"""The voicemail SMS of ``voicemail.action = leave_message_and_sms`` (specs/07-media-pipeline.md §14 step 6;
specs/21 §1, §2; specs/07 §14 Decision P05-T10e).

:class:`VoicemailSms` runs as the takeover's ``after_message`` hook, after the voicemail message was played
and before the hangup. It renders ``voicemail.sms.message`` with the call's variables, sends it from
``voicemail.sms.from`` (else the call's ``from`` number) to the callee through Telnyx Messaging, and records
it as an outbound message of the number pair's conversation (a send-only one when none is active). The message
id is derived from the call id (:func:`voicemail_message_id`), so the SMS goes out at most once per call: a
second attempt finds the row and sends nothing. One Telnyx attempt is made (``send_message`` is not
idempotent). ``call.sms_sent`` is published after a successful send. Every failure is logged and swallowed:
the call ends with ``voicemail_left`` either way. Numbers, the text and keys are never logged.
"""

from __future__ import annotations

import uuid
from typing import Any, Final, Protocol, get_args

from sqlalchemy.exc import SQLAlchemyError

from va_common.crypto import CryptoError
from va_common.logging import get_logger
from va_common.time import Clock
from va_db.models import MessageStatus
from va_db.session import Database
from va_db.voicemail_sms import SmsSender, queue_outbound_message, record_failed, record_sent, sms_sender
from va_domain.call_context import CallContext
from va_engine.opener import render_call_text
from va_events import EventPublishError, EventType, new_event
from va_media.voicemail import VoicemailEvents
from va_telnyx import (
    Message,
    MessageCreate,
    TelnyxApiError,
    TelnyxError,
    UnknownTelnyxAccount,
    WebhookScheme,
    webhook_urls,
)

__all__ = [
    "SMS_SENT_MESSAGE",
    "VOICEMAIL_SMS_NAMESPACE",
    "SmsClient",
    "SmsClients",
    "VoicemailSms",
    "voicemail_message_id",
]

VOICEMAIL_SMS_NAMESPACE: Final = uuid.UUID("5d0f0b6e-3c51-4f8e-9a57-0c7e4b1a9d21")
"""The uuid5 namespace of voicemail SMS message ids."""

SMS_SENT_MESSAGE: Final = "Voicemail SMS sent"
"""``message`` of the ``call.sms_sent`` event."""

_FAILED: Final = "media.voicemail_sms_failed"
_SKIPPED: Final = "media.voicemail_sms_skipped"

_log = get_logger(__name__)


def voicemail_message_id(call_id: uuid.UUID) -> uuid.UUID:
    """The ``messages.id`` of a call's voicemail SMS (the same for every attempt of the call)."""
    return uuid.uuid5(VOICEMAIL_SMS_NAMESPACE, str(call_id))


class SmsClient(Protocol):
    """Sends one message (:class:`va_telnyx.TelnyxClient` satisfies it)."""

    async def send_message(self, message: MessageCreate) -> Message: ...


class SmsClients(Protocol):
    """The Telnyx client of an account (:class:`va_telnyx.TelnyxClientFactory` satisfies it)."""

    async def for_account(self, telnyx_account_id: uuid.UUID | None) -> SmsClient: ...


class _Skip(Exception):  # noqa: N818 - a control-flow signal, not an error
    """Stop the send; ``reason`` is logged."""

    def __init__(self, event: str, reason: str) -> None:
        super().__init__(reason)
        self.event = event
        self.reason = reason


class VoicemailSms:
    """Sends the voicemail SMS of a call (:meth:`send` never raises)."""

    def __init__(  # noqa: PLR0913 - per-process collaborators, all keyword-only
        self,
        *,
        database: Database,
        telnyx: SmsClients | None,
        events: VoicemailEvents | None,
        platform_profile_id: str,
        edge_public_host: str,
        clock: Clock,
        webhook_scheme: WebhookScheme = "https",
    ) -> None:
        self._database = database
        self._telnyx = telnyx
        self._events = events
        self._platform_profile_id = platform_profile_id
        self._edge_public_host = edge_public_host
        self._webhook_scheme: WebhookScheme = webhook_scheme
        self._clock = clock

    async def send(self, context: CallContext) -> None:
        """Render, record and send the SMS of ``context`` (failures are logged, never raised)."""
        try:
            await self._send(context)
        except _Skip as skip:
            if skip.event == _FAILED:
                _log.warning(skip.event, reason=skip.reason)
            else:
                _log.info(skip.event, reason=skip.reason)

    async def _send(self, context: CallContext) -> None:
        sms = context.config.voicemail.sms
        if sms is None:
            raise _Skip(_SKIPPED, "no_sms")
        text = render_call_text(
            context, sms.message, now=self._clock.now(), field_name="voicemail.sms.message"
        )
        if text is None:
            raise _Skip(_SKIPPED, "blank_message")
        sender_number = sms.from_ or context.from_number
        message_id = voicemail_message_id(context.call_id)
        sender, conversation_id = await self._queue(context, sender_number, text, message_id)
        profile = sender.messaging_profile_id
        if profile is None and sender.telnyx_account_id is None:
            profile = self._platform_profile_id or None
        if profile is None:
            await self._failed(context, message_id, {"code": "no_messaging_profile"})
            raise _Skip(_FAILED, "no_messaging_profile")
        client = await self._client(context, sender, message_id)
        request = MessageCreate(
            from_=sender_number,
            to=context.to_number,
            text=text,
            messaging_profile_id=profile,
            webhook_url=webhook_urls(
                self._edge_public_host, sender.telnyx_account_id, scheme=self._webhook_scheme
            ).messaging,
            use_profile_webhooks=False,
        )
        try:
            sent = await client.send_message(request)
        except TelnyxError as exc:
            await self._failed(context, message_id, _telnyx_error(exc))
            raise _Skip(_FAILED, "telnyx_error") from exc
        segments = sent.parts or 1
        await self._sent(context, message_id, sent, segments)
        await self._publish(context, message_id, conversation_id, segments)
        _log.info("media.voicemail_sms_sent", segments=segments)

    async def _queue(
        self, context: CallContext, sender_number: str, text: str, message_id: uuid.UUID
    ) -> tuple[SmsSender, uuid.UUID]:
        try:
            async with self._database.tenant_session(context.org_id) as session:
                sender = await sms_sender(session, context.org_id, sender_number)
                if sender is None:
                    raise _Skip(_FAILED, "sms_not_enabled")
                queued = await queue_outbound_message(
                    session,
                    org_id=context.org_id,
                    message_id=message_id,
                    agent_number=sender_number,
                    user_number=context.to_number,
                    contact_id=context.contact_id,
                    body=text,
                    call_id=context.call_id,
                    now=self._clock.now(),
                )
        except (SQLAlchemyError, OSError) as exc:
            raise _Skip(_FAILED, "database") from exc
        if not queued.created:
            raise _Skip("media.voicemail_sms_duplicate", "already_queued")
        return sender, queued.conversation_id

    async def _client(self, context: CallContext, sender: SmsSender, message_id: uuid.UUID) -> SmsClient:
        if self._telnyx is not None:
            try:
                return await self._telnyx.for_account(sender.telnyx_account_id)
            except (UnknownTelnyxAccount, CryptoError, SQLAlchemyError, OSError) as exc:
                _log.warning("media.telnyx_unavailable", error_type=type(exc).__name__)
        await self._failed(context, message_id, {"code": "telnyx_unavailable"})
        raise _Skip(_FAILED, "telnyx_unavailable")

    async def _failed(self, context: CallContext, message_id: uuid.UUID, error: dict[str, Any]) -> None:
        try:
            async with self._database.tenant_session(context.org_id) as session:
                await record_failed(session, context.org_id, message_id, error=error)
        except (SQLAlchemyError, OSError) as exc:
            _log.warning("media.voicemail_sms_record_failed", target="failed", error_type=type(exc).__name__)

    async def _sent(self, context: CallContext, message_id: uuid.UUID, sent: Message, segments: int) -> None:
        stored = _stored_status(sent.to[0].status if sent.to else None)
        try:
            async with self._database.tenant_session(context.org_id) as session:
                await record_sent(
                    session,
                    context.org_id,
                    message_id,
                    telnyx_message_id=sent.id,
                    status=stored,
                    segments=segments,
                )
        except (SQLAlchemyError, OSError) as exc:
            _log.warning("media.voicemail_sms_record_failed", target="sent", error_type=type(exc).__name__)

    async def _publish(
        self, context: CallContext, message_id: uuid.UUID, conversation_id: uuid.UUID, segments: int
    ) -> None:
        if self._events is None:
            return
        event = new_event(
            EventType.CALL_SMS_SENT,
            org_id=context.org_id,
            clock=self._clock,
            call_id=context.call_id,
            message=SMS_SENT_MESSAGE,
            data={
                "message_id": str(message_id),
                "conversation_id": str(conversation_id),
                "segments": segments,
                "source": "voicemail",
            },
        )
        try:
            await self._events.publish(event)
        except (EventPublishError, OSError) as exc:
            _log.warning("media.voicemail_sms_record_failed", target="event", error_type=type(exc).__name__)


def _stored_status(status: str | None) -> MessageStatus:
    """Telnyx's recipient status when it is a ``messages.status`` value, else ``queued``."""
    for candidate in get_args(MessageStatus):
        if candidate == status:
            return candidate  # type: ignore[no-any-return]
    return "queued"


def _telnyx_error(exc: TelnyxError) -> dict[str, Any]:
    """The ``messages.error`` of a failed send: the first Telnyx error code (else the failure type)."""
    if isinstance(exc, TelnyxApiError) and exc.errors:
        return {"code": exc.errors[0].code, "detail": exc.errors[0].title}
    return {"code": type(exc).__name__}
