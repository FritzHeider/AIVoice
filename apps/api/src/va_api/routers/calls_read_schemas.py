"""Query and response models of ``GET /v1/calls`` (specs/05-calls-api.md §6; Decision P05-T16a; legacy
envelope). ``GET /v1/calls/{call_id}`` answers the bare :class:`va_domain.call_object.CallObject`."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from va_domain.call_object import CallListItem

__all__ = ["MAX_CALL_LIST_LIMIT", "CallListQuery", "CallListResponse"]

MAX_CALL_LIST_LIMIT: Final = 1000
"""Largest (and default) page size, as in Bland."""


class CallListQuery(BaseModel):
    """Every specs/05 §6 filter; unknown query parameters are ignored (Bland parity)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    to_number: str | None = Field(default=None, description="Exact callee number (E.164).")
    to: str | None = Field(default=None, description="Alias of `to_number`.")
    from_number: str | None = Field(default=None, description="Exact caller id (E.164).")
    from_: str | None = Field(default=None, alias="from", description="Alias of `from_number`.")
    limit: int = Field(default=MAX_CALL_LIST_LIMIT, ge=1, le=MAX_CALL_LIST_LIMIT, description="Page size.")
    ascending: bool = Field(default=False, description="Oldest first instead of newest first.")
    start_date: date | None = Field(default=None, description="First creation day, in the org timezone.")
    end_date: date | None = Field(default=None, description="Last creation day (inclusive), org timezone.")
    created_at_gte: datetime | None = Field(default=None, description="Created at or after (naive = UTC).")
    created_at_lte: datetime | None = Field(default=None, description="Created at or before (naive = UTC).")
    batch_id: uuid.UUID | None = Field(default=None, description="Batch of the call.")
    campaign_id: uuid.UUID | None = Field(default=None, description="Alias of `batch_id`.")
    pathway_id: uuid.UUID | None = Field(default=None, description="Pathway run by the call.")
    persona_id: uuid.UUID | None = Field(default=None, description="Persona run by the call.")
    agent_id: uuid.UUID | None = Field(default=None, description="Agent run by the call.")
    completed: bool | None = Field(default=None, description="Post-call processing finished (or not).")
    answered_by: Literal["human", "voicemail", "ivr", "unknown", "no-answer", "fax"] | None = Field(
        default=None, description="Who answered."
    )
    inbound: bool | None = Field(default=None, description="Inbound calls only (`true`) or all others.")
    status: str | None = Field(
        default=None, description="Comma-separated call statuses, Bland (`no-answer`) or internal names."
    )
    disposition_tag: str | None = Field(default=None, description="Disposition tag.")
    duration_gt: float | None = Field(default=None, description="Connected length above, in minutes.")
    duration_lt: float | None = Field(default=None, description="Connected length below, in minutes.")
    external_id: str | None = Field(default=None, description="External id.")
    cursor: str | None = Field(default=None, description="Cursor of the next page (`total_count` omitted).")


def _is_none(value: object) -> bool:
    return value is None


_LIST_EXAMPLE: Final[dict[str, JsonValue]] = {
    "status": "success",
    "total_count": 1532,
    "count": 1,
    "next_cursor": "MjAyNi0xMC0yNFQxODoyMDowMS4wMDAwMDArMDA6MDB8MDE5M2M1ZDItN2IxZS03YTEw",
    "calls": [CallListItem.model_json_schema()["examples"][0]],
}


class CallListResponse(BaseModel):
    """One page of calls; `total_count` (all matching calls) only on the first page (no `cursor`)."""

    model_config = ConfigDict(json_schema_extra={"examples": [_LIST_EXAMPLE]})

    status: Literal["success"] = "success"
    total_count: int | None = Field(default=None, exclude_if=_is_none)
    count: int
    next_cursor: str | None
    calls: list[CallListItem]
