"""Webhooks and billing: endpoints, the delivery outbox, plans, subscriptions, credits, usage, invoices,
Stripe event dedupe (specs/03-data-model.md §9).

Follows the conventions of :mod:`va_db.models.tenancy`: explicit SQL types, ``Literal`` aliases enforced by
:func:`check_in`, and spec-named indexes. Per the §1 decision, every tenant table has an ``org_id`` index.
``plans`` and ``stripe_events`` are system scope: text primary keys, no ``org_id``, no RLS.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal, get_args

from sqlalchemy import (
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from va_db.base import Base, IdMixin, TimestampMixin
from va_db.models._checks import check_in
from va_db.models._columns import (
    bool_column,
    fk_column,
    jsonb_column,
    micros_column,
    org_id_column,
    text_array_column,
    text_column,
    tz,
)

__all__ = [
    "CreditEntryType",
    "CreditHold",
    "CreditHoldStatus",
    "CreditLedger",
    "Invoice",
    "InvoiceStatus",
    "OrgSubscription",
    "Plan",
    "StripeEvent",
    "SubscriptionStatus",
    "UsageEvent",
    "UsageMeter",
    "WebhookDelivery",
    "WebhookDeliveryStatus",
    "WebhookEndpoint",
    "WebhookFormat",
]

WebhookFormat = Literal["bland", "envelope"]
WebhookDeliveryStatus = Literal["pending", "delivering", "succeeded", "failed", "dead"]
SubscriptionStatus = Literal["active", "past_due", "canceled", "trialing"]
CreditEntryType = Literal[
    "purchase",
    "auto_recharge",
    "grant",
    "refund",
    "usage",
    "adjustment",
    "subscription_fee",
    "hold",
    "hold_release",
    "transfer_in",
    "transfer_out",
]
CreditHoldStatus = Literal["held", "captured", "released"]
UsageMeter = Literal[
    "call_connected_seconds",
    "call_transfer_seconds",
    "call_attempt",
    "sms_segment_out",
    "sms_segment_in",
    "widget_message",
    "tts_characters",
    "kb_embedding_tokens",
    "voice_clone",
    "phone_number_month",
    "llm_tokens_in",
    "llm_tokens_out",
    "stt_seconds",
]
InvoiceStatus = Literal["draft", "finalized", "paid", "void"]


class WebhookEndpoint(IdMixin, TimestampMixin, Base):
    """``webhook_endpoints``: an org-level webhook subscription, in addition to the per-call ``webhook``.

    ``event_types`` ``{*}`` subscribes to every event; under HIPAA mode transcripts are included only when
    ``phi_allowed`` is true.
    """

    __tablename__ = "webhook_endpoints"
    __table_args__ = (Index("webhook_endpoints_org_idx", "org_id"),)

    org_id: Mapped[uuid.UUID] = org_id_column()
    url: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = text_column("")
    event_types: Mapped[list[str]] = text_array_column("{*}")
    enabled: Mapped[bool] = bool_column(default=True)
    phi_allowed: Mapped[bool] = bool_column(default=False)


class WebhookDelivery(IdMixin, TimestampMixin, Base):
    """``webhook_deliveries``: the transactional outbox of outbound webhooks, one row per event and URL.

    ``event_id`` is ``evt_<uuid>``. ``call_id``, ``batch_id``, and ``conversation_id`` carry no foreign key,
    as in the spec, so delivery history outlives the resources it reports on.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        check_in("format", get_args(WebhookFormat)),
        check_in("status", get_args(WebhookDeliveryStatus)),
        UniqueConstraint("event_id", "url"),
        Index(
            "webhook_deliveries_due_idx",
            "next_attempt_at",
            postgresql_where=text("status IN ('pending', 'delivering')"),
        ),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    event_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    endpoint_id: Mapped[uuid.UUID | None] = fk_column("webhook_endpoints.id", "SET NULL")
    payload: Mapped[dict[str, Any]] = jsonb_column()
    format: Mapped[WebhookFormat] = text_column("bland")
    status: Mapped[WebhookDeliveryStatus] = text_column("pending")
    attempts: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    next_attempt_at: Mapped[datetime] = mapped_column(tz(), nullable=False, server_default=func.now())
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    last_response_ms: Mapped[int | None] = mapped_column(Integer)
    call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    delivered_at: Mapped[datetime | None] = mapped_column(tz())


Index("webhook_deliveries_org_idx", WebhookDelivery.org_id, WebhookDelivery.created_at.desc())


class Plan(TimestampMixin, Base):
    """``plans``: the platform plan catalog (system scope), keyed by ``start``/``build``/``scale``/
    ``enterprise``; prices per specs/24-billing-and-limits.
    """

    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    monthly_fee_micros: Mapped[int] = micros_column()
    connected_rate_micros_per_min: Mapped[int] = micros_column()
    transfer_rate_micros_per_min: Mapped[int] = micros_column()
    min_attempt_micros: Mapped[int] = micros_column()
    sms_rate_micros: Mapped[int] = micros_column()
    widget_message_micros: Mapped[int] = micros_column()
    # Standalone TTS product price.
    tts_rate_micros_per_1k_chars: Mapped[int] = micros_column(0)
    # The caps are unlimited when NULL.
    daily_call_cap: Mapped[int | None] = mapped_column(Integer)
    hourly_call_cap: Mapped[int | None] = mapped_column(Integer)
    concurrency_cap: Mapped[int | None] = mapped_column(Integer)
    voice_clone_cap: Mapped[int | None] = mapped_column(Integer)
    features: Mapped[list[str]] = text_array_column("{}")
    stripe_price_id: Mapped[str | None] = mapped_column(Text)
    is_public: Mapped[bool] = bool_column(default=True)


