"""``client_state`` and ``command_id`` (specs/06-telephony-telnyx.md §2, §3).

``client_state`` is base64 (standard alphabet, padded) of compact JSON
``{"v":1,"call_id":"<uuid>","leg":"primary|transfer_target|warm_proxy|monitor","org_id":"<uuid>"}``;
Telnyx echoes it on later webhooks of the leg. :func:`decode` raises :class:`ClientStateError` for anything
else, and the caller falls back to the ``call_legs`` lookup by ``call_control_id``.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from typing import Final, Literal

import orjson
from pydantic import BaseModel, ConfigDict, ValidationError

__all__ = [
    "MAX_CLIENT_STATE_CHARS",
    "ClientState",
    "ClientStateError",
    "LegRole",
    "command_id",
    "decode",
    "encode",
]

type LegRole = Literal["primary", "transfer_target", "warm_proxy", "monitor"]

MAX_CLIENT_STATE_CHARS: Final = 1024


class ClientStateError(ValueError):
    """A ``client_state`` value that is not ours (bad base64, JSON, version, leg, or too long)."""


class ClientState(BaseModel):
    """The decoded ``client_state`` of one call leg."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    v: Literal[1] = 1
    call_id: uuid.UUID
    leg: LegRole
    org_id: uuid.UUID


def encode(state: ClientState) -> str:
    """Base64 of the compact JSON document (keys in the order ``v``, ``call_id``, ``leg``, ``org_id``)."""
    return base64.b64encode(orjson.dumps(state.model_dump(mode="json"))).decode("ascii")


def decode(value: str) -> ClientState:
    """Parse a ``client_state`` echoed by Telnyx."""
    if len(value) > MAX_CLIENT_STATE_CHARS:
        msg = f"client_state is longer than {MAX_CLIENT_STATE_CHARS} characters"
        raise ClientStateError(msg)
    try:
        raw = base64.b64decode(value, validate=True)
        return ClientState.model_validate(orjson.loads(raw))
    except (binascii.Error, orjson.JSONDecodeError, ValidationError) as exc:
        msg = f"client_state is not a VoiceAgents leg state: {type(exc).__name__}"
        raise ClientStateError(msg) from exc


def command_id(action: str, call_id: uuid.UUID | str, leg: LegRole, *parts: str | int) -> str:
    """Deterministic Telnyx ``command_id``: ``f"{action}-{call_id}-{leg}"`` plus ``-{part}`` per extra
    part, so a retried command (same inputs) is deduplicated by Telnyx and a distinct one (e.g. the n-th
    transfer) is not."""
    return "-".join([action, str(call_id), leg, *(str(part) for part in parts)])
