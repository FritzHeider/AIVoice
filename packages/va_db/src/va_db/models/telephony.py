"""Telephony models: carrier accounts, numbers, orders, pools, raw Telnyx events (specs/03-data-model.md §4).

Follows the conventions of :mod:`va_db.models.tenancy`: explicit SQL types, ``Literal`` aliases enforced by
:func:`check_in`, and spec-named indexes.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any, Literal, get_args

from sqlalchemy import BigInteger, Date, ForeignKey, Index, LargeBinary, Text, UniqueConstraint, Uuid, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from va_db.base import Base, IdMixin, SoftDeleteMixin, TimestampMixin
from va_db.models._checks import check_in
from va_db.models._columns import (
    fk_column,
    int_column,
    jsonb_column,
    org_id_column,
    text_array_column,
    text_column,
    tz,
)

__all__ = [
    "NumberOrder",
    "NumberOrderStatus",
    "NumberPool",
    "NumberPoolMember",
    "NumberPoolStrategy",
    "NumberType",
    "PhoneNumber",
    "PhoneNumberStatus",
    "TelnyxAccount",
    "TelnyxAccountStatus",
    "TelnyxEvent",
]

TelnyxAccountStatus = Literal["pending", "active", "error", "deleted"]
NumberType = Literal["local", "toll_free", "mobile", "national"]
PhoneNumberStatus = Literal["pending", "active", "releasing", "released"]
NumberOrderStatus = Literal["pending", "success", "failure"]
NumberPoolStrategy = Literal["round_robin", "least_recently_used", "random", "local_match"]


class TelnyxAccount(IdMixin, TimestampMixin, Base):
    """``telnyx_accounts``: a customer's own Telnyx account ("Bring Your Own Telnyx")."""

    __tablename__ = "telnyx_accounts"
    __table_args__ = (
        check_in("status", get_args(TelnyxAccountStatus)),
        Index("telnyx_accounts_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    label: Mapped[str] = mapped_column(Text, nullable=False)
    api_key_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    # Ed25519 public key for their webhooks.
    public_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Created in their account by our bootstrap activity.
    call_control_app_id: Mapped[str | None] = mapped_column(Text)
    messaging_profile_id: Mapped[str | None] = mapped_column(Text)
    outbound_voice_profile_id: Mapped[str | None] = mapped_column(Text)
    # Opaque value returned to customers as ``encrypted_key``.
    encrypted_key_handle: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    status: Mapped[TelnyxAccountStatus] = text_column("pending")
    status_detail: Mapped[str | None] = mapped_column(Text)


class PhoneNumber(IdMixin, SoftDeleteMixin, TimestampMixin, Base):
    """``phone_numbers``: ``inbound_config`` is an ``InboundConfig`` (specs/16 §4), ``sms_config`` a
    ``SmsNumberConfig`` (specs/21 §3). An E.164 number is unique among ``pending``/``active`` rows only.
    """

    __tablename__ = "phone_numbers"
    __table_args__ = (
        check_in("number_type", get_args(NumberType)),
        check_in("status", get_args(PhoneNumberStatus)),
        Index(
            "phone_numbers_e164_active_uq",
            "e164",
            unique=True,
            postgresql_where=text("status IN ('pending', 'active')"),
        ),
        Index("phone_numbers_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    e164: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL = platform account.
    telnyx_account_id: Mapped[uuid.UUID | None] = fk_column("telnyx_accounts.id", "SET NULL")
    telnyx_phone_number_id: Mapped[str | None] = mapped_column(Text)
    country_code: Mapped[str] = mapped_column(Text, nullable=False)
    number_type: Mapped[NumberType] = text_column("local")
    label: Mapped[str | None] = mapped_column(Text)
    # voice, sms, mms.
    capabilities: Mapped[list[str]] = text_array_column("{voice}")
    inbound_config: Mapped[dict[str, Any]] = jsonb_column("{}")
    sms_config: Mapped[dict[str, Any]] = jsonb_column("{}")
    monthly_cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    status: Mapped[PhoneNumberStatus] = text_column("active")
    purchased_at: Mapped[datetime | None] = mapped_column(tz())
    released_at: Mapped[datetime | None] = mapped_column(tz())


class NumberOrder(IdMixin, TimestampMixin, Base):
    """``number_orders``: a Telnyx number order placed for an org."""

    __tablename__ = "number_orders"
    __table_args__ = (
        check_in("status", get_args(NumberOrderStatus)),
        Index("number_orders_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    telnyx_order_id: Mapped[str | None] = mapped_column(Text)
    requested_numbers: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    status: Mapped[NumberOrderStatus] = mapped_column(Text, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)


class NumberPool(IdMixin, TimestampMixin, Base):
    """``number_pools``: a named set of caller-ID numbers rotated by ``strategy``."""

    __tablename__ = "number_pools"
    __table_args__ = (
        check_in("strategy", get_args(NumberPoolStrategy)),
        UniqueConstraint("org_id", "name"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    strategy: Mapped[NumberPoolStrategy] = text_column("round_robin")
    max_calls_per_number_per_day: Mapped[int] = int_column(100)


class NumberPoolMember(TimestampMixin, Base):
    """``number_pool_members``: a pool's phone number and its daily usage, keyed by the pair of ids."""

    __tablename__ = "number_pool_members"
    __table_args__ = (Index("number_pool_members_org_idx", "org_id"),)

    pool_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("number_pools.id", ondelete="CASCADE"), primary_key=True
    )
    phone_number_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("phone_numbers.id", ondelete="CASCADE"), primary_key=True
    )
    org_id: Mapped[uuid.UUID] = org_id_column()
    last_used_at: Mapped[datetime | None] = mapped_column(tz())
    calls_today: Mapped[int] = int_column(0)
    calls_today_date: Mapped[date | None] = mapped_column(Date)


class TelnyxEvent(IdMixin, TimestampMixin, Base):
    """``telnyx_events``: raw inbound Telnyx webhooks, system scope (``TelnyxEventRepo``, specs/03 §2).

    ``org_id`` is NULL until the event is attributed; ``call_id`` has no foreign key because an event may be
    stored before its call row exists.
    """

    __tablename__ = "telnyx_events"
    __table_args__ = (
        Index("telnyx_events_call_idx", "call_id"),
        Index("telnyx_events_ccid_idx", "call_control_id"),
    )

    # Telnyx ``data.id``.
    telnyx_event_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    org_id: Mapped[uuid.UUID | None] = org_id_column(nullable=True)
    call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    call_control_id: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(tz(), nullable=False)
    payload: Mapped[dict[str, Any]] = jsonb_column()
    processed_at: Mapped[datetime | None] = mapped_column(tz())
    process_error: Mapped[str | None] = mapped_column(Text)
