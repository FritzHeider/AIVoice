"""Webhook, billing, and §9a models against Postgres, on the rolled-back ``scratch_session`` schema."""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from model_factories import (
    ModelBuilder,
    assert_check_enforced,
    assert_enum_check_enforced,
    assert_rejected,
    flushed,
    make_org,
)
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from va_db.base import Base
from va_db.models import (
    CreditEntryType,
    CreditHold,
    CreditHoldStatus,
    CreditLedger,
    DncImport,
    ErasureRequest,
    Invoice,
    InvoiceStatus,
    JobStatus,
    Org,
    OrgDataKey,
    OrgDataKeyStatus,
    OrgLlmProvider,
    OrgSecret,
    OrgSubscription,
    Plan,
    StripeEvent,
    SubscriptionStatus,
    UsageEvent,
    UsageMeter,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookEndpoint,
    WebhookFormat,
)

pytestmark = pytest.mark.integration

_PERIOD_START = datetime(2026, 10, 1, tzinfo=UTC)
_PERIOD_END = datetime(2026, 11, 1, tzinfo=UTC)


async def _org_id(session: AsyncSession) -> uuid.UUID:
    return (await make_org(session)).id


async def _plan(session: AsyncSession, plan_id: str | None = None) -> Plan:
    plan = Plan(
        id=plan_id or f"plan_{secrets.token_hex(4)}",
        name="Build",
        monthly_fee_micros=299_000_000,
        connected_rate_micros_per_min=120_000,
        transfer_rate_micros_per_min=50_000,
        min_attempt_micros=15_000,
        sms_rate_micros=20_000,
        widget_message_micros=5_000,
    )
    return await flushed(session, plan)


def _subscription(org_id: uuid.UUID, plan: Plan, status: SubscriptionStatus) -> OrgSubscription:
    return OrgSubscription(
        org_id=org_id,
        plan_id=plan.id,
        status=status,
        current_period_start=_PERIOD_START,
        current_period_end=_PERIOD_END,
    )


def _delivery(org_id: uuid.UUID, **values: Any) -> WebhookDelivery:
    values.setdefault("event_id", f"evt_{uuid.uuid4()}")
    values.setdefault("url", "https://hooks.example.test/calls")
    return WebhookDelivery(org_id=org_id, event_type="call.completed", payload={"call_id": "c1"}, **values)


def _ledger(org_id: uuid.UUID, key: str) -> CreditLedger:
    return CreditLedger(
        org_id=org_id,
        entry_type="purchase",
        amount_micros=50_000_000,
        balance_after_micros=50_000_000,
        idempotency_key=key,
    )


def _usage(org_id: uuid.UUID, key: str, **values: Any) -> UsageEvent:
    values.setdefault("quantity", Decimal("61.5"))
    return UsageEvent(
        org_id=org_id,
        meter="call_connected_seconds",
        unit_price_micros=2_000,
        amount_micros=123_000,
        reference_type="call",
        reference_id=str(uuid.uuid4()),
        idempotency_key=key,
        occurred_at=_PERIOD_START,
        **values,
    )


def _data_key(org_id: uuid.UUID, status: OrgDataKeyStatus) -> OrgDataKey:
    return OrgDataKey(org_id=org_id, kek_id="alias/va-kek", encrypted_data_key=os.urandom(48), status=status)


async def _build_delivery(session: AsyncSession) -> Base:
    return _delivery(await _org_id(session))


async def _build_subscription(session: AsyncSession) -> Base:
    # canceled, so the partial "one live subscription" index never interferes with the check under test
    return _subscription(await _org_id(session), await _plan(session), "canceled")


async def _build_ledger(session: AsyncSession) -> Base:
    return _ledger(await _org_id(session), secrets.token_hex(8))


async def _build_hold(session: AsyncSession) -> Base:
    return CreditHold(
        org_id=await _org_id(session),
        reference_type="call",
        reference_id=str(uuid.uuid4()),
        amount_micros=600_000,
        status="held",
        expires_at=_PERIOD_END,
    )


async def _build_usage(session: AsyncSession) -> Base:
    return _usage(await _org_id(session), secrets.token_hex(8))


async def _build_invoice(session: AsyncSession) -> Base:
    return Invoice(
        org_id=await _org_id(session),
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        subtotal_micros=1_000_000,
        credits_applied_micros=0,
        total_micros=1_000_000,
        line_items=[],
        status="draft",
    )


async def _build_dnc_import(session: AsyncSession) -> Base:
    return DncImport(org_id=await _org_id(session), status="queued", s3_key="imports/dnc.csv")


async def _build_erasure(session: AsyncSession) -> Base:
    return ErasureRequest(
        org_id=await _org_id(session), selector={"email": "pat@example.test"}, status="queued"
    )


async def _build_data_key(session: AsyncSession) -> Base:
    return _data_key(await _org_id(session), "retired")


async def _build_secret(session: AsyncSession) -> Base:
    return OrgSecret(org_id=await _org_id(session), name="SEED", value_enc=os.urandom(16))


