"""Request and response bodies of the Bland-compatible ``/v1/orgs`` sub-account routes (specs/04 §8.2).

These are legacy bodies: success responses carry ``"status": "success"`` at the top level next to their
fields, and the create body ignores unknown fields like Bland does.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, field_validator

from va_common.money import micros_to_dollars
from va_db.models import Org, OrgStatus

__all__ = [
    "MAX_INITIAL_CREDITS",
    "MONEY_DECIMALS",
    "SubAccountCreate",
    "SubAccountCreated",
    "SubAccountItem",
    "SubAccountList",
    "SubAccountPlan",
    "SubAccountResult",
    "sub_account_item",
]

MAX_INITIAL_CREDITS: Final = 1_000_000
MONEY_DECIMALS: Final = 6

SubAccountPlan = Literal["start", "build", "scale"]

_ITEM_EXAMPLE: Final[dict[str, JsonValue]] = {
    "org_id": "0192f3a4-b5c6-7d8e-9f0a-1b2c3d4e5f60",
    "org_display_name": "Acme Dental - Austin",
    "org_plan": "build",
    "kyc_level": 1,
    "credits": 10.0,
    "status": "active",
    "created_at": "2026-10-29T14:03:11Z",
}
_CREATED_EXAMPLE: Final[dict[str, JsonValue]] = {
    "status": "success",
    "org_id": _ITEM_EXAMPLE["org_id"],
    "org_display_name": _ITEM_EXAMPLE["org_display_name"],
    "org_plan": "build",
    "kyc_level": 1,
    "api_key": "va_live_3kQ9xT2mLpR7vB1nZc8dHf4sWq6yJa0e",
    "credits": 10.0,
    "created_at": _ITEM_EXAMPLE["created_at"],
}

DisplayName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class SubAccountCreate(BaseModel):
    """``POST /v1/orgs/create``: the sub-account's name, optional plan, and credits moved from the parent."""

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "examples": [
                {"org_display_name": "Acme Dental - Austin", "org_plan": "build", "initial_credits": 10}
            ]
        },
    )

    org_display_name: DisplayName = Field(description="Name of the sub-account (1-200 characters).")
    org_plan: SubAccountPlan | None = Field(
        default=None, description="Plan of the sub-account; default: the parent's."
    )
    initial_credits: float | None = Field(
        default=None,
        ge=0,
        le=MAX_INITIAL_CREDITS,
        allow_inf_nan=False,
        description="Dollars moved from the parent's available balance (at most 6 decimals).",
    )

    @field_validator("initial_credits")
    @classmethod
    def _six_decimals(cls, value: float | None) -> float | None:
        exponent = Decimal(str(value)).as_tuple().exponent if value is not None else 0
        if isinstance(exponent, int) and exponent < -MONEY_DECIMALS:
            msg = f"must have at most {MONEY_DECIMALS} decimal places"
            raise ValueError(msg)
        return value


class SubAccountItem(BaseModel):
    """A sub-account of the calling org."""

    model_config = ConfigDict(json_schema_extra={"examples": [_ITEM_EXAMPLE]})

    org_id: uuid.UUID
    org_display_name: str
    org_plan: str
    kyc_level: int
    credits: float = Field(description="Credit balance in dollars.")
    status: OrgStatus
    created_at: datetime


def sub_account_item(org: Org) -> SubAccountItem:
    """The response item of an ``orgs`` row."""
    return SubAccountItem(
        org_id=org.id,
        org_display_name=org.name,
        org_plan=org.plan_id,
        kyc_level=org.kyc_level,
        credits=float(micros_to_dollars(org.credits_balance_micros)),
        status=org.status,
        created_at=org.created_at,
    )


class SubAccountCreated(BaseModel):
    """``POST /v1/orgs/create`` result; ``api_key`` is the sub-account's key, shown only in this response."""

    model_config = ConfigDict(json_schema_extra={"examples": [_CREATED_EXAMPLE]})

    status: Literal["success"] = "success"
    org_id: uuid.UUID
    org_display_name: str
    org_plan: str
    kyc_level: int
    api_key: str | None = Field(
        description=(
            "API key with every scope for the sub-account, shown only in this response; store it now. "
            "null on an Idempotency-Key replay: the key is never stored, so a lost key must be rolled."
        )
    )
    credits: float = Field(description="Credit balance of the sub-account in dollars.")
    created_at: datetime


class SubAccountList(BaseModel):
    """``GET /v1/orgs`` result: every direct sub-account, oldest first."""

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"status": "success", "orgs": [_ITEM_EXAMPLE]}]}
    )

    status: Literal["success"] = "success"
    orgs: list[SubAccountItem]


class SubAccountResult(BaseModel):
    """One sub-account (``GET /v1/orgs/{org_id}``, ``suspend``, ``reactivate``)."""

    model_config = ConfigDict(json_schema_extra={"examples": [{"status": "success", "org": _ITEM_EXAMPLE}]})

    status: Literal["success"] = "success"
    org: SubAccountItem
