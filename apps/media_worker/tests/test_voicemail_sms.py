"""The voicemail SMS sender on a real database (specs/07 §14 step 6; specs/07 §14 Decision P05-T10e)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from clock_helpers import ManualClock
from media_helpers import call_context
from service_helpers import json_log_stream, log_events
from sms_fakes import FakeTelnyx
from sms_rows import add_sms_number, add_telnyx_account, random_e164
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from tenant_rows import OrgFactory, open_database

from va_common.ids import new_id
from va_db.models import Conversation, Message, PhoneNumber
from va_db.session import Database
from va_domain.call_context import CallContext
from va_events import DomainEvent, EventPublishError, EventType
from va_media.voicemail_sms import SMS_SENT_MESSAGE, VoicemailSms, voicemail_message_id
from va_telnyx import TelnyxApiError, WebhookScheme
from va_telnyx.errors import TelnyxErrorItem, TelnyxTransportError

pytestmark = pytest.mark.integration

EDGE = "edge.test:8100"
PLATFORM_PROFILE = "mp-platform"
TEMPLATE = "Hi {{name}}, sorry we missed you. Call {{from}} back."


@dataclass
class FakeEvents:
    fail: bool = False
    published: list[DomainEvent] = field(default_factory=list)

    async def publish(self, event: DomainEvent) -> DomainEvent:
        if self.fail:
            raise EventPublishError(event, "seq")
        self.published.append(event)
        return event


@pytest.fixture
async def database(media_db_url: str) -> AsyncIterator[Database]:
    async with open_database(media_db_url) as db:
        yield db


@dataclass
class Call:
    context: CallContext
    sender: str


async def make_call(
    database: Database, *, sms_from: bool = False, account: bool = False, message: str = TEMPLATE
) -> Call:
    org_id = await OrgFactory(database).create()
    account_id = await add_telnyx_account(database, org_id, "mp-byo") if account else None
    from_number = await add_sms_number(database, org_id, telnyx_account_id=account_id)
    sms: dict[str, Any] = {"message": message}
    sender = from_number
    if sms_from:
        sender = await add_sms_number(database, org_id, telnyx_account_id=account_id)
        sms["from"] = sender
    context = call_context(
        new_id(),
        org_id=org_id,
        voicemail={"action": "leave_message_and_sms", "message": "Please call back.", "sms": sms},
        request_data={"name": "Dana"},
    ).model_copy(update={"from_number": from_number, "to_number": random_e164()})
    return Call(context=context, sender=sender)


def sender_of(
    database: Database,
    telnyx: FakeTelnyx | None,
    events: FakeEvents | None = None,
    *,
    profile: str = PLATFORM_PROFILE,
    scheme: WebhookScheme = "https",
) -> VoicemailSms:
    return VoicemailSms(
        database=database,
        telnyx=telnyx,
        events=events,
        platform_profile_id=profile,
        edge_public_host=EDGE,
        clock=ManualClock(),
        webhook_scheme=scheme,
    )


async def messages_of(database: Database, context: CallContext) -> list[Message]:
    async with database.tenant_session(context.org_id) as session:
        return list((await session.scalars(select(Message).where(Message.org_id == context.org_id))).all())


async def conversations_of(database: Database, context: CallContext) -> list[Conversation]:
    async with database.tenant_session(context.org_id) as session:
        rows = await session.scalars(select(Conversation).where(Conversation.org_id == context.org_id))
        return list(rows.all())


def logged(stream: Any, event: str) -> list[dict[str, Any]]:
    return [line for line in log_events(stream) if line["event"] == event]


async def test_local_environments_get_a_plain_http_status_webhook(database: Database) -> None:
    call = await make_call(database)
    telnyx = FakeTelnyx()
    await sender_of(database, telnyx, scheme="http").send(call.context)
    (sent,) = telnyx.sent
    assert sent.webhook_url == f"http://{EDGE}/telnyx/messaging"


async def test_a_platform_number_sends_the_rendered_sms_and_records_it(database: Database) -> None:
    call = await make_call(database)
    telnyx, events = FakeTelnyx(), FakeEvents()
    await sender_of(database, telnyx, events).send(call.context)
    (sent,) = telnyx.sent
    assert sent.text == f"Hi Dana, sorry we missed you. Call {call.context.from_number} back."
    assert (sent.from_, sent.to) == (call.sender, call.context.to_number)
    assert sent.messaging_profile_id == PLATFORM_PROFILE
    assert sent.webhook_url == f"https://{EDGE}/telnyx/messaging"
    assert not sent.use_profile_webhooks
    assert telnyx.accounts == [None]
    (message,) = await messages_of(database, call.context)
    assert message.id == voicemail_message_id(call.context.call_id)
    assert (message.status, message.segments, message.body) == ("queued", 2, sent.text)
    assert message.telnyx_message_id is not None
    (conversation,) = await conversations_of(database, call.context)
    assert (conversation.agent_number, conversation.user_number) == (call.sender, call.context.to_number)
    (event,) = events.published
    assert event.type == EventType.CALL_SMS_SENT
    assert (event.call_id, event.message) == (call.context.call_id, SMS_SENT_MESSAGE)
    assert event.data == {
        "message_id": str(message.id),
        "conversation_id": str(conversation.id),
        "segments": 2,
        "source": "voicemail",
    }


async def test_a_byo_sender_from_sms_from_uses_its_profile_and_account_webhook(database: Database) -> None:
    call = await make_call(database, sms_from=True, account=True)
    telnyx = FakeTelnyx(status="sending", parts=None)
    await sender_of(database, telnyx).send(call.context)
    (sent,) = telnyx.sent
    assert sent.from_ == call.sender != call.context.from_number
    assert sent.messaging_profile_id == "mp-byo"
    (account,) = telnyx.accounts
    assert account is not None
    assert sent.webhook_url == f"https://{EDGE}/telnyx/messaging/{account}"
    (message,) = await messages_of(database, call.context)
    assert (message.status, message.segments) == ("sending", 1)


async def test_an_unknown_telnyx_status_is_stored_as_queued(database: Database) -> None:
    call = await make_call(database)
    await sender_of(database, FakeTelnyx(status="weird")).send(call.context)
    (message,) = await messages_of(database, call.context)
    assert message.status == "queued"


async def test_the_sms_is_sent_once_per_call(database: Database) -> None:
    stream = json_log_stream()
    call = await make_call(database)
    telnyx, events = FakeTelnyx(), FakeEvents()
    sender = sender_of(database, telnyx, events)
    await sender.send(call.context)
    await sender.send(call.context)
    assert len(telnyx.sent) == 1
    assert len(events.published) == 1
    assert len(await messages_of(database, call.context)) == 1
    assert [line["reason"] for line in logged(stream, "media.voicemail_sms_duplicate")] == ["already_queued"]


async def test_the_event_carries_no_numbers_or_text_and_logs_neither(database: Database) -> None:
    stream = json_log_stream()
    call = await make_call(database)
    events = FakeEvents()
    await sender_of(database, FakeTelnyx(), events).send(call.context)
    (event,) = events.published
    rendered = str(event.data)
    output = stream.getvalue()
    for secret in (call.sender[2:], call.context.to_number[2:], "Dana"):
        assert secret not in rendered
        assert secret not in output
    assert [line["segments"] for line in logged(stream, "media.voicemail_sms_sent")] == [2]


async def test_a_telnyx_failure_marks_the_message_failed(database: Database) -> None:
    stream = json_log_stream()
    call = await make_call(database)
    error = TelnyxApiError(422, (TelnyxErrorItem("40310", "Invalid source number"),), "send_message")
    events = FakeEvents()
    await sender_of(database, FakeTelnyx(fail=error), events).send(call.context)
    (message,) = await messages_of(database, call.context)
    assert message.status == "sending_failed"
    assert message.error == {"code": "40310", "detail": "Invalid source number"}
    assert message.telnyx_message_id is None
    assert events.published == []
    assert [line["reason"] for line in logged(stream, "media.voicemail_sms_failed")] == ["telnyx_error"]


async def test_a_transport_failure_records_its_type(database: Database) -> None:
    call = await make_call(database)
    failure = TelnyxTransportError("send_message", OSError("connection refused"))
    await sender_of(database, FakeTelnyx(fail=failure)).send(call.context)
    (message,) = await messages_of(database, call.context)
    assert message.error == {"code": "TelnyxTransportError"}


async def test_a_sender_that_cannot_send_sms_sends_nothing(database: Database) -> None:
    stream = json_log_stream()
    call = await make_call(database)
    async with database.tenant_session(call.context.org_id) as session:
        number = await session.scalar(select(PhoneNumber).where(PhoneNumber.e164 == call.sender))
        assert number is not None
        number.capabilities = ["voice"]
    telnyx = FakeTelnyx()
    await sender_of(database, telnyx).send(call.context)
    assert telnyx.sent == []
    assert await messages_of(database, call.context) == []
    assert [line["reason"] for line in logged(stream, "media.voicemail_sms_failed")] == ["sms_not_enabled"]


async def test_a_platform_number_without_a_platform_profile_fails(database: Database) -> None:
    stream = json_log_stream()
    call = await make_call(database)
    telnyx = FakeTelnyx()
    await sender_of(database, telnyx, profile="").send(call.context)
    assert telnyx.sent == []
    (message,) = await messages_of(database, call.context)
    assert (message.status, message.error) == ("sending_failed", {"code": "no_messaging_profile"})
    assert [line["reason"] for line in logged(stream, "media.voicemail_sms_failed")] == [
        "no_messaging_profile"
    ]


@pytest.mark.parametrize("unknown", [False, True], ids=["no-factory", "unknown-account"])
async def test_without_a_telnyx_client_the_message_fails(database: Database, unknown: bool) -> None:
    stream = json_log_stream()
    call = await make_call(database)
    telnyx = FakeTelnyx(unknown=True) if unknown else None
    await sender_of(database, telnyx).send(call.context)
    (message,) = await messages_of(database, call.context)
    assert (message.status, message.error) == ("sending_failed", {"code": "telnyx_unavailable"})
    assert [line["reason"] for line in logged(stream, "media.voicemail_sms_failed")] == ["telnyx_unavailable"]


async def test_a_publish_failure_still_records_the_message_as_sent(database: Database) -> None:
    stream = json_log_stream()
    call = await make_call(database)
    await sender_of(database, FakeTelnyx(), FakeEvents(fail=True)).send(call.context)
    (message,) = await messages_of(database, call.context)
    assert message.telnyx_message_id is not None
    assert [line["target"] for line in logged(stream, "media.voicemail_sms_record_failed")] == ["event"]


async def test_a_blank_render_or_missing_sms_skips(database: Database) -> None:
    stream = json_log_stream()
    call = await make_call(database, message="{{missing}}")
    telnyx = FakeTelnyx()
    await sender_of(database, telnyx).send(call.context)
    no_sms = call.context.model_copy(
        update={
            "config": call.context.config.model_copy(
                update={"voicemail": call.context.config.voicemail.model_copy(update={"sms": None})}
            )
        }
    )
    await sender_of(database, telnyx).send(no_sms)
    assert telnyx.sent == []
    assert await messages_of(database, call.context) == []
    reasons = [line["reason"] for line in logged(stream, "media.voicemail_sms_skipped")]
    assert reasons == ["blank_message", "no_sms"]


class _BrokenSession:
    async def __aenter__(self) -> None:
        raise OperationalError("SELECT", None, ConnectionError("down"))

    async def __aexit__(self, *_: object) -> None:
        return None


class _BrokenDatabase:
    def tenant_session(self, _org_id: uuid.UUID) -> _BrokenSession:
        return _BrokenSession()


async def test_a_database_failure_sends_nothing() -> None:
    stream = json_log_stream()
    telnyx = FakeTelnyx()
    sender = VoicemailSms(
        database=_BrokenDatabase(),  # type: ignore[arg-type]
        telnyx=telnyx,
        events=None,
        platform_profile_id=PLATFORM_PROFILE,
        edge_public_host=EDGE,
        clock=ManualClock(),
    )
    context = call_context(
        new_id(),
        voicemail={"action": "leave_message_and_sms", "message": "x", "sms": {"message": "Call us."}},
    )
    await sender.send(context)
    assert telnyx.sent == []
    assert [line["reason"] for line in logged(stream, "media.voicemail_sms_failed")] == ["database"]