@pytest.mark.parametrize(
    ("builder", "column", "literal"),
    [
        (_build_delivery, "format", WebhookFormat),
        (_build_delivery, "status", WebhookDeliveryStatus),
        (_build_subscription, "status", SubscriptionStatus),
        (_build_ledger, "entry_type", CreditEntryType),
        (_build_hold, "status", CreditHoldStatus),
        (_build_usage, "meter", UsageMeter),
        (_build_invoice, "status", InvoiceStatus),
        (_build_dnc_import, "status", JobStatus),
        (_build_erasure, "status", JobStatus),
        (_build_data_key, "status", OrgDataKeyStatus),
    ],
)
async def test_enum_values_accepted_and_rejected(
    scratch_session: AsyncSession, builder: ModelBuilder, column: str, literal: Any
) -> None:
    await assert_enum_check_enforced(scratch_session, builder, column, literal)


async def test_org_secret_name_check(scratch_session: AsyncSession) -> None:
    await assert_check_enforced(
        scratch_session,
        _build_secret,
        "name",
        ("API_KEY", "A", "X" * 64, "CRM_2"),
        ("api_key", "1KEY", "_KEY", "X" * 65, "API-KEY"),
    )


async def test_defaults(scratch_session: AsyncSession) -> None:
    org_id = await _org_id(scratch_session)
    endpoint = await flushed(
        scratch_session, WebhookEndpoint(org_id=org_id, url="https://hooks.example.test")
    )
    assert (endpoint.description, endpoint.event_types) == ("", ["*"])
    assert (endpoint.enabled, endpoint.phi_allowed) == (True, False)

    delivery = await flushed(scratch_session, _delivery(org_id, endpoint_id=endpoint.id))
    assert (delivery.format, delivery.status, delivery.attempts) == ("bland", "pending", 0)
    assert delivery.next_attempt_at == delivery.created_at

    plan = await _plan(scratch_session)
    assert (plan.tts_rate_micros_per_1k_chars, plan.features, plan.is_public) == (0, [], True)
    assert (plan.daily_call_cap, plan.hourly_call_cap, plan.concurrency_cap, plan.voice_clone_cap) == (
        None,
        None,
        None,
        None,
    )
    subscription = await flushed(scratch_session, _subscription(org_id, plan, "trialing"))
    assert subscription.cancel_at_period_end is False

    ledger = await flushed(scratch_session, _ledger(org_id, "stripe:pi_1"))
    assert (ledger.description, ledger.reference_type) == ("", None)

    usage = await flushed(scratch_session, _usage(org_id, "call:1", quantity=Decimal("1.5")))
    assert usage.cost_micros == 0
    assert usage.quantity == Decimal("1.500000")

    provider = await flushed(
        scratch_session,
        OrgLlmProvider(
            org_id=org_id,
            base_url="https://llm.example.test/v1",
            api_key_enc=os.urandom(32),
            models={"dialogue": "m1", "router": "m1", "analysis": "m2", "fallback": "m2"},
        ),
    )
    assert (provider.supports_tool_choice, provider.baa_covered) == (True, False)


async def test_one_live_subscription_per_org(scratch_session: AsyncSession) -> None:
    org_id = await _org_id(scratch_session)
    plan = await _plan(scratch_session)
    await flushed(scratch_session, _subscription(org_id, plan, "canceled"))
    await flushed(scratch_session, _subscription(org_id, plan, "active"))
    for status in ("active", "past_due", "trialing"):
        await assert_rejected(
            scratch_session, _subscription(org_id, plan, status), "org_subscriptions_active_uq"
        )
    await flushed(scratch_session, _subscription(org_id, plan, "canceled"))
    await flushed(scratch_session, _subscription(await _org_id(scratch_session), plan, "active"))


async def test_one_active_data_key_per_org(scratch_session: AsyncSession) -> None:
    org_id = await _org_id(scratch_session)
    await flushed(scratch_session, _data_key(org_id, "retired"))
    await flushed(scratch_session, _data_key(org_id, "retired"))
    await flushed(scratch_session, _data_key(org_id, "active"))
    await assert_rejected(scratch_session, _data_key(org_id, "active"), "org_data_keys_active_uq")
    await flushed(scratch_session, _data_key(await _org_id(scratch_session), "active"))


