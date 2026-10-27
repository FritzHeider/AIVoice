"""Memory and messaging models against Postgres, on the rolled-back ``scratch_session`` schema."""

from __future__ import annotations

import secrets
import uuid
from typing import Any

import pytest
from model_factories import ModelBuilder, assert_enum_check_enforced, assert_rejected, flushed, make_org
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.base import Base
from va_db.models import (
    EMBEDDING_DIMENSIONS,
    Agent,
    Contact,
    Conversation,
    ConversationChannel,
    ConversationRunnerType,
    ConversationStatus,
    Memory,
    MemoryChange,
    MemoryChangeType,
    Message,
    MessageDirection,
    MessageRole,
    MessageStatus,
    Pathway,
    Persona,
    TenDlcBrand,
    TenDlcBrandStatus,
    TenDlcCampaign,
    TenDlcCampaignStatus,
    Widget,
)

pytestmark = pytest.mark.integration


async def _contact(session: AsyncSession, org_id: uuid.UUID | None = None) -> Contact:
    org_id = org_id or (await make_org(session)).id
    return await flushed(session, Contact(org_id=org_id, phone_e164=f"+1415{secrets.randbelow(10**7):07d}"))


async def _memory(session: AsyncSession, contact: Contact, persona_id: uuid.UUID | None = None) -> Memory:
    return await flushed(session, Memory(org_id=contact.org_id, contact_id=contact.id, persona_id=persona_id))


def _conversation(org_id: uuid.UUID, **values: Any) -> Conversation:
    values.setdefault("channel", "sms")
    values.setdefault("runner_type", "objective")
    return Conversation(org_id=org_id, **values)


async def _make_conversation(session: AsyncSession) -> Conversation:
    return await flushed(session, _conversation((await make_org(session)).id))


def _message(conversation: Conversation, **values: Any) -> Message:
    values.setdefault("direction", "outbound")
    values.setdefault("role", "assistant")
    values.setdefault("body", "Your cleaning is booked for Tuesday at 9:30.")
    return Message(org_id=conversation.org_id, conversation_id=conversation.id, **values)


def _brand(org_id: uuid.UUID, **values: Any) -> TenDlcBrand:
    values.setdefault("status", "draft")
    return TenDlcBrand(org_id=org_id, payload={"displayName": "Acme Dental"}, **values)


async def _build_memory_change(session: AsyncSession) -> Base:
    memory = await _memory(session, await _contact(session))
    return MemoryChange(org_id=memory.org_id, memory_id=memory.id, change_type="summary")


async def _build_conversation(session: AsyncSession) -> Base:
    return _conversation((await make_org(session)).id)


async def _build_message(session: AsyncSession) -> Base:
    return _message(await _make_conversation(session))


async def _build_brand(session: AsyncSession) -> Base:
    return _brand((await make_org(session)).id)


async def _build_campaign(session: AsyncSession) -> Base:
    brand = await flushed(session, _brand((await make_org(session)).id))
    return TenDlcCampaign(
        org_id=brand.org_id, brand_id=brand.id, use_case="CUSTOMER_CARE", payload={}, status="draft"
    )


@pytest.mark.parametrize(
    ("builder", "column", "literal"),
    [
        (_build_memory_change, "change_type", MemoryChangeType),
        (_build_conversation, "channel", ConversationChannel),
        (_build_conversation, "runner_type", ConversationRunnerType),
        (_build_conversation, "status", ConversationStatus),
        (_build_message, "direction", MessageDirection),
        (_build_message, "role", MessageRole),
        (_build_message, "status", MessageStatus),
        (_build_brand, "status", TenDlcBrandStatus),
        (_build_campaign, "status", TenDlcCampaignStatus),
    ],
)
async def test_enum_values_accepted_and_rejected(
    scratch_session: AsyncSession, builder: ModelBuilder, column: str, literal: Any
) -> None:
    await assert_enum_check_enforced(scratch_session, builder, column, literal)


async def test_defaults(scratch_session: AsyncSession) -> None:
    memory = await _memory(scratch_session, await _contact(scratch_session))
    assert (memory.summary, memory.facts, memory.open_items, memory.recent_messages) == ("", {}, [], [])
    assert (memory.enabled, memory.summary_embedding, memory.persona_id) == (True, None, None)

    change = await flushed(scratch_session, await _build_memory_change(scratch_session))
    assert isinstance(change, MemoryChange)
    assert (change.before, change.after, change.call_id, change.conversation_id) == (None, None, None, None)

    conversation = await _make_conversation(scratch_session)
    assert (conversation.engine_state, conversation.request_data, conversation.metadata_) == ({}, {}, {})
    assert (conversation.status, conversation.time_out_minutes) == ("active", 1440)

    message = await flushed(scratch_session, _message(conversation))
    assert (message.media_urls, message.status, message.segments, message.price_micros) == (
        [],
        "queued",
        1,
        0,
    )
    assert (message.error, message.telnyx_message_id) == (None, None)


