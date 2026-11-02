"""Control channel messages to the media worker that owns a call (specs/07-media-pipeline.md §17).

The API and the workflow publish one JSON message per command on ``call:{<id>}:control``; the owning media
worker handles them in arrival order and, after handling one, writes ``SET call:{<id>}:control_ack:<message
id> 1 EX 30`` so the publisher can tell "done" from "queued". Messages are discriminated on ``type``; unknown
types or fields are rejected (:class:`ControlMessageError`).
"""

from __future__ import annotations

import uuid
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError

__all__ = [
    "CONTROL_ACK_TTL_S",
    "CONTROL_TYPES",
    "ControlMessage",
    "ControlMessageError",
    "CreditsExhaustedControl",
    "DrainNoticeControl",
    "InjectContextControl",
    "SayControl",
    "StopControl",
    "TakeoverControl",
    "TransferControl",
    "control_ack_key",
    "control_channel",
    "parse_control",
]

CONTROL_ACK_TTL_S: Final = 30
"""TTL of ``call:{<id>}:control_ack:<message id>`` (specs/07 §17)."""

type _Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

_MESSAGE = ConfigDict(frozen=True, extra="forbid")


class StopControl(BaseModel):
    """End the call (``POST /v1/calls/{id}/stop``)."""

    model_config = _MESSAGE

    id: uuid.UUID
    type: Literal["stop"] = "stop"
    reason: str = "api"


class SayControl(BaseModel):
    """Speak ``text`` now."""

    model_config = _MESSAGE

    id: uuid.UUID
    type: Literal["say"] = "say"
    text: _Text


class InjectContextControl(BaseModel):
    """Add ``text`` to the conversation context without speaking it."""

    model_config = _MESSAGE

    id: uuid.UUID
    type: Literal["inject_context"] = "inject_context"
    text: _Text


class TransferControl(BaseModel):
    """Transfer the call to ``phone_number``."""

    model_config = _MESSAGE

    id: uuid.UUID
    type: Literal["transfer"] = "transfer"
    phone_number: _Text
    mode: Literal["cold", "warm"] = "cold"


class TakeoverControl(BaseModel):
    """Cold transfer to ``phone_number`` without a spoken line."""

    model_config = _MESSAGE

    id: uuid.UUID
    type: Literal["takeover"] = "takeover"
    phone_number: _Text


class CreditsExhaustedControl(BaseModel):
    """The org ran out of credit: speak the ``max_duration_message`` variant and end (specs/24 §6)."""

    model_config = _MESSAGE

    id: uuid.UUID
    type: Literal["credits_exhausted"] = "credits_exhausted"


class DrainNoticeControl(BaseModel):
    """The pod is draining (engine logs only, specs/07 §19)."""

    model_config = _MESSAGE

    id: uuid.UUID
    type: Literal["drain_notice"] = "drain_notice"


type ControlMessage = Annotated[
    StopControl
    | SayControl
    | InjectContextControl
    | TransferControl
    | TakeoverControl
    | CreditsExhaustedControl
    | DrainNoticeControl,
    Field(discriminator="type"),
]

CONTROL_TYPES: Final = (
    "stop",
    "say",
    "inject_context",
    "transfer",
    "takeover",
    "credits_exhausted",
    "drain_notice",
)

_ADAPTER: Final[TypeAdapter[ControlMessage]] = TypeAdapter(ControlMessage)


class ControlMessageError(ValueError):
    """A control message that is not valid JSON or matches no message type (``errors`` = validation count)."""

    def __init__(self, message: str, *, errors: int = 1) -> None:
        super().__init__(message)
        self.errors = errors


def parse_control(raw: str | bytes) -> ControlMessage:
    """The typed message of ``raw``; raises :class:`ControlMessageError` (the text is never echoed)."""
    try:
        return _ADAPTER.validate_json(raw)
    except ValidationError as exc:
        msg = f"invalid control message ({exc.error_count()} errors)"
        raise ControlMessageError(msg, errors=exc.error_count()) from None


def control_channel(call_id: uuid.UUID) -> str:
    """``call:{<id>}:control``: pub/sub channel of commands to the owning media worker."""
    return f"call:{{{call_id}}}:control"


def control_ack_key(call_id: uuid.UUID, message_id: uuid.UUID) -> str:
    """``call:{<id>}:control_ack:<message id>``: set by the owner after handling the message."""
    return f"call:{{{call_id}}}:control_ack:{message_id}"
