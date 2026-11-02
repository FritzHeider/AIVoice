"""Pure inbound routing decisions (specs/06-telephony-telnyx.md §7 steps 1-6, Decision P05-T12b)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from va_domain.numbers import InboundConfig
from va_edge.routing import Accept, Fallback, Reject, RoutingFacts, decide, fallback_of

NOW = datetime(2026, 11, 2, 15, 0, tzinfo=UTC)
CALLER = "+14155550123"
FALLBACK = "+14155550188"


def facts(config: dict[str, Any] | None = None, **values: Any) -> RoutingFacts:
    parsed = InboundConfig.model_validate(config or {})
    defaults: dict[str, Any] = {
        "config": parsed,
        "fallback_number": parsed.fallback_number,
        "caller": CALLER,
        "org_status": "active",
        "available_micros": 1_000_000,
        "min_attempt_micros": 15_000,
        "caller_dnc": False,
        "now": NOW,
    }
    return RoutingFacts(**(defaults | values))


def with_fallback(**config: Any) -> dict[str, Any]:
    return {"fallback_number": FALLBACK, **config}


def test_a_plain_number_accepts_with_its_config() -> None:
    routed = facts({"prompt": "Hi"})
    assert decide(routed) == Accept(routed.config)  # type: ignore[arg-type]


def test_invalid_config_falls_back_only_with_a_fallback_number() -> None:
    assert decide(replace(facts(), config=None, fallback_number=None)) == Reject(
        "CALL_REJECTED", "invalid_inbound_config"
    )
    assert decide(replace(facts(), config=None, fallback_number=FALLBACK)) == Fallback(
        FALLBACK, "invalid_inbound_config"
    )


def test_blocked_caller_is_rejected_even_with_a_fallback_and_before_the_allow_list() -> None:
    config = with_fallback(blocked_callers=[CALLER], allowed_callers=[CALLER])
    assert decide(facts(config)) == Reject("CALL_REJECTED", "caller_blocked")


def test_allow_list() -> None:
    assert decide(facts(with_fallback(allowed_callers=["+14155550999"]))) == Reject(
        "CALL_REJECTED", "caller_not_allowed"
    )
    assert isinstance(decide(facts({"allowed_callers": [CALLER]})), Accept)
    assert isinstance(decide(facts({"blocked_callers": ["+14155550999"]})), Accept)


def test_anonymous_callers_never_match_the_allow_list_and_are_never_blocked() -> None:
    assert decide(facts({"allowed_callers": [CALLER]}, caller=None)) == Reject(
        "CALL_REJECTED", "caller_not_allowed"
    )
    assert isinstance(decide(facts({"blocked_callers": [CALLER]}, caller=None)), Accept)


@pytest.mark.parametrize("status", ["suspended", "deleted"])
def test_inactive_org(status: str) -> None:
    assert decide(facts(org_status=status)) == Reject("CALL_REJECTED", f"org_{status}")
    assert decide(facts(with_fallback(), org_status=status)) == Fallback(FALLBACK, f"org_{status}")


def test_org_status_beats_credits() -> None:
    assert decide(facts(org_status="suspended", available_micros=0)) == Reject(
        "CALL_REJECTED", "org_suspended"
    )


def test_credits_at_the_minimum_accept_one_below_refuses() -> None:
    assert isinstance(decide(facts(available_micros=15_000)), Accept)
    assert decide(facts(available_micros=14_999)) == Reject("CALL_REJECTED", "insufficient_credits")
    assert decide(facts(with_fallback(), available_micros=-5)) == Fallback(FALLBACK, "insufficient_credits")


def test_dnc_callers_are_rejected_only_when_the_number_blocks_them() -> None:
    assert isinstance(decide(facts(caller_dnc=True)), Accept)
    assert decide(facts(with_fallback(block_dnc_callers=True), caller_dnc=True)) == Reject(
        "CALL_REJECTED", "caller_dnc"
    )
    assert isinstance(decide(facts({"block_dnc_callers": True}, caller_dnc=False)), Accept)


def window(**values: Any) -> dict[str, Any]:
    return {"enabled": True, **values}


def test_maintenance_inside_the_window() -> None:
    inside = window(
        starts_at=(NOW - timedelta(hours=1)).isoformat(), ends_at=(NOW + timedelta(hours=1)).isoformat()
    )
    assert decide(facts(with_fallback(maintenance=inside))) == Fallback(FALLBACK, "maintenance")
    assert decide(facts({"maintenance": inside})) == Reject("CALL_REJECTED", "maintenance")
    routed = facts({"maintenance": {**inside, "message": "We are closed today."}})
    assert decide(routed) == Accept(routed.config, maintenance_message=True)  # type: ignore[arg-type]
    both = facts(with_fallback(maintenance={**inside, "message": "We are closed today."}))
    assert decide(both) == Fallback(FALLBACK, "maintenance")


def test_maintenance_outside_the_window_or_disabled_accepts() -> None:
    later = window(starts_at=(NOW + timedelta(minutes=1)).isoformat())
    assert isinstance(decide(facts(with_fallback(maintenance=later))), Accept)
    assert isinstance(decide(facts(with_fallback(maintenance={"enabled": False}))), Accept)
    open_ended = window()
    assert decide(facts(with_fallback(maintenance=open_ended))) == Fallback(FALLBACK, "maintenance")


def test_dnc_beats_maintenance() -> None:
    config = with_fallback(block_dnc_callers=True, maintenance=window())
    assert decide(facts(config, caller_dnc=True)) == Reject("CALL_REJECTED", "caller_dnc")


def test_fallback_of_an_invalid_config_keeps_only_a_valid_e164_value() -> None:
    parsed = InboundConfig.model_validate({"fallback_number": FALLBACK})
    assert fallback_of(parsed, {}) == FALLBACK
    assert fallback_of(None, {"fallback_number": FALLBACK, "from": "+1"}) == FALLBACK
    assert fallback_of(None, {"fallback_number": "(415) 555-0188"}) is None
    assert fallback_of(None, {"fallback_number": "not a number"}) is None
    assert fallback_of(None, {"fallback_number": 14155550188}) is None
    assert fallback_of(None, {}) is None
