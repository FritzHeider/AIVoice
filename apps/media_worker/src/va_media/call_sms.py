"""SMS sent during a voice call (specs/21 §1, §2; specs/07 §14 Decision P05-T10e; specs/11 §10 Decision
P07-T12b).

:class:`OutboundSms` is the send core shared by the voicemail SMS (:mod:`va_media.voicemail_sms`) and the
SMS node of pathway calls (:class:`CallSmsPort`): it records the message as an outbound message of the number
pair's conversation (a send-only one when none is active), sends it through Telnyx Messaging from an
SMS-capable org number (the platform messaging profile for platform numbers), and records the outcome on the
``messages`` row. One Telnyx attempt is made (``send_message`` is not idempotent); a message id that already
has a row sends nothing. Every failure raises :class:`SmsNotSent` (``event`` is the log event, ``reason`` the
short machine reason) after the row was marked failed where one exists. Numbers, texts and keys are never
logged.

:class:`CallSmsPort` is the :class:`va_engine.ports.SmsPort` of a pathway call: the message goes from the
node's ``from`` number (else the call's agent number) to the node's ``to`` number under a fresh message id; it
never raises (a failure is an :class:`~va_engine.ports.SmsOutcome` with ``sent=False`` and the reason) and
publishes nothing itself (the engine reports ``call.sms_sent``).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Protocol, get_args

from sqlalchemy.exc import SQLAlchemyError

from va_common.crypto import CryptoError
from va_common.ids import new_id
from va_common.logging import get_logger
from va_common.time import Clock
from va_db.models import MessageStatus
from va_db.session import Database
from va_db.voicemail_sms import SmsSender, queue_outbound_message, record_failed, record_sent, sms_sender
from va_domain.call_context import CallContext
from va_engine.ports import SmsOutcome, SmsRequest
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
    "CallSmsPort",
    "OutboundSms",
    "SentSms",
    "SmsClient",
    "SmsClients",
    "SmsNotSent",
    "agent_number",
    "counterpart_number",
]

_log = get_logger(__name__)


class SmsClient(Protocol):
    """Sends one message (:class:`va_telnyx.TelnyxClient` satisfies it)."""

    async def send_message(self, message: MessageCreate) -> Message: ...


class SmsClients(Protocol):
    """The Telnyx client of an account (:class:`va_telnyx.TelnyxClientFactory` satisfies it)."""

    async def for_account(self, telnyx_account_id: uuid.UUID | None) -> SmsClient: ...


class SmsNotSent(Exception):  # noqa: N818 - a control-flow signal carrying the log event, not an error type
    """The message was not sent; ``event`` is the log event, ``reason`` the machine reason."""

    def __init__(self, event: str, reason: str) -> None:
        super().__init__(reason)
        self.event = event
        self.reason = reason


@dataclass(frozen=True, slots=True)
class SentSms:
    """A message Telnyx accepted."""

    message_id: uuid.UUID
    conversation_id: uuid.UUID
    segments: int


def agent_number(context: CallContext) -> str:
    """The call's own number: ``from`` of an outbound call, ``to`` of an inbound one."""
    return context.to_number if context.direction == "inbound" else context.from_number


def counterpart_number(context: CallContext) -> str:
    """The other party's number: ``to`` of an outbound call, ``from`` of an inbound one."""
    return context.from_number if context.direction == "inbound" else context.to_number