async def test_memory_scope_unique_per_contact_and_persona(scratch_session: AsyncSession) -> None:
    contact = await _contact(scratch_session)
    personas = [
        await flushed(scratch_session, Persona(org_id=contact.org_id, name=name)) for name in ("Ava", "Max")
    ]
    await _memory(scratch_session, contact)
    for persona in personas:
        await _memory(scratch_session, contact, persona.id)
    duplicates = [
        Memory(org_id=contact.org_id, contact_id=contact.id),
        Memory(org_id=contact.org_id, contact_id=contact.id, persona_id=personas[0].id),
    ]
    for duplicate in duplicates:
        await assert_rejected(scratch_session, duplicate, "memories_contact_persona_uq")
    await _memory(scratch_session, await _contact(scratch_session, contact.org_id))


async def test_summary_embedding_round_trip(scratch_session: AsyncSession) -> None:
    embedding = [index / EMBEDDING_DIMENSIONS for index in range(EMBEDDING_DIMENSIONS)]
    contact = await _contact(scratch_session)
    memory = await flushed(
        scratch_session,
        Memory(org_id=contact.org_id, contact_id=contact.id, summary_embedding=embedding),
    )
    scratch_session.expunge_all()

    stored = (await scratch_session.execute(select(Memory).where(Memory.id == memory.id))).scalar_one()
    assert stored.summary_embedding is not None
    assert [round(value, 6) for value in stored.summary_embedding] == [round(v, 6) for v in embedding]


async def test_unique_external_ids(scratch_session: AsyncSession) -> None:
    conversation = await _make_conversation(scratch_session)
    await flushed(scratch_session, _message(conversation, telnyx_message_id="msg-1"))
    await assert_rejected(
        scratch_session, _message(conversation, telnyx_message_id="msg-1"), "messages_telnyx_message_id_key"
    )
    for _ in range(2):
        await flushed(scratch_session, _message(conversation))

    org_id = conversation.org_id
    await flushed(scratch_session, _brand(org_id, telnyx_brand_id="BRAND1"))
    await assert_rejected(
        scratch_session, _brand(org_id, telnyx_brand_id="BRAND1"), "ten_dlc_brands_telnyx_brand_id_key"
    )


async def _count(session: AsyncSession, model: type[Base], **where: Any) -> int:
    query = select(func.count()).select_from(model).filter_by(**where)
    return int((await session.execute(query)).scalar_one())


async def test_contact_and_persona_deletes_cascade_to_memories(scratch_session: AsyncSession) -> None:
    contact = await _contact(scratch_session)
    persona = await flushed(scratch_session, Persona(org_id=contact.org_id, name="Ava"))
    org_wide = await _memory(scratch_session, contact)
    await _memory(scratch_session, contact, persona.id)
    await flushed(
        scratch_session,
        MemoryChange(
            org_id=contact.org_id, memory_id=org_wide.id, change_type="fact_set", after={"dob": "1990"}
        ),
    )
    scratch_session.expunge_all()

    await scratch_session.execute(delete(Persona).where(Persona.id == persona.id))
    assert await _count(scratch_session, Memory, contact_id=contact.id) == 1
    await scratch_session.execute(delete(Contact).where(Contact.id == contact.id))
    assert await _count(scratch_session, Memory, contact_id=contact.id) == 0
    assert await _count(scratch_session, MemoryChange, memory_id=org_wide.id) == 0


async def test_conversation_delete_cascades_messages(scratch_session: AsyncSession) -> None:
    conversation = await _make_conversation(scratch_session)
    for body in ("Hi", "Hello"):
        await flushed(scratch_session, _message(conversation, body=body))
    scratch_session.expunge_all()

    await scratch_session.execute(delete(Conversation).where(Conversation.id == conversation.id))
    assert await _count(scratch_session, Message, conversation_id=conversation.id) == 0


async def test_conversation_references_set_null(scratch_session: AsyncSession) -> None:
    org_id = (await make_org(scratch_session)).id
    parents: list[Widget | Contact | Pathway | Persona | Agent] = [
        Widget(org_id=org_id, name="Site chat", public_key=f"wgt_{secrets.token_hex(12)}"),
        Contact(org_id=org_id, phone_e164="+14155550142"),
        Pathway(org_id=org_id, name="Booking"),
        Persona(org_id=org_id, name="Ava"),
        Agent(org_id=org_id, name="Receptionist"),
    ]
    for parent in parents:
        await flushed(scratch_session, parent)
    columns = ("widget_id", "contact_id", "pathway_id", "persona_id", "agent_id")
    ids = {column: parent.id for column, parent in zip(columns, parents, strict=True)}
    conversation = await flushed(
        scratch_session, _conversation(org_id, channel="widget", runner_type="agent", **ids)
    )
    scratch_session.expunge_all()

    for column, parent in zip(columns, parents, strict=True):
        model = type(parent)
        await scratch_session.execute(delete(model).where(model.id == ids[column]))
        stored = select(Conversation.__table__.c[column]).where(Conversation.id == conversation.id)
        assert (await scratch_session.execute(stored)).scalar_one() is None


async def test_brand_delete_cascades_campaigns(scratch_session: AsyncSession) -> None:
    campaign = await flushed(scratch_session, await _build_campaign(scratch_session))
    assert isinstance(campaign, TenDlcCampaign)
    scratch_session.expunge_all()

    await scratch_session.execute(delete(TenDlcBrand).where(TenDlcBrand.id == campaign.brand_id))
    assert await _count(scratch_session, TenDlcCampaign, brand_id=campaign.brand_id) == 0
