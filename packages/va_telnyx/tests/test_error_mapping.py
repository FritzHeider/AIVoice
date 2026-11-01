"""Error mapping: CallAlreadyEnded for ended calls, CARRIER_ERROR otherwise (specs/06 §2.2)."""

from __future__ import annotations

import httpx
import pytest
import respx
from telnyx_client_helpers import CCID, rig

from va_telnyx import CallAlreadyEnded, TelnyxApiError
from va_telnyx.transport import Operation

ENDED = {"errors": [{"code": "90018", "title": "Call has already ended", "detail": "gone"}]}
NOT_FOUND = {"errors": [{"code": "10005", "title": "Resource not found"}]}


@pytest.mark.parametrize(("status", "body"), [(422, ENDED), (404, NOT_FOUND)])
async def test_hangup_of_an_ended_call_returns_false(
    telnyx_api: respx.MockRouter, status: int, body: dict[str, object]
) -> None:
    telnyx_api.post(f"/calls/{CCID}/actions/hangup").respond(status, json=body)
    async with httpx.AsyncClient() as http:
        assert await rig(http).client.hangup(CCID, command_id="h") is False


@pytest.mark.parametrize(("status", "body"), [(422, ENDED), (404, NOT_FOUND)])
async def test_other_commands_on_an_ended_call_raise_call_already_ended(
    telnyx_api: respx.MockRouter, status: int, body: dict[str, object]
) -> None:
    telnyx_api.post(f"/calls/{CCID}/actions/record_stop").respond(status, json=body)
    async with httpx.AsyncClient() as http:
        with pytest.raises(CallAlreadyEnded) as caught:
            await rig(http).client.record_stop(CCID, command_id="r", client_state="cw==")
    assert caught.value.status == status
    assert caught.value.operation == "record_stop"


async def test_90018_on_a_conference_command_is_call_already_ended(telnyx_api: respx.MockRouter) -> None:
    telnyx_api.post("/conferences/conf-1/actions/leave").respond(422, json=ENDED)
    async with httpx.AsyncClient() as http:
        with pytest.raises(CallAlreadyEnded):
            await rig(http).client.conference_leave("conf-1", CCID, command_id="l")


async def test_404_outside_call_commands_is_a_carrier_error(telnyx_api: respx.MockRouter) -> None:
    telnyx_api.post("/conferences/conf-1/actions/join").respond(404, json=NOT_FOUND)
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelnyxApiError) as caught:
            await rig(http).client.conference_join(
                "conf-1", CCID, end_conference_on_exit=False, command_id="j", client_state="cw=="
            )
    assert not isinstance(caught.value, CallAlreadyEnded)
    assert caught.value.to_app_error().details == {
        "telnyx_errors": [{"code": "10005", "title": "Resource not found"}]
    }


async def test_422_without_90018_is_a_carrier_error(telnyx_api: respx.MockRouter) -> None:
    telnyx_api.post(f"/calls/{CCID}/actions/hangup").respond(
        422, json={"errors": [{"code": "10015", "title": "Invalid value", "source": {"pointer": "/x"}}]}
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelnyxApiError) as caught:
            await rig(http).client.hangup(CCID, command_id="h")
    assert not isinstance(caught.value, CallAlreadyEnded)


async def test_401_is_a_carrier_error(telnyx_api: respx.MockRouter) -> None:
    telnyx_api.post("/conferences").respond(
        401, json={"errors": [{"code": "10009", "title": "Authentication failed"}]}
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelnyxApiError) as caught:
            await rig(http).client.conference_create(CCID, "va-1", command_id="c", client_state="cw==")
    assert (caught.value.status, caught.value.errors[0].code) == (401, "10009")
    assert caught.value.to_app_error().code == "CARRIER_ERROR"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>ok</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"data": {"unexpected": True}}),
        httpx.Response(200, json={"errors": []}),
    ],
)
async def test_malformed_success_bodies_are_carrier_errors(
    telnyx_api: respx.MockRouter, response: httpx.Response
) -> None:
    telnyx_api.post(f"/calls/{CCID}/actions/record_stop").mock(return_value=response)
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelnyxApiError) as caught:
            await rig(http).client.record_stop(CCID, command_id="r", client_state="cw==")
    assert caught.value.errors[0].code == "invalid_response"
    assert caught.value.to_app_error().code == "CARRIER_ERROR"


async def test_empty_success_body_is_an_empty_document(telnyx_api: respx.MockRouter) -> None:
    telnyx_api.delete("/phone_numbers/1").respond(204)
    async with httpx.AsyncClient() as http:
        operation = Operation("delete_phone_number", "DELETE", None, idempotent=True)
        result = await rig(http).client._transport.request(operation, "/phone_numbers/1")
    assert result == {}
