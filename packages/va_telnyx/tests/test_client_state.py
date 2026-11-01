"""client_state encode/decode and deterministic command ids (specs/06-telephony-telnyx.md §2, §3)."""

from __future__ import annotations

import base64
import uuid

import orjson
import pytest
from telnyx_client_helpers import CALL_ID, ORG_ID

from va_telnyx.client_state import (
    MAX_CLIENT_STATE_CHARS,
    ClientState,
    ClientStateError,
    LegRole,
    command_id,
    decode,
    encode,
)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def test_encode_is_base64_of_compact_json_in_spec_key_order() -> None:
    value = encode(ClientState(call_id=CALL_ID, leg="warm_proxy", org_id=ORG_ID))
    assert base64.b64decode(value) == (
        f'{{"v":1,"call_id":"{CALL_ID}","leg":"warm_proxy","org_id":"{ORG_ID}"}}'.encode()
    )


@pytest.mark.parametrize("leg", ["primary", "transfer_target", "warm_proxy", "monitor"])
def test_round_trip(leg: LegRole) -> None:
    state = ClientState(call_id=uuid.uuid4(), leg=leg, org_id=uuid.uuid4())
    encoded = encode(state)
    assert len(encoded) <= MAX_CLIENT_STATE_CHARS
    assert decode(encoded) == state


@pytest.mark.parametrize(
    "value",
    [
        "not base64!",
        _b64(b"\xff\xfe not json"),
        _b64(b"[1, 2]"),
        _b64(orjson.dumps({"v": 2, "call_id": str(CALL_ID), "leg": "primary", "org_id": str(ORG_ID)})),
        _b64(orjson.dumps({"v": 1, "call_id": str(CALL_ID), "leg": "caller", "org_id": str(ORG_ID)})),
        _b64(orjson.dumps({"v": 1, "call_id": "nope", "leg": "primary", "org_id": str(ORG_ID)})),
        _b64(orjson.dumps({"v": 1, "call_id": str(CALL_ID), "leg": "primary"})),
        _b64(
            orjson.dumps({"v": 1, "call_id": str(CALL_ID), "leg": "primary", "org_id": str(ORG_ID), "x": 1})
        ),
        "A" * (MAX_CLIENT_STATE_CHARS + 4),
    ],
)
def test_foreign_values_are_rejected(value: str) -> None:
    with pytest.raises(ClientStateError):
        decode(value)


def test_client_state_error_is_a_value_error() -> None:
    assert issubclass(ClientStateError, ValueError)


def test_command_id_is_deterministic() -> None:
    assert command_id("hangup", CALL_ID, "primary") == f"hangup-{CALL_ID}-primary"
    assert command_id("transfer", CALL_ID, "primary", 2) == f"transfer-{CALL_ID}-primary-2"
    assert command_id("dial", str(CALL_ID), "warm_proxy") == command_id("dial", CALL_ID, "warm_proxy")
    assert command_id("transfer", CALL_ID, "primary", 1) != command_id("transfer", CALL_ID, "primary", 2)
