"""The voicemail SMS rows on a migrated database (specs/21 §1; specs/07 §14 Decision P05-T10e)."""

from __future__ import annotations

import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sms_rows import add_sms_number, add_telnyx_account, random_e164
from sqlalchemy import func, select
from tenant_rows import OrgFactory, open_database

from va_common.ids import new_id
from va_db.models import Conversation, Message
from va_db.session import Database
from va_db.voicemail_sms import (
    QueuedMessage,
    SmsSender,
    queue_outbound_message,
    record_failed,
    record_sent,
    sms_sender,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    async with open_database(migrated_url) as db:
        yield db


@pytest.fixture
async def org(database: Database) -> uuid.UUID:
    """A fresh org (numbers and conversations of other tests stay invisible)."""
    return await OrgFactory(database).create()


async def sender_of(database: Database, org_id: uuid.UUID, e164: str) -> SmsSender | None:
    async with database.tenant_session(org_id) as session:
        return await sms_sender(session, org_id, e164)


async def test_a_platform_sms_number_has_no_account_profile(database: Database, org: uuid.UUID) -> None:
    e164 = await add_sms_number(database, org)
    assert await sender_of(database, org, e164) == SmsSender(
        telnyx_account_id=None, messaging_profile_id=None
    )


async def test_a_byo_number_carries_its_account_profile(database: Database, org: uuid.UUID) -> None:
    account = await add_telnyx_account(database, org, "mp-byo")
    e164 = await add_sms_number(database, org, telnyx_account_id=account)
    assert await sender_of(database, org, e164) == SmsSender(
        telnyx_account_id=account, messaging_profile_id="mp-byo"
    )
    bare = await add_telnyx_account(database, org, "")
    other = await add_sms_number(database, org, telnyx_account_id=bare)
    assert await sender_of(database, org, other) == SmsSender(
        telnyx_account_id=bare, messaging_profile_id=None
    )


@pytest.mark.parametrize(
    "values",
    [
        {"capabilities": ["voice"]},
        {"status": "released"},
        {"deleted_at": NOW},
    ],
    ids=["voice-only", "released", "deleted"],
)
async def test_numbers_that_cannot_send_sms_are_not_senders(
    database: Database, org: uuid.UUID, values: dict[str, Any]
) -> None:
    e164 = await add_sms_number(database, org, **values)
    assert await sender_of(database, org, e164) is None


async def test_another_orgs_number_is_not_a_sender(database: Database, org: uuid.UUID) -> None:
    other = await OrgFactory(database).create()
    e164 = await add_sms_number(database, other)
    assert await sender_of(database, org, e164) is None
    assert await sender_of(database, org, random_e164()) is None


async def queue(  # noqa: PLR0913 - the pair plus keyword-only overrides
    database: Database,
    org_id: uuid.UUID,
    agent: str,
    user: str,
    *,
    message_id: uuid.UUID | None = None,
    now: datetime = NOW,
) -> tuple[uuid.UUID, QueuedMessage]:
    message = message_id if message_id is not None else uuid.uuid4()
    async with database.tenant_session(org_id) as session:
        queued = await queue_outbound_message(
            session,
            org_id=org_id,
            message_id=message,
            agent_number=agent,
            user_number=user,
            contact_id=None,
            body="Sorry we missed you.",
            call_id=uuid.UUID(int=7),
            now=now,
        )
    return message, queued


async def conversation_of(database: Database, org_id: uuid.UUID, conversation_id: uuid.UUID) -> Conversation:
    async with database.tenant_session(org_id) as session:
        return await session.get_one(Conversation, conversation_id)


async def message_of(database: Database, org_id: uuid.UUID, message_id: uuid.UUID) -> Message:
    async with database.tenant_session(org_id) as session:
        return await session.get_one(Message, message_id)


async def add_conversation(
    database: Database, org_id: uuid.UUID, agent: str, user: str, **values: Any
) -> uuid.UUID:
    conversation_id = new_id()
    defaults: dict[str, Any] = {"channel": "sms", "runner_type": "objective", "last_message_at": NOW}
    async with database.tenant_session(org_id) as session:
        session.add(
            Conversation(
                id=conversation_id,
                org_id=org_id,
                agent_number=agent,
                user_number=user,
                **{**defaults, **values},
            )
        )
    return conversation_id


async def test_a_new_pair_gets_a_send_only_conversation(database: Database, org: uuid.UUID) -> None:
    agent, user = random_e164(), random_e164()
    message_id, queued = await queue(database, org, agent, user)
    assert queued.created
    conversation = await conversation_of(database, org, queued.conversation_id)
    assert (conversation.channel, conversation.runner_type, conversation.objective) == (
        "sms",
        "objective",
        None,
    )
    assert (conversation.agent_number, conversation.user_number) == (agent, user)
    assert conversation.metadata_ == {"source": "voicemail", "call_id": str(uuid.UUID(int=7))}
    assert conversation.last_message_at == NOW
    message = await message_of(database, org, message_id)
    assert (message.direction, message.role, message.status, message.segments) == (
        "outbound",
        "assistant",
        "queued",
        1,
    )
    assert message.body == "Sorry we missed you."
    assert message.conversation_id == queued.conversation_id


async def test_an_active_conversation_is_reused(database: Database, org: uuid.UUID) -> None:
    agent, user = random_e164(), random_e164()
    older = await add_conversation(database, org, agent, user, last_message_at=NOW - timedelta(hours=2))
    recent = await add_conversation(
        database, org, agent, user, status="human_takeover", last_message_at=NOW - timedelta(minutes=5)
    )
    _, queued = await queue(database, org, agent, user)
    assert queued.conversation_id == recent != older
    assert (await conversation_of(database, org, recent)).last_message_at == NOW


async def test_timed_out_and_closed_conversations_are_not_reused(database: Database, org: uuid.UUID) -> None:
    agent, user = random_e164(), random_e164()
    stale = await add_conversation(
        database, org, agent, user, time_out_minutes=30, last_message_at=NOW - timedelta(minutes=31)
    )
    closed = await add_conversation(database, org, agent, user, status="closed")
    _, queued = await queue(database, org, agent, user)
    assert queued.conversation_id not in {stale, closed}
    assert (await conversation_of(database, org, stale)).last_message_at == NOW - timedelta(minutes=31)


async def test_the_same_message_id_is_queued_once(database: Database, org: uuid.UUID) -> None:
    agent, user = random_e164(), random_e164()
    message_id, first = await queue(database, org, agent, user)
    _, second = await queue(database, org, agent, user, message_id=message_id, now=NOW + timedelta(minutes=1))
    assert first.created
    assert second == QueuedMessage(conversation_id=first.conversation_id, created=False)
    async with database.tenant_session(org) as session:
        count = await session.scalar(
            select(func.count()).select_from(Message).where(Message.id == message_id)
        )
    assert count == 1
    assert (await conversation_of(database, org, first.conversation_id)).last_message_at == NOW


async def test_sent_and_failed_outcomes_are_recorded(database: Database, org: uuid.UUID) -> None:
    agent, user = random_e164(), random_e164()
    sent_id, _ = await queue(database, org, agent, user)
    failed_id, _ = await queue(database, org, agent, user)
    telnyx_id = f"msg-{secrets.token_hex(8)}"
    async with database.tenant_session(org) as session:
        assert await record_sent(
            session, org, sent_id, telnyx_message_id=telnyx_id, status="sending", segments=2
        )
        assert await record_failed(session, org, failed_id, error={"code": "40310", "detail": "no profile"})
        assert not await record_sent(
            session, org, uuid.uuid4(), telnyx_message_id="x", status="sent", segments=1
        )
        assert not await record_failed(session, org, uuid.uuid4(), error={})
    sent = await message_of(database, org, sent_id)
    assert (sent.telnyx_message_id, sent.status, sent.segments) == (telnyx_id, "sending", 2)
    failed = await message_of(database, org, failed_id)
    assert (failed.status, failed.error) == ("sending_failed", {"code": "40310", "detail": "no profile"})
