"""``delete_recording`` (specs/06-telephony-telnyx.md §2.1; §11 Decision P05-T14): path, idempotent 404,
errors, and the contract against FakeTelnyx."""

from __future__ import annotations

import httpx
import pytest
import respx
from telnyx_client_helpers import rig, sim_clients
from telnyx_helpers import telnyx_state

from va_telnyx import TelnyxApiError

_NOT_FOUND = {"errors": [{"code": "10005", "title": "Resource not found"}]}


async def test_deletes_by_id_with_an_empty_body(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.delete("/recordings/rec%2F1").respond(
        200, json={"data": {"id": "rec/1", "record_type": "recording", "status": "deleted"}}
    )
    async with httpx.AsyncClient() as http:
        assert await rig(http).client.delete_recording("rec/1") is True
    request = route.calls[0].request
    assert request.content == b""
    assert request.headers["authorization"].startswith("Bearer ")


async def test_a_missing_recording_is_false(telnyx_api: respx.MockRouter) -> None:
    telnyx_api.delete("/recordings/gone").respond(404, json=_NOT_FOUND)
    async with httpx.AsyncClient() as http:
        assert await rig(http).client.delete_recording("gone") is False


async def test_other_errors_propagate(telnyx_api: respx.MockRouter) -> None:
    telnyx_api.delete("/recordings/r").respond(
        403, json={"errors": [{"code": "10010", "title": "Forbidden"}]}
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelnyxApiError) as caught:
            await rig(http).client.delete_recording("r")
    assert caught.value.status == 403


async def test_contract_delete_against_fake_telnyx() -> None:
    state = telnyx_state()
    state.store_recording("rec-contract", b"RIFF")
    async with sim_clients(state) as (raw, client):
        assert await client.delete_recording("rec-contract") is True
        assert (await raw.get("/sim/recordings/rec-contract.wav")).status_code == 404
        assert await client.delete_recording("rec-contract") is False