class OrgSubscription(IdMixin, TimestampMixin, Base):
    """``org_subscriptions``: an org's plan subscription; at most one is active, past due, or trialing.

    ``plan_id`` has no ``ON DELETE`` clause (NO ACTION), as in the spec: a subscribed plan cannot be deleted.
    """

    __tablename__ = "org_subscriptions"
    __table_args__ = (
        check_in("status", get_args(SubscriptionStatus)),
        Index(
            "org_subscriptions_active_uq",
            "org_id",
            unique=True,
            postgresql_where=text("status IN ('active', 'past_due', 'trialing')"),
        ),
        Index("org_subscriptions_org_idx", "org_id"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    plan_id: Mapped[str] = mapped_column(Text, ForeignKey("plans.id"), nullable=False)
    status: Mapped[SubscriptionStatus] = mapped_column(Text, nullable=False)
    stripe_subscription_id: Mapped[str | None] = mapped_column(Text, unique=True)
    current_period_start: Mapped[datetime] = mapped_column(tz(), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(tz(), nullable=False)
    cancel_at_period_end: Mapped[bool] = bool_column(default=False)


class CreditLedger(IdMixin, TimestampMixin, Base):
    """``credit_ledger``: the append-only credit ledger; ``amount_micros`` is positive for credits and
    negative for debits.

    ``reference_type`` is one of ``call``, ``sms``, ``widget_message``, ``stripe_payment``, ``subscription``,
    ``manual``, ``sub_account``.
    """

    __tablename__ = "credit_ledger"
    __table_args__ = (
        check_in("entry_type", get_args(CreditEntryType)),
        UniqueConstraint("org_id", "idempotency_key"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    entry_type: Mapped[CreditEntryType] = mapped_column(Text, nullable=False)
    amount_micros: Mapped[int] = micros_column()
    balance_after_micros: Mapped[int] = micros_column()
    reference_type: Mapped[str | None] = mapped_column(Text)
    reference_id: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = text_column("")
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)


Index("credit_ledger_org_created_idx", CreditLedger.org_id, CreditLedger.created_at.desc())


class CreditHold(IdMixin, TimestampMixin, Base):
    """``credit_holds``: credit reserved for an in-flight resource until captured, released, or expired."""

    __tablename__ = "credit_holds"
    __table_args__ = (
        check_in("status", get_args(CreditHoldStatus)),
        Index("credit_holds_org_status_idx", "org_id", "status"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    reference_type: Mapped[str] = mapped_column(Text, nullable=False)
    reference_id: Mapped[str] = mapped_column(Text, nullable=False)
    amount_micros: Mapped[int] = micros_column()
    status: Mapped[CreditHoldStatus] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(tz(), nullable=False)


class UsageEvent(IdMixin, TimestampMixin, Base):
    """``usage_events``: the authoritative metering rows, also mirrored to ClickHouse.

    ``unit_price_micros`` is the customer price per unit at the time of the event and ``cost_micros`` our
    vendor cost estimate (COGS).
    """

    __tablename__ = "usage_events"
    __table_args__ = (
        check_in("meter", get_args(UsageMeter)),
        UniqueConstraint("org_id", "idempotency_key"),
    )

    org_id: Mapped[uuid.UUID] = org_id_column()
    meter: Mapped[UsageMeter] = mapped_column(Text, nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    unit_price_micros: Mapped[int] = micros_column()
    amount_micros: Mapped[int] = micros_column()
    cost_micros: Mapped[int] = micros_column(0)
    reference_type: Mapped[str] = mapped_column(Text, nullable=False)
    reference_id: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(tz(), nullable=False)


Index("usage_events_org_time_idx", UsageEvent.org_id, UsageEvent.occurred_at.desc())


class Invoice(IdMixin, TimestampMixin, Base):
    """``invoices``: one billing period's invoice with its ``line_items``."""

    __tablename__ = "invoices"
    __table_args__ = (check_in("status", get_args(InvoiceStatus)),)

    org_id: Mapped[uuid.UUID] = org_id_column()
    period_start: Mapped[datetime] = mapped_column(tz(), nullable=False)
    period_end: Mapped[datetime] = mapped_column(tz(), nullable=False)
    subtotal_micros: Mapped[int] = micros_column()
    credits_applied_micros: Mapped[int] = micros_column()
    total_micros: Mapped[int] = micros_column()
    line_items: Mapped[list[Any]] = jsonb_column()
    status: Mapped[InvoiceStatus] = mapped_column(Text, nullable=False)
    stripe_invoice_id: Mapped[str | None] = mapped_column(Text, unique=True)
    pdf_s3_key: Mapped[str | None] = mapped_column(Text)


Index("invoices_org_idx", Invoice.org_id, Invoice.period_start.desc())


class StripeEvent(TimestampMixin, Base):
    """``stripe_events``: processed Stripe webhook events (system scope), keyed by the Stripe event id for
    deduplication.
    """

    __tablename__ = "stripe_events"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = jsonb_column()
    processed_at: Mapped[datetime | None] = mapped_column(tz())
