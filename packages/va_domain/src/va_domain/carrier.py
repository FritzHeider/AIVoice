"""Carrier events shared by the telephony edge, the media worker, and ``CallWorkflow``.

Specs: specs/06-telephony-telnyx.md §5 Decision P05-T02; specs/07-media-pipeline.md §17; specs/03 §11.

A :class:`CarrierEvent` is the message the edge publishes on ``call:{<id>}:carrier`` and the argument of the
Temporal signal ``CallWorkflow.signal_carrier_event``. Per-call Redis keys use the hash-tag form
``call:{<uuid>}:...`` so every key of one call maps to the same cluster slot.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

__all__ = [
    "CARRIER_SIGNAL",
    "CARRIER_STATE_TTL_S",
    "CarrierEvent",
    "CarrierLeg",
    "call_workflow_id",
    "carrier_channel",
    "carrier_state_key",
    "owner_key",
]

type CarrierLeg = Literal["primary", "transfer_target", "warm_proxy", "monitor"]

CARRIER_SIGNAL: Final = "signal_carrier_event"
"""Name of the ``CallWorkflow`` signal that receives :class:`CarrierEvent` values."""

CARRIER_STATE_TTL_S: Final = 7200
"""TTL of ``call:{<id>}:carrier_state`` (2 h, specs/03 §11), refreshed on every write."""


class CarrierEvent(BaseModel):
    """One Telnyx voice event of a resolved call: ``type`` is the Telnyx ``event_type``, ``payload`` the raw
    webhook ``data.payload``; ``telnyx_event_id`` lets consumers drop redeliveries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    leg: CarrierLeg
    call_id: uuid.UUID
    org_id: uuid.UUID
    telnyx_event_id: str
    occurred_at: datetime
    payload: dict[str, JsonValue] = Field(default_factory=dict)


def call_workflow_id(call_id: uuid.UUID) -> str:
    """``call-<id>``: the workflow id of the call's ``CallWorkflow`` (specs/05 §5)."""
    return f"call-{call_id}"


def carrier_state_key(call_id: uuid.UUID) -> str:
    """``call:{<id>}:carrier_state``: hash of the latest carrier state (answered, amd_result, ...)."""
    return f"call:{{{call_id}}}:carrier_state"


def carrier_channel(call_id: uuid.UUID) -> str:
    """``call:{<id>}:carrier``: pub/sub channel of carrier events to the owning media worker."""
    return f"call:{{{call_id}}}:carrier"


def owner_key(call_id: uuid.UUID) -> str:
    """``call:{<id>}:owner``: the media worker ownership lock (value = pod id)."""
    return f"call:{{{call_id}}}:owner"
