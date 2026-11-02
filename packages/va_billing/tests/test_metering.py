"""Pure call metering (specs/24 §3): meters, rounding, attempt minimum, BYO transfer, snapshot parsing."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from va_billing.metering import (
    CallNotEndedError,
    CallUsageFacts,
    PlannedUsage,
    call_idempotency_key,
    plan_call_usage,
    pricing_from_snapshot,
    round_half_up,
    seconds_amount,
)
from va_billing.plans import EffectivePricing
from va_common.ids import new_id
from va_db.models import CallDirection

PRICING = EffectivePricing(
    connected_rate_micros_per_min=120_000,
    transfer_rate_micros_per_min=60_000,
    min_attempt_micros=15_000,
    sms_rate_micros=20_000,
    widget_message_micros=10_000,
    tts_rate_micros_per_1k_chars=120_000,
)
CALL_ID = new_id()


def facts(  # noqa: PLR0913 - keyword-only call facts
    *,
    connected: str = "0",
    transfer: str = "0",
    direction: CallDirection = "outbound",
    dialed: bool = True,
    byo: bool = False,
    pricing: EffectivePricing = PRICING,
) -> CallUsageFacts:
    return CallUsageFacts(
        call_id=CALL_ID,
        direction=direction,
        dialed=dialed,
        connected_seconds=Decimal(connected),
        transfer_seconds=Decimal(transfer),
        byo=byo,
        pricing=pricing,
    )


def amounts(planned: list[PlannedUsage]) -> dict[str, int]:
    return {usage.meter: usage.amount_micros for usage in planned}


def test_ten_second_call_costs_two_cents() -> None:
    planned = plan_call_usage(facts(connected="10"))
    assert amounts(planned) == {"call_connected_seconds": 20_000, "call_attempt": 0}
    assert sum(usage.amount_micros for usage in planned) == 20_000
    connected = planned[0]
    assert connected.quantity == Decimal(10)
    assert connected.unit_price_micros == 2_000


def test_three_second_call_costs_the_attempt_minimum() -> None:
    planned = plan_call_usage(facts(connected="3"))
    assert amounts(planned) == {"call_connected_seconds": 6_000, "call_attempt": 9_000}
    assert sum(usage.amount_micros for usage in planned) == 15_000


def test_unanswered_dialed_outbound_costs_the_minimum() -> None:
    (attempt,) = plan_call_usage(facts())
    assert attempt.meter == "call_attempt"
    assert attempt.quantity == Decimal(1)
    assert attempt.unit_price_micros == 15_000
    assert attempt.amount_micros == 15_000


def test_never_dialed_call_costs_nothing() -> None:
    assert plan_call_usage(facts(dialed=False)) == []


def test_inbound_call_has_no_attempt_minimum() -> None:
    assert amounts(plan_call_usage(facts(connected="3", direction="inbound"))) == {
        "call_connected_seconds": 6_000
    }
    assert plan_call_usage(facts(direction="inbound")) == []


def test_transfer_seconds_use_the_transfer_rate() -> None:
    planned = plan_call_usage(facts(connected="30", transfer="90"))
    assert [usage.meter for usage in planned] == [
        "call_connected_seconds",
        "call_transfer_seconds",
        "call_attempt",
    ]
    assert amounts(planned) == {
        "call_connected_seconds": 60_000,
        "call_transfer_seconds": 90_000,
        "call_attempt": 0,
    }
    assert planned[1].unit_price_micros == 1_000


def test_byo_transfer_posts_zero_price() -> None:
    planned = plan_call_usage(facts(connected="30", transfer="90", byo=True))
    transfer = planned[1]
    assert transfer.meter == "call_transfer_seconds"
    assert transfer.quantity == Decimal(90)
    assert transfer.unit_price_micros == 0
    assert transfer.amount_micros == 0
    assert amounts(planned)["call_connected_seconds"] == 60_000


def test_rounding_half_up_with_millisecond_quantity() -> None:
    assert seconds_amount(Decimal("61.437"), 120_000) == 122_874
    assert seconds_amount(Decimal("0.001"), 90_000) == 2  # exactly 1.5 µ$ rounds up
    assert seconds_amount(Decimal("0.001"), 30_000) == 1  # 0.5 rounds up
    assert round_half_up(Decimal("2.4999")) == 2
    planned = plan_call_usage(facts(connected="61.437"))
    assert amounts(planned) == {"call_connected_seconds": 122_874, "call_attempt": 0}


def test_unit_price_rounds_the_per_second_rate() -> None:
    odd = replace(PRICING, connected_rate_micros_per_min=100_000)  # 1 666.67 µ$/s
    (connected, _) = plan_call_usage(facts(connected="61.437", pricing=odd))
    assert connected.unit_price_micros == 1_667
    assert connected.amount_micros == 102_395  # exact rate: 61.437 * 100 000 / 60 = 102 395


def test_idempotency_keys() -> None:
    assert call_idempotency_key(CALL_ID, "call_connected_seconds") == f"call:{CALL_ID}:connected"
    assert call_idempotency_key(CALL_ID, "call_transfer_seconds") == f"call:{CALL_ID}:transfer"
    assert call_idempotency_key(CALL_ID, "call_attempt") == f"call:{CALL_ID}:attempt"
    keys = [usage.idempotency_key for usage in plan_call_usage(facts(connected="5", transfer="5"))]
    assert keys == [
        f"call:{CALL_ID}:connected",
        f"call:{CALL_ID}:transfer",
        f"call:{CALL_ID}:attempt",
    ]


def test_pricing_snapshot_round_trip() -> None:
    assert pricing_from_snapshot(PRICING.snapshot()) == PRICING


@pytest.mark.parametrize(
    "snapshot",
    [
        None,
        "pricing",
        {},
        {key: value for key, value in PRICING.snapshot().items() if key != "min_attempt_micros"},
        PRICING.snapshot() | {"connected_rate_micros_per_min": True},
        PRICING.snapshot() | {"transfer_rate_micros_per_min": "60000"},
        PRICING.snapshot() | {"min_attempt_micros": 1.5},
    ],
)
def test_pricing_snapshot_rejects_incomplete_values(snapshot: object) -> None:
    assert pricing_from_snapshot(snapshot) is None


def test_call_not_ended_error_carries_the_status() -> None:
    error = CallNotEndedError("in_progress")
    assert error.status == "in_progress"
    assert "in_progress" in str(error)
