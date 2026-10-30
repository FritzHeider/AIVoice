"""FakeTelnyx number inventory (specs/30-testing-and-evals.md §3.2 ``GET /v2/available_phone_numbers``).

A fixed, deterministic inventory: 50 numbers in each of ``+1415555`` (San Francisco), ``+1212555`` (New York),
``+1888555`` (toll-free), and ``+44 20 7946`` (London). Numbers that are reserved by a pending order or owned
by any account are excluded from search results.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final, Literal

__all__ = ["NUMBER_TYPES", "AvailableNumber", "NumberFilters", "search", "seed_inventory", "to_json"]

type NumberType = Literal["local", "toll_free", "mobile", "national"]
type Feature = Literal["voice", "sms", "mms"]

NUMBER_TYPES: Final[frozenset[str]] = frozenset({"local", "toll_free", "mobile", "national"})
_BLOCK_SIZE: Final = 50


@dataclass(frozen=True, slots=True)
class AvailableNumber:
    phone_number: str
    country_code: str
    ndc: str
    phone_number_type: NumberType
    locality: str | None
    administrative_area: str | None
    features: tuple[Feature, ...]
    monthly_cost: str
    upfront_cost: str


@dataclass(frozen=True, slots=True)
class _Block:
    prefix: str
    first_suffix: int
    suffix_digits: int
    country_code: str
    ndc: str
    phone_number_type: NumberType
    locality: str | None
    administrative_area: str | None
    features: tuple[Feature, ...]
    monthly_cost: str


_BLOCKS: Final = (
    _Block("+1415555", 2000, 4, "US", "415", "local", "San Francisco", "CA", ("voice", "sms", "mms"), "1.00"),
    _Block("+1212555", 2000, 4, "US", "212", "local", "New York", "NY", ("voice", "sms", "mms"), "1.00"),
    _Block("+1888555", 2000, 4, "US", "888", "toll_free", None, None, ("voice", "sms"), "2.00"),
    _Block("+4420794", 60000, 5, "GB", "20", "national", "London", None, ("voice",), "1.50"),
)


def seed_inventory() -> tuple[AvailableNumber, ...]:
    """Every number FakeTelnyx can sell, in a stable order."""
    return tuple(
        AvailableNumber(
            phone_number=f"{block.prefix}{block.first_suffix + offset:0{block.suffix_digits}d}",
            country_code=block.country_code,
            ndc=block.ndc,
            phone_number_type=block.phone_number_type,
            locality=block.locality,
            administrative_area=block.administrative_area,
            features=block.features,
            monthly_cost=block.monthly_cost,
            upfront_cost="1.00",
        )
        for block in _BLOCKS
        for offset in range(_BLOCK_SIZE)
    )


@dataclass(frozen=True, slots=True)
class NumberFilters:
    """The ``filter[...]`` query of a number search (already validated)."""

    country_code: str | None = None
    national_destination_code: str | None = None
    locality: str | None = None
    administrative_area: str | None = None
    contains: str | None = None
    features: tuple[str, ...] = ()
    phone_number_type: str | None = None
    limit: int = 10


def _matches(number: AvailableNumber, filters: NumberFilters) -> bool:
    checks = (
        filters.country_code is None or number.country_code == filters.country_code.upper(),
        filters.national_destination_code is None or number.ndc == filters.national_destination_code,
        filters.locality is None or (number.locality or "").casefold() == filters.locality.casefold(),
        filters.administrative_area is None
        or (number.administrative_area or "").casefold() == filters.administrative_area.casefold(),
        filters.contains is None or filters.contains.lstrip("+") in number.phone_number[1:],
        all(feature in number.features for feature in filters.features),
        filters.phone_number_type is None or number.phone_number_type == filters.phone_number_type,
    )
    return all(checks)


def search(
    numbers: Iterable[AvailableNumber], filters: NumberFilters, taken: set[str]
) -> list[AvailableNumber]:
    """Up to ``filters.limit`` numbers matching ``filters`` that are not in ``taken``."""
    found: list[AvailableNumber] = []
    for number in numbers:
        if number.phone_number not in taken and _matches(number, filters):
            found.append(number)
            if len(found) == filters.limit:
                break
    return found


def _regions(number: AvailableNumber) -> Sequence[dict[str, str]]:
    regions = [{"region_type": "country_code", "region_name": number.country_code}]
    if number.administrative_area is not None:
        regions.append({"region_type": "state", "region_name": number.administrative_area})
    if number.locality is not None:
        regions.append({"region_type": "location", "region_name": number.locality})
        regions.append({"region_type": "rate_center", "region_name": number.locality.upper()})
    return regions


def to_json(number: AvailableNumber) -> dict[str, object]:
    """The ``available_phone_number`` record of ``number``."""
    return {
        "record_type": "available_phone_number",
        "phone_number": number.phone_number,
        "vanity_format": "",
        "best_effort": False,
        "quickship": True,
        "reservable": True,
        "features": [{"name": feature} for feature in number.features],
        "cost_information": {
            "monthly_cost": number.monthly_cost,
            "upfront_cost": number.upfront_cost,
            "currency": "USD",
        },
        "region_information": list(_regions(number)),
    }
