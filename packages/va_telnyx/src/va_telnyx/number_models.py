"""Telnyx number search, order, and phone-number models (specs/06-telephony-telnyx.md §2.1 number rows).

:class:`NumberSearch` validates the search before any request (Decision 2026-11-01 P03-T08b rule 3) and
renders the ``filter[...]`` query; responses keep the fields the platform reads.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from va_telnyx.models import ResponseModel

__all__ = [
    "MAX_SEARCH_LIMIT",
    "AvailableNumber",
    "NumberFeature",
    "NumberOrder",
    "NumberSearch",
    "OrderedNumber",
    "PhoneNumber",
    "PhoneNumberMessaging",
]

MAX_SEARCH_LIMIT: Final = 50

type NumberFeature = Literal["voice", "sms", "mms"]
type NumberType = Literal["local", "toll_free", "mobile", "national"]
type Filter = Annotated[str, StringConstraints(min_length=1)]


class NumberSearch(BaseModel):
    """``GET /available_phone_numbers`` filters; unset filters are not sent."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    country_code: Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")] = "US"
    national_destination_code: Filter | None = None
    locality: Filter | None = None
    administrative_area: Filter | None = None
    contains: Filter | None = None
    features: tuple[NumberFeature, ...] = ()
    phone_number_type: NumberType | None = None
    limit: Annotated[int, Field(ge=1, le=MAX_SEARCH_LIMIT)] = 10

    def params(self) -> dict[str, str | list[str]]:
        """The query: ``filter[...]`` per set field, features repeated, ``filter[best_effort]=false``."""
        optional = {
            "filter[national_destination_code]": self.national_destination_code,
            "filter[locality]": self.locality,
            "filter[administrative_area]": self.administrative_area,
            "filter[phone_number][contains]": self.contains,
            "filter[phone_number_type]": self.phone_number_type,
        }
        params: dict[str, str | list[str]] = {"filter[country_code]": self.country_code}
        params.update({name: value for name, value in optional.items() if value is not None})
        if self.features:
            params["filter[features][]"] = list(self.features)
        params["filter[limit]"] = str(self.limit)
        params["filter[best_effort]"] = "false"
        return params


def _named(value: Any) -> Any:
    """``[{"name": "voice"}, ...]`` as ``("voice", ...)``."""
    if isinstance(value, list):
        return tuple(item.get("name") if isinstance(item, dict) else item for item in value)
    return value


class AvailableNumber(ResponseModel):
    """One ``available_phone_number``; costs come from ``cost_information``."""

    phone_number: str
    features: tuple[str, ...] = ()
    monthly_cost: Decimal | None = None
    upfront_cost: Decimal | None = None
    currency: str | None = None
    region_information: tuple[dict[str, Any], ...] = ()
    best_effort: bool = False
    reservable: bool = False

    _features = field_validator("features", mode="before")(_named)

    @model_validator(mode="before")
    @classmethod
    def _flatten_costs(cls, raw: Any) -> Any:
        """Lift ``cost_information.{monthly_cost, upfront_cost, currency}`` to the top level."""
        costs = raw.get("cost_information") if isinstance(raw, dict) else None
        if not isinstance(costs, dict):
            return raw
        keys = ("monthly_cost", "upfront_cost", "currency")
        return {**raw, **{key: costs[key] for key in keys if key in costs}}


class OrderedNumber(ResponseModel):
    id: str
    phone_number: str
    status: str


class NumberOrder(ResponseModel):
    """A number order: ``pending`` until Telnyx finishes it (``success`` or ``failure``)."""

    id: str
    status: Literal["pending", "success", "failure"]
    phone_numbers: tuple[OrderedNumber, ...] = ()
    connection_id: str | None = None
    messaging_profile_id: str | None = None
    customer_reference: str | None = None


class PhoneNumber(ResponseModel):
    """An owned phone number."""

    id: str
    phone_number: str
    status: str
    connection_id: str | None = None
    messaging_profile_id: str | None = None
    tags: tuple[str, ...] = ()
    customer_reference: str | None = None
    phone_number_type: str | None = None


class PhoneNumberMessaging(ResponseModel):
    """``data`` of ``PATCH /phone_numbers/{id}/messaging``."""

    id: str
    phone_number: str
    messaging_profile_id: str | None = None
