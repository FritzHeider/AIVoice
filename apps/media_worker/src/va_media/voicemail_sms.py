"""The voicemail SMS of ``voicemail.action = leave_message_and_sms`` (specs/07-media-pipeline.md §14 step 6;
specs/21 §1, §2; specs/07 §14 Decision P05-T10e).

:class:`VoicemailSms` runs as the takeover's ``after_message`` hook, after the voicemail message was played
and before the hangup. It renders ``voicemail.sms.message`` with the call's variables, sends it from
``voicemail.sms.from`` (else the call's ``from`` number) to the callee through Telnyx Messaging, and records
it as an outbound message of the number pair's conversation (a send-only one when none is active). The message
id is derived from the call id (:func:`voicemail_message_id`), so the SMS goes out at most once per call: a
second attempt finds the row and sends nothing. Recording and sending is the shared
:class:`~va_media.call_sms.OutboundSms` core (one Telnyx attempt). ``call.sms_sent`` is published after a
successful send. Every failure is logged and swallowed:
the call ends with ``voicemail_left`` either way. Numbers, the text and keys are never logged.
"""

from __future__ import annotations

import uuid
from typing import Final

from va_common.logging import get_logger
from va_common.time import Clock
from va_db.session import Database
from va_domain.call_context import CallContext
from va_engine.opener import render_call_text
from va_events import EventPublishError, EventType, new_event
from va_media.call_sms import OutboundSms, SentSms, SmsClients, SmsNotSent
from va_media.voicemail import VoicemailEvents
from va_telnyx import WebhookScheme

__all__ = ["SMS_SENT_MESSAGE", "VOICEMAIL_SMS_NAMESPACE", "VoicemailSms", "voicemail_message_id"]

VOICEMAIL_SMS_NAMESPACE: Final = uuid.UUID("5d0f0b6e-3c51-4f8e-9a57-0c7e4b1a9d21")
"""The uuid5 namespace of voicemail SMS message ids."""

SMS_SENT_MESSAGE: Final = "Voicemail SMS sent"
"""``message`` of the ``call.sms_sent`` event."""

_PREFIX: Final = "media.voicemail_sms"
_SKIPPED: Final = f"{_PREFIX}_skipped"

_log = get_logger(__name__)


def voicemail_message_id(call_id: uuid.UUID) -> uuid.UUID:
    """The ``messages.id`` of a call's voicemail SMS (the same for every attempt of the call)."""
    return uuid.uuid5(VOICEMAIL_SMS_NAMESPACE, str(call_id))


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
        self._sender = OutboundSms(
            database=database,
            telnyx=telnyx,
            platform_profile_id=platform_profile_id,
            edge_public_host=edge_public_host,
            clock=clock,
            log_prefix=_PREFIX,
            webhook_scheme=webhook_scheme,
        )
        self._events = events
        self._clock = clock

    async def send(self, context: CallContext) -> None:
        """Render, record and send the SMS of ``context`` (failures are logged, never raised)."""
        try:
            sent = await self._send(context)
        except SmsNotSent as skip:
            if skip.event == self._sender.failed_event:
                _log.warning(skip.event, reason=skip.reason)
            else:
                _log.info(skip.event, reason=skip.reason)
            return
        await self._publish(context, sent)
        _log.info("media.voicemail_sms_sent", segments=sent.segments)

    async def _send(self, context: CallContext) -> SentSms:
        sms = context.config.voicemail.sms
        if sms is None:
            raise SmsNotSent(_SKIPPED, "no_sms")
        text = render_call_text(
            context, sms.message, now=self._clock.now(), field_name="voicemail.sms.message"
        )
        if text is None:
            raise SmsNotSent(_SKIPPED, "blank_message")
        return await self._sender.deliver(
            context,
            sender_number=sms.from_ or context.from_number,
            to=context.to_number,
            text=text,
            message_id=voicemail_message_id(context.call_id),
        )

    async def _publish(self, context: CallContext, sent: SentSms) -> None:
        if self._events is None:
            return
        event = new_event(
            EventType.CALL_SMS_SENT,
            org_id=context.org_id,
            clock=self._clock,
            call_id=context.call_id,
            message=SMS_SENT_MESSAGE,
            data={
                "message_id": str(sent.message_id),
                "conversation_id": str(sent.conversation_id),
                "segments": sent.segments,
                "source": "voicemail",
            },
        )
        try:
            await self._events.publish(event)
        except (EventPublishError, OSError) as exc:
            _log.warning("media.voicemail_sms_record_failed", target="event", error_type=type(exc).__name__)
