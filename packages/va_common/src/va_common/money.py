"""Integer micro-dollar money helpers (``1 USD = 1_000_000`` micros)."""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from typing import Final

__all__ = ["MICROS_PER_DOLLAR", "dollars_to_micros", "micros_to_dollars", "micros_to_dollars_str"]

MICROS_PER_DOLLAR: Final = 1_000_000
_SIX_PLACES: Final = Decimal("0.000001")


def _to_decimal(value: Decimal | int | str | float) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str | float):
        msg = f"unsupported money type: {type(value).__name__}"
        raise TypeError(msg)
    try:
        result = Decimal(str(value).strip()) if isinstance(value, str | float) else Decimal(value)
    except InvalidOperation:
        msg = f"not a decimal amount: {value!r}"
        raise ValueError(msg) from None
    if not result.is_finite():
        msg = f"amount must be finite: {value!r}"
        raise ValueError(msg)
    return result


def dollars_to_micros(value: Decimal | int | str | float) -> int:
    """Convert a dollar amount to integer micros, rounding half-even at the sixth decimal place."""
    dollars = _to_decimal(value).quantize(_SIX_PLACES, rounding=ROUND_HALF_EVEN)
    return int(dollars * MICROS_PER_DOLLAR)


def _check_micros(micros: int) -> None:
    if isinstance(micros, bool) or not isinstance(micros, int):
        msg = f"micros must be int, got {type(micros).__name__}"
        raise TypeError(msg)


def micros_to_dollars(micros: int) -> Decimal:
    """Exact dollar amount with six decimal places."""
    _check_micros(micros)
    return (Decimal(micros) / MICROS_PER_DOLLAR).quantize(_SIX_PLACES)


def micros_to_dollars_str(micros: int) -> str:
    """Fixed six-decimal dollar string, e.g. ``1_500_000 -> "1.500000"``."""
    return f"{micros_to_dollars(micros):.6f}"
