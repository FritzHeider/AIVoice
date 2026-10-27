"""Read the specs/24-billing-and-limits.md §2 plan catalogue as the rows ``0002_seed_plans`` must insert."""

from __future__ import annotations

import re
from dataclasses import astuple, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

__all__ = [
    "CAP_COLUMNS",
    "PRICE_COLUMNS",
    "SPEC_24",
    "SPEC_PLAN_COLUMNS",
    "SpecPlan",
    "paragraph_values",
    "parse_spec_plans",
    "table_cells",
    "table_prices",
]

SPEC_24: Final = Path(__file__).resolve().parents[4] / "specs" / "24-billing-and-limits.md"

PRICE_COLUMNS: Final = (
    "monthly_fee_micros",
    "connected_rate_micros_per_min",
    "transfer_rate_micros_per_min",
    "min_attempt_micros",
    "sms_rate_micros",
    "widget_message_micros",
)
"""The §2 table price columns, in table order (monthly fee through widget $/msg)."""

CAP_COLUMNS: Final = ("daily_call_cap", "hourly_call_cap", "concurrency_cap", "voice_clone_cap")
"""The §2 cap columns seeded into ``plans`` (the ``numbers`` cap is ``va_billing.plans.PLAN_NUMBER_CAPS``)."""

SPEC_PLAN_COLUMNS: Final = (
    "id",
    "name",
    *PRICE_COLUMNS,
    "tts_rate_micros_per_1k_chars",
    *CAP_COLUMNS,
    "features",
)
"""Seeded ``plans`` columns, in :class:`SpecPlan` field order."""

_MICROS_PER_DOLLAR: Final = 1_000_000
_TTS_CHARACTERS: Final = 1_000
_NUMBER: Final = r"\d{1,3}(?: \d{3})*"
# "Micro-dollar seed values" lists: four per-plan values (table order) or one value shared by every plan.
_FOUR: Final = rf"({_NUMBER}) / ({_NUMBER}) / ({_NUMBER}) / "  # first three values and the separator
_PER_PLAN: Final = {
    "connected_rate_micros_per_min": rf"connected {_FOUR}enterprise default ({_NUMBER})",
    "transfer_rate_micros_per_min": rf"transfer {_FOUR}({_NUMBER})",
    "monthly_fee_micros": rf"fees {_FOUR}({_NUMBER})",
}
_SHARED: Final = {
    "min_attempt_micros": rf"min attempt ({_NUMBER})",
    "sms_rate_micros": rf"SMS ({_NUMBER})",
    "widget_message_micros": rf"widget ({_NUMBER})",
}
_TTS_PRICE: Final = r"TTS standalone characters \$(\d+\.\d+)/char"


@dataclass(frozen=True, slots=True)
class SpecPlan:
    """One ``plans`` row as specs/24 §2 defines it; ``None`` caps are unlimited."""

    id: str
    name: str
    monthly_fee_micros: int
    connected_rate_micros_per_min: int
    transfer_rate_micros_per_min: int
    min_attempt_micros: int
    sms_rate_micros: int
    widget_message_micros: int
    tts_rate_micros_per_1k_chars: int
    daily_call_cap: int | None
    hourly_call_cap: int | None
    concurrency_cap: int | None
    voice_clone_cap: int | None
    features: tuple[str, ...]

    def as_row(self) -> tuple[Any, ...]:
        """The row in :data:`SPEC_PLAN_COLUMNS` order, ``features`` as the list a ``text[]`` reads back as."""
        *columns, features = astuple(self)
        return (*columns, list(features))


def _section() -> str:
    markdown = SPEC_24.read_text(encoding="utf-8")
    return markdown[markdown.index("\n## 2. ") : markdown.index("\n## 3. ")]


def _integer(cell: str) -> int:
    return int(cell.replace(" ", ""))


def _dollars(cell: str) -> int | None:
    """``$0.12`` -> 120 000 micro-dollars; ``custom...`` cells -> ``None`` (resolved from the seed values)."""
    if cell.startswith("custom"):
        return None
    return int(Decimal(cell.removeprefix("$")) * _MICROS_PER_DOLLAR)


def _cap(cell: str) -> int | None:
    return None if cell == "unlimited" else _integer(cell)


def _features(cell: str) -> tuple[str, ...]:
    inner = cell.strip("`{}")
    return tuple(name.strip() for name in inner.split(",")) if inner else ()


def table_cells() -> list[list[str]]:
    """The cells of every plan row of the §2 table (header and rule rows excluded), backticks kept."""
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in _section().splitlines()
        if line.startswith("| `")
    ]


def table_prices() -> dict[str, dict[str, int | None]]:
    """Plan id -> price column -> micro-dollars as the table states them (``None`` for "custom")."""
    return {
        cells[0].strip("`"): dict(zip(PRICE_COLUMNS, map(_dollars, cells[2:8]), strict=True))
        for cells in table_cells()
    }


def _search(pattern: str, text: str) -> tuple[str, ...]:
    match = re.search(pattern, text)
    assert match is not None, pattern
    return match.groups()


def _per_plan(text: str, plan_ids: list[str]) -> dict[str, dict[str, int]]:
    columns = {column: _search(pattern, text) for column, pattern in _PER_PLAN.items()}
    return {
        plan_id: {column: _integer(numbers[index]) for column, numbers in columns.items()}
        for index, plan_id in enumerate(plan_ids)
    }


def paragraph_values() -> dict[str, dict[str, int]]:
    """Plan id -> price column -> micro-dollars from the "Micro-dollar seed values" paragraph and TTS."""
    text = re.sub(r"\s+", " ", _section())
    shared = {column: _integer(_search(pattern, text)[0]) for column, pattern in _SHARED.items()}
    (tts_dollars_per_char,) = _search(_TTS_PRICE, text)
    tts_micros = Decimal(tts_dollars_per_char) * _MICROS_PER_DOLLAR * _TTS_CHARACTERS
    shared["tts_rate_micros_per_1k_chars"] = int(tts_micros)
    per_plan = _per_plan(text, [cells[0].strip("`") for cells in table_cells()])
    return {plan_id: values | shared for plan_id, values in per_plan.items()}


def _spec_plan(cells: list[str], prices: dict[str, int | None], seeds: dict[str, int]) -> SpecPlan:
    def price(column: str) -> int:
        stated = prices[column]
        return seeds[column] if stated is None else stated

    return SpecPlan(
        id=cells[0].strip("`"),
        name=cells[1],
        monthly_fee_micros=price("monthly_fee_micros"),
        connected_rate_micros_per_min=price("connected_rate_micros_per_min"),
        transfer_rate_micros_per_min=price("transfer_rate_micros_per_min"),
        min_attempt_micros=price("min_attempt_micros"),
        sms_rate_micros=price("sms_rate_micros"),
        widget_message_micros=price("widget_message_micros"),
        tts_rate_micros_per_1k_chars=seeds["tts_rate_micros_per_1k_chars"],
        daily_call_cap=_cap(cells[8]),
        hourly_call_cap=_cap(cells[9]),
        concurrency_cap=_cap(cells[10]),
        voice_clone_cap=_cap(cells[11]),
        features=_features(cells[13]),
    )


def parse_spec_plans() -> dict[str, SpecPlan]:
    """Plan id -> :class:`SpecPlan`; "custom" table cells take the paragraph's seed value."""
    prices, seeds = table_prices(), paragraph_values()
    plans = (
        _spec_plan(cells, prices[cells[0].strip("`")], seeds[cells[0].strip("`")]) for cells in table_cells()
    )
    return {plan.id: plan for plan in plans}