class OutboundSms:
    """Records and sends messages of a call; log events are ``<log_prefix>_failed`` / ``_duplicate`` /
    ``_record_failed``."""

    def __init__(  # noqa: PLR0913 - per-process collaborators, all keyword-only
        self,
        *,
        database: Database,
        telnyx: SmsClients | None,
        platform_profile_id: str,
        edge_public_host: str,
        clock: Clock,
        log_prefix: str,
        webhook_scheme: WebhookScheme = "https",
    ) -> None:
        self._database = database
        self._telnyx = telnyx
        self._platform_profile_id = platform_profile_id
        self._edge_public_host = edge_public_host
        self._clock = clock
        self._prefix = log_prefix
        self._webhook_scheme: WebhookScheme = webhook_scheme

    @property
    def failed_event(self) -> str:
        return f"{self._prefix}_failed"

    async def deliver(
        self, context: CallContext, *, sender_number: str, to: str, text: str, message_id: uuid.UUID
    ) -> SentSms:
        """Record and send ``text`` from ``sender_number`` to ``to``; :class:`SmsNotSent` on any failure."""
        sender, conversation_id = await self._queue(context, sender_number, to, text, message_id)
        profile = sender.messaging_profile_id
        if profile is None and sender.telnyx_account_id is None:
            profile = self._platform_profile_id or None
        if profile is None:
            await self._failed(context, message_id, {"code": "no_messaging_profile"})
            raise SmsNotSent(self.failed_event, "no_messaging_profile")
        client = await self._client(context, sender, message_id)
        request = MessageCreate(
            from_=sender_number,
            to=to,
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
            raise SmsNotSent(self.failed_event, "telnyx_error") from exc
        segments = sent.parts or 1
        await self._sent(context, message_id, sent, segments)
        return SentSms(message_id, conversation_id, segments)

    async def _queue(
        self, context: CallContext, sender_number: str, to: str, text: str, message_id: uuid.UUID
    ) -> tuple[SmsSender, uuid.UUID]:
        try:
            async with self._database.tenant_session(context.org_id) as session:
                sender = await sms_sender(session, context.org_id, sender_number)
                if sender is None:
                    raise SmsNotSent(self.failed_event, "sms_not_enabled")
                queued = await queue_outbound_message(
                    session,
                    org_id=context.org_id,
                    message_id=message_id,
                    agent_number=sender_number,
                    user_number=to,
                    contact_id=context.contact_id if to == counterpart_number(context) else None,
                    body=text,
                    call_id=context.call_id,
                    now=self._clock.now(),
                )
        except (SQLAlchemyError, OSError) as exc:
            raise SmsNotSent(self.failed_event, "database") from exc
        if not queued.created:
            raise SmsNotSent(f"{self._prefix}_duplicate", "already_queued")
        return sender, queued.conversation_id

    async def _client(self, context: CallContext, sender: SmsSender, message_id: uuid.UUID) -> SmsClient:
        if self._telnyx is not None:
            try:
                return await self._telnyx.for_account(sender.telnyx_account_id)
            except (UnknownTelnyxAccount, CryptoError, SQLAlchemyError, OSError) as exc:
                _log.warning("media.telnyx_unavailable", error_type=type(exc).__name__)
        await self._failed(context, message_id, {"code": "telnyx_unavailable"})
        raise SmsNotSent(self.failed_event, "telnyx_unavailable")

    async def _failed(self, context: CallContext, message_id: uuid.UUID, error: dict[str, Any]) -> None:
        try:
            async with self._database.tenant_session(context.org_id) as session:
                await record_failed(session, context.org_id, message_id, error=error)
        except (SQLAlchemyError, OSError) as exc:
            _log.warning(f"{self._prefix}_record_failed", target="failed", error_type=type(exc).__name__)

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
            _log.warning(f"{self._prefix}_record_failed", target="sent", error_type=type(exc).__name__)


class CallSmsPort:
    """The SMS port of one pathway call (see the module docstring)."""

    def __init__(self, sender: OutboundSms, context: CallContext) -> None:
        self._sender = sender
        self._context = context

    async def send(self, request: SmsRequest) -> SmsOutcome:
        context = self._context
        message_id = new_id()
        try:
            sent = await self._sender.deliver(
                context,
                sender_number=request.from_ or agent_number(context),
                to=request.to,
                text=request.body,
                message_id=message_id,
            )
        except SmsNotSent as refused:
            _log.warning(refused.event, reason=refused.reason, node_id=request.node_id)
            return SmsOutcome(sent=False, error=refused.reason)
        _log.info("media.pathway_sms_sent", segments=sent.segments, node_id=request.node_id)
        return SmsOutcome(sent=True, message_id=str(sent.message_id))


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
