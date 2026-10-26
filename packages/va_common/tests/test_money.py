from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from va_common.money import MICROS_PER_DOLLAR, dollars_to_micros, micros_to_dollars, micros_to_dollars_str


@pytest.mark.parametrize(
    ("value", "micros"),
    [
        (0, 0),
        (1, 1_000_000),
        ("0.09", 90_000),
        (" 2.5 ", 2_500_000),
        (Decimal("1.2345675"), 1_234_568),
        (Decimal("1.2345665"), 1_234_566),
        (0.1, 100_000),
        (-0.09, -90_000),
        ("-12.000001", -12_000_001),
        ("1e-6", 1),
        (Decimal("0.0000004"), 0),
    ],
)
def test_dollars_to_micros(value: Decimal | int | str | float, micros: int) -> None:
    assert dollars_to_micros(value) == micros


@pytest.mark.parametrize("value", ["abc", "", "NaN", "inf", float("nan"), float("-inf"), Decimal("Infinity")])
def test_dollars_to_micros_rejects_invalid_values(value: Decimal | str | float) -> None:
    with pytest.raises(ValueError, match=r"decimal amount|finite"):
        dollars_to_micros(value)


@pytest.mark.parametrize("value", [True, None, [1]])
def test_dollars_to_micros_rejects_invalid_types(value: object) -> None:
    with pytest.raises(TypeError):
        dollars_to_micros(value)  # type: ignore[arg-type]


def test_micros_to_dollars() -> None:
    assert MICROS_PER_DOLLAR == 1_000_000
    assert micros_to_dollars(1_500_000) == Decimal("1.500000")
    assert micros_to_dollars_str(1_500_000) == "1.500000"
    assert micros_to_dollars_str(-90_000) == "-0.090000"
    assert micros_to_dollars_str(0) == "0.000000"
    assert micros_to_dollars_str(1) == "0.000001"


@pytest.mark.parametrize("value", [True, 1.5, "1"])
def test_micros_helpers_reject_non_int(value: object) -> None:
    with pytest.raises(TypeError):
        micros_to_dollars(value)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        micros_to_dollars_str(value)  # type: ignore[arg-type]


@given(st.integers(min_value=-(10**15), max_value=10**15))
def test_micros_round_trip(micros: int) -> None:
    assert dollars_to_micros(micros_to_dollars_str(micros)) == micros
    assert dollars_to_micros(micros_to_dollars(micros)) == micros


@given(
    st.decimals(
        min_value=Decimal(-(10**9)), max_value=Decimal(10**9), places=6, allow_nan=False, allow_infinity=False
    )
)
def test_six_place_decimals_round_trip_exactly(value: Decimal) -> None:
    assert micros_to_dollars(dollars_to_micros(value)) == value
