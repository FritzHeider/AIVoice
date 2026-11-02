"""The rows behind the voicemail SMS of an outbound call (specs/07-media-pipeline.md §14 step 6; specs/21
§1, §2 step 5; specs/07 §14 Decision P05-T10e).

Every function runs in the caller's tenant session and filters by ``org_id``:

- :func:`sms_sender` finds the org's active, SMS-capable number the SMS goes out from (the Send Call check
  and the media worker share it); BYO numbers carry their Telnyx account's messaging profile;
- :func:`queue_outbound_message` reuses the active conversation of the number pair (specs/21 §1) or opens a
  send-only one, and inserts the ``queued`` message under the caller's deterministic id; an id that already
  exists reports ``created=False`` (the duplicate guard: the SMS is sent at most once);
- :func:`record_sent` / :func:`record_failed` store the outcome of the Telnyx send.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.models import Conversation, Message, MessageStatus, PhoneNumber, TelnyxAccount

__all__ = [
    "OPEN_CONVERSATION_STATUSES",
    "QueuedMessage",
    "SmsSender",
    "queue_outbound_message",
    "record_failed",
    "record_sent",
    "sms_sender",
]

OPEN_CONVERSATION_STATUSES: Final = ("active", "human_takeover")
"""Conversation statuses the active-conversation lookup accepts (specs/21 §1)."""

_SMS_CAPABILITY: Final = "sms"


@dataclass(frozen=True, slots=True)
class SmsSender:
    """An SMS-capable number of the org: its Telnyx account (``None`` = platform) and, for a BYO account,
    that account's messaging profile (``None`` for platform numbers or a BYO account without one)."""

    telnyx_account_id: uuid.UUID | None
    messaging_profile_id: str | None


@dataclass(frozen=True, slots=True)
class QueuedMessage:
    conversation_id: uuid.UUID
    created: bool


async def sms_sender(session: AsyncSession, org_id: uuid.UUID, e164: str) -> SmsSender | None:
    """The org's active, not deleted number ``e164`` when it has the ``sms`` capability, else ``None``."""
    query = (
        select(PhoneNumber.telnyx_account_id, TelnyxAccount.messaging_profile_id)
        .outerjoin(
            TelnyxAccount,
            (TelnyxAccount.id == PhoneNumber.telnyx_account_id) & (TelnyxAccount.org_id == org_id),
        )
        .where(
            PhoneNumber.org_id == org_id,
            PhoneNumber.e164 == e164,
            PhoneNumber.status == "active",
            PhoneNumber.deleted_at.is_(None),
            PhoneNumber.capabilities.contains([_SMS_CAPABILITY]),
        )
        .limit(1)
    )
    row = (await session.execute(query)).first()
    if row is None:
        return None
    account_id, profile_id = row
    return SmsSender(telnyx_account_id=account_id, messaging_profile_id=profile_id or None)


async def _active_conversation(
    session: AsyncSession, org_id: uuid.UUID, agent_number: str, user_number: str, now: datetime
) -> uuid.UUID | None:
    timeout = func.make_interval(0, 0, 0, 0, 0, Conversation.time_out_minutes)
    query = (
        select(Conversation.id)
        .where(
            Conversation.org_id == org_id,
            Conversation.channel == "sms",
            Conversation.agent_number == agent_number,
            Conversation.user_number == user_number,
            Conversation.status.in_(OPEN_CONVERSATION_STATUSES),
            Conversation.last_message_at + timeout > now,
        )
        .order_by(Conversation.last_message_at.desc())
        .limit(1)
    )
    return (await session.execute(query)).scalar_one_or_none()


async def queue_outbound_message(  # noqa: PLR0913 - the message's facts, keyword-only
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    message_id: uuid.UUID,
    agent_number: str,
    user_number: str,
    contact_id: uuid.UUID | None,
    body: str,
    call_id: uuid.UUID,
    now: datetime,
) -> QueuedMessage:
    """Insert the ``queued`` outbound message ``message_id`` into the pair's active conversation (or a new
    send-only one); ``created=False`` when ``message_id`` already exists (nothing changes then)."""
    existing = (
        await session.execute(
            select(Message.conversation_id).where(Message.org_id == org_id, Message.id == message_id)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return QueuedMessage(conversation_id=existing, created=False)
    conversation_id = await _active_conversation(session, org_id, agent_number, user_number, now)
    if conversation_id is None:
        conversation = Conversation(
            org_id=org_id,
            channel="sms",
            agent_number=agent_number,
            user_number=user_number,
            contact_id=contact_id,
            runner_type="objective",
            objective=None,
            metadata_={"source": "voicemail", "call_id": str(call_id)},
            last_message_at=now,
        )
        session.add(conversation)
        await session.flush()
        conversation_id = conversation.id
    inserted = (
        await session.execute(
            insert(Message)
            .values(
                id=message_id,
                org_id=org_id,
                conversation_id=conversation_id,
                direction="outbound",
                role="assistant",
                status="queued",
                body=body,
                segments=1,
            )
            .on_conflict_do_nothing(index_elements=[Message.id])
            .returning(Message.id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        return QueuedMessage(conversation_id=conversation_id, created=False)
    await session.execute(
        update(Conversation)
        .where(Conversation.org_id == org_id, Conversation.id == conversation_id)
        .values(last_message_at=now)
    )
    return QueuedMessage(conversation_id=conversation_id, created=True)


async def record_sent(  # noqa: PLR0913 - the outcome's facts, keyword-only
    session: AsyncSession,
    org_id: uuid.UUID,
    message_id: uuid.UUID,
    *,
    telnyx_message_id: str,
    status: MessageStatus,
    segments: int,
) -> bool:
    """Store the Telnyx id, status and segment count of a sent message; ``False`` when the row is gone."""
    result = await session.execute(
        update(Message)
        .where(Message.org_id == org_id, Message.id == message_id)
        .values(telnyx_message_id=telnyx_message_id, status=status, segments=segments)
        .returning(Message.id)
    )
    return result.scalar_one_or_none() is not None


async def record_failed(
    session: AsyncSession, org_id: uuid.UUID, message_id: uuid.UUID, *, error: dict[str, Any]
) -> bool:
    """Mark the message ``sending_failed`` with ``error``; ``False`` when the row is gone."""
    result = await session.execute(
        update(Message)
        .where(Message.org_id == org_id, Message.id == message_id)
        .values(status="sending_failed", error=error)
        .returning(Message.id)
    )
    return result.scalar_one_or_none() is not None