async def test_uniques(scratch_session: AsyncSession) -> None:
    org_id, other_org_id = await _org_id(scratch_session), await _org_id(scratch_session)

    first = await flushed(scratch_session, _delivery(org_id))
    await assert_rejected(
        scratch_session, _delivery(org_id, event_id=first.event_id), "webhook_deliveries_event_id_url_key"
    )
    await flushed(
        scratch_session, _delivery(org_id, event_id=first.event_id, url="https://other.example.test")
    )

    for build, constraint in (
        (_ledger, "credit_ledger_org_id_idempotency_key_key"),
        (_usage, "usage_events_org_id_idempotency_key_key"),
    ):
        await flushed(scratch_session, build(org_id, "key-1"))
        await assert_rejected(scratch_session, build(org_id, "key-1"), constraint)
        await flushed(scratch_session, build(other_org_id, "key-1"))

    def secret(owner: uuid.UUID) -> OrgSecret:
        return OrgSecret(org_id=owner, name="CRM_TOKEN", value_enc=b"\x00\x01")

    await flushed(scratch_session, secret(org_id))
    await assert_rejected(scratch_session, secret(org_id), "org_secrets_org_id_name_key")
    await flushed(scratch_session, secret(other_org_id))

    def provider(owner: uuid.UUID) -> OrgLlmProvider:
        return OrgLlmProvider(org_id=owner, base_url="https://llm.example.test", api_key_enc=b"k", models={})

    await flushed(scratch_session, provider(org_id))
    await assert_rejected(scratch_session, provider(org_id), "org_llm_providers_org_id_key")


async def test_plan_is_system_scope_and_protected_by_subscriptions(scratch_session: AsyncSession) -> None:
    plan = await _plan(scratch_session, "build")
    unused = await _plan(scratch_session, "scale")
    await flushed(scratch_session, _subscription(await _org_id(scratch_session), plan, "active"))
    scratch_session.expunge_all()

    with pytest.raises(IntegrityError, match="org_subscriptions_plan_id_fkey"):
        async with scratch_session.begin_nested():
            await scratch_session.execute(delete(Plan).where(Plan.id == plan.id))
    await scratch_session.execute(delete(Plan).where(Plan.id == unused.id))
    assert (await scratch_session.execute(select(Plan.id))).scalars().all() == ["build"]

    event = await flushed(
        scratch_session, StripeEvent(id="evt_1Q2w3E", type="invoice.paid", payload={"id": "evt_1Q2w3E"})
    )
    assert (event.processed_at, event.id) == (None, "evt_1Q2w3E")
    scratch_session.expunge(event)
    await assert_rejected(
        scratch_session, StripeEvent(id="evt_1Q2w3E", type="invoice.paid", payload={}), "stripe_events_pkey"
    )


async def test_endpoint_delete_keeps_deliveries(scratch_session: AsyncSession) -> None:
    org_id = await _org_id(scratch_session)
    endpoint = await flushed(
        scratch_session, WebhookEndpoint(org_id=org_id, url="https://hooks.example.test")
    )
    delivery = await flushed(scratch_session, _delivery(org_id, endpoint_id=endpoint.id))
    scratch_session.expunge_all()

    await scratch_session.execute(delete(WebhookEndpoint).where(WebhookEndpoint.id == endpoint.id))
    stored = select(WebhookDelivery.endpoint_id).where(WebhookDelivery.id == delivery.id)
    assert (await scratch_session.execute(stored)).scalar_one() is None


async def _count(session: AsyncSession, model: type[Base], org_id: uuid.UUID) -> int:
    query = select(func.count()).select_from(model).where(model.__table__.c.org_id == org_id)
    return int((await session.execute(query)).scalar_one())


async def test_org_delete_cascades(scratch_session: AsyncSession) -> None:
    org_id = await _org_id(scratch_session)
    await flushed(
        scratch_session,
        OrgLlmProvider(org_id=org_id, base_url="https://llm.example.test", api_key_enc=b"k", models={}),
    )
    await flushed(scratch_session, _data_key(org_id, "active"))
    await flushed(scratch_session, _subscription(org_id, await _plan(scratch_session), "active"))
    models: list[type[Base]] = [OrgLlmProvider, OrgDataKey, OrgSubscription]
    assert [await _count(scratch_session, model, org_id) for model in models] == [1, 1, 1]
    scratch_session.expunge_all()

    await scratch_session.execute(delete(Org).where(Org.id == org_id))
    assert [await _count(scratch_session, model, org_id) for model in models] == [0, 0, 0]


async def test_encrypted_values_round_trip(scratch_session: AsyncSession) -> None:
    value = os.urandom(64)
    secret = await flushed(
        scratch_session, OrgSecret(org_id=await _org_id(scratch_session), name="API_KEY", value_enc=value)
    )
    scratch_session.expunge_all()

    stored = (await scratch_session.execute(select(OrgSecret).where(OrgSecret.id == secret.id))).scalar_one()
    assert stored.value_enc == value
    assert isinstance(stored.value_enc, bytes)


async def test_job_counters_and_results_are_optional(scratch_session: AsyncSession) -> None:
    job = await flushed(scratch_session, await _build_dnc_import(scratch_session))
    assert isinstance(job, DncImport)
    assert (job.total, job.inserted, job.invalid, job.duplicates, job.error) == (None,) * 5
    erasure = await flushed(scratch_session, await _build_erasure(scratch_session))
    assert isinstance(erasure, ErasureRequest)
    assert erasure.result is None

    erasure.result = {"calls_redacted": 3}
    erasure.status = "completed"
    await scratch_session.flush()
    await scratch_session.refresh(erasure)
    assert erasure.result == {"calls_redacted": 3}
