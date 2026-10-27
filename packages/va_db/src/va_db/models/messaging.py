"""Messaging and widget conversations: conversations, messages, 10DLC registrations
(specs/03-data-model.md §8).

Follows the conventions of :mod:`va_db.models.tenancy`: explicit SQL types, ``Literal`` aliases enforced by
:func:`check_in`, and spec-named indexes. Per the §1 decision, every tenant table has an ``org_id`` index.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, get_args

from sqlalchemy import Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from va_db.base import Base, IdMixin, TimestampMixin
from va_db.models._checks import check_in
from va_db.models._columns import (
    fk_column,
    int_column,
    jsonb_column,
    jsonb_default,
    micros_column,
    org_id_column,
    text_array_column,
    text_column,
    tz,
)

__all__ = [
    "Conversation",
    "ConversationChannel",
    "ConversationRunnerType",
    "ConversationStatus",
    "Message",
    "MessageDirection",
    "MessageRole",
    "MessageStatus",
    "TenDlcBrand",
    "TenDlcBrandStatus",
    "TenDlcCampaign",
    "TenDlcCampaignStatus",
]

ConversationChannel = Literal["sms", "mms", "widget", "imessage", "rcs"]
# Not ``calls.RunnerType``: text conversations run an ``objective`` where calls run a ``task``.
ConversationRunnerType = Literal["objective", "pathway", "persona", "agent"]
ConversationStatus = Literal["active", "human_takeover", "closed", "timed_out"]
MessageDirection = Literal["inbound", "outbound"]
MessageRole = Literal["user", "assistant", "human_agent", "system"]
MessageStatus = Literal[
    "queued",
    "sending",
    "sent",
    "delivered",
    "sending_failed",
    "delivery_failed",
    "delivery_unconfirmed",
    "received",
]
TenDlcBrandStatus = Literal["draft", "submitted", "verified", "unverified", "failed"]
TenDlcCampaignStatus = Literal["draft", "submitted", "active", "rejected", "suspended"]


class Conversation(IdMixin, TimestampMixin, Base):
    """``conversations``: one SMS/MMS/widget/iMessage/RCS thread between an agent and a user.

    ``engine_state`` is the serialized ``EngineState`` (specs/11-pathways-engine §9). A conversation times out
    after ``time_out_minutes`` without messages.
    """

    __tablename__ = "conversations"
    __table_args__ = (
        check_in("channel", get_args(ConversationChannel)),
        check_in("runner_type", get_args(ConversationRunnerType)),
        check_in("status", get_args(ConversationStatus)),
        Index("conversations_lookup_idx", "org_id", "channel", "agent_number", "user_number", "status"),
        Index("conversations_widget_idx", "widget_id", "widget_visitor_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    channel: Mapped[ConversationChannel] = mapped_column(Text, nullable=False)
    agent_number: Mapped[str | None] = mapped_column(Text)
    user_number: Mapped[str | None] = mapped_column(Text)
    widget_id: Mapped[uuid.UUID | None] = fk_column("widgets.id", "SET NULL")
    widget_visitor_id: Mapped[str | None] = mapped_column(Text)
    contact_id: Mapped[uuid.UUID | None] = fk_column("contacts.id", "SET NULL")
    runner_type: Mapped[ConversationRunnerType] = mapped_column(Text, nullable=False)
    objective: Mapped[str | None] = mapped_column(Text)
    pathway_id: Mapped[uuid.UUID | None] = fk_column("pathways.id", "SET NULL")
    pathway_version: Mapped[int | None] = mapped_column(Integer)
    persona_id: Mapped[uuid.UUID | None] = fk_column("personas.id", "SET NULL")
    agent_id: Mapped[uuid.UUID | None] = fk_column("agents.id", "SET NULL")
    current_node_id: Mapped[str | None] = mapped_column(Text)
    engine_state: Mapped[dict[str, Any]] = jsonb_column("{}")
    request_data: Mapped[dict[str, Any]] = jsonb_column("{}")
    # ``metadata`` is reserved on Declarative classes (it is the MetaData), so the attribute takes a trailing
    # underscore while the column keeps the spec name.
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=jsonb_default("{}")
    )
    status: Mapped[ConversationStatus] = text_column("active")
    time_out_minutes: Mapped[int] = int_column(1440)
    last_message_at: Mapped[datetime | None] = mapped_column(tz())
    webhook: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    disposition_tag: Mapped[str | None] = mapped_column(Text)


class Message(IdMixin, TimestampMixin, Base):
    """``messages``: one message of a conversation; ``status`` follows Telnyx Messaging delivery states."""

    __tablename__ = "messages"
    __table_args__ = (
        check_in("direction", get_args(MessageDirection)),
        check_in("role", get_args(MessageRole)),
        check_in("status", get_args(MessageStatus)),
        Index("messages_conversation_idx", "conversation_id", "created_at"),
        Index("messages_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    conversation_id: Mapped[uuid.UUID] = fk_column("conversations.id", "CASCADE", nullable=False)
    direction: Mapped[MessageDirection] = mapped_column(Text, nullable=False)
    role: Mapped[MessageRole] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    media_urls: Mapped[list[str]] = text_array_column("{}")
    telnyx_message_id: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[MessageStatus] = text_column("queued")
    error: Mapped[Any | None] = mapped_column(JSONB, nullable=True)
    segments: Mapped[int] = int_column(1)
    price_micros: Mapped[int] = micros_column(0)
    node_id: Mapped[str | None] = mapped_column(Text)


class TenDlcBrand(IdMixin, TimestampMixin, Base):
    """``ten_dlc_brands``: a 10DLC brand registration; ``payload`` holds the brand fields
    (specs/21-messaging §6).
    """

    __tablename__ = "ten_dlc_brands"
    __table_args__ = (
        check_in("status", get_args(TenDlcBrandStatus)),
        Index("ten_dlc_brands_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    telnyx_brand_id: Mapped[str | None] = mapped_column(Text, unique=True)
    payload: Mapped[dict[str, Any]] = jsonb_column()
    status: Mapped[TenDlcBrandStatus] = mapped_column(Text, nullable=False)
    status_detail: Mapped[str | None] = mapped_column(Text)


class TenDlcCampaign(IdMixin, TimestampMixin, Base):
    """``ten_dlc_campaigns``: a 10DLC campaign registered under a brand (specs/21-messaging §6)."""

    __tablename__ = "ten_dlc_campaigns"
    __table_args__ = (
        check_in("status", get_args(TenDlcCampaignStatus)),
        Index("ten_dlc_campaigns_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    brand_id: Mapped[uuid.UUID] = fk_column("ten_dlc_brands.id", "CASCADE", nullable=False)
    telnyx_campaign_id: Mapped[str | None] = mapped_column(Text, unique=True)
    use_case: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = jsonb_column()
    status: Mapped[TenDlcCampaignStatus] = mapped_column(Text, nullable=False)
    status_detail: Mapped[str | None] = mapped_column(Text)
