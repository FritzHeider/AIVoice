"""FakeTelnyx Bearer auth and per-account isolation."""

from __future__ import annotations

import pytest
from telnyx_helpers import create_app, error_of, telnyx_client, telnyx_state
from telnyx_shapes import Balance, Errors, assert_shape


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": "Bearer wrong-key"},
        {"authorization": "Basic sim-telnyx-key"},
        {"authorization": ""},
    ],
)
async def test_missing_or_wrong_credentials_get_10009(headers: dict[str, str]) -> None:
    async with telnyx_client(telnyx_state()) as client:
        client.headers.pop("authorization")
        response = await client.get("/v2/balance", headers=headers)
    assert error_of(response) == (401, "10009", None)
    assert_shape(Errors, response.json())


async def test_built_in_key_and_case_insensitive_scheme() -> None:
    async with telnyx_client(telnyx_state()) as client:
        response = await client.get("/v2/balance", headers={"authorization": "bearer sim-telnyx-key"})
    assert response.status_code == 200
    balance = assert_shape(Balance, response.json()["data"])
    assert balance.available_credit == "100.00"


async def test_registered_accounts_are_isolated() -> None:
    state = telnyx_state()
    async with telnyx_client(state) as client:
        registered = await client.post("/sim/telnyx/accounts", json={"api_key": "byo-key"})
        again = await client.post("/sim/telnyx/accounts", json={"api_key": "byo-key"})
        app = await create_app(client)
        byo = {"authorization": "Bearer byo-key"}
        other = await client.get(f"/v2/call_control_applications/{app['id']}", headers=byo)
        listed = await client.get("/v2/call_control_applications", headers=byo)
        own = await client.get(f"/v2/call_control_applications/{app['id']}")
        await client.post("/sim/reset")
        after_reset = await client.get("/v2/balance", headers=byo)
    assert (registered.status_code, registered.json()) == (201, {"api_key": "byo-key"})
    assert again.status_code == 201
    assert error_of(other) == (404, "10005", None)
    assert listed.json()["data"] == []
    assert own.json()["data"]["id"] == app["id"]
    assert error_of(after_reset) == (401, "10009", None)


async def test_account_registration_validates_the_body() -> None:
    async with telnyx_client(telnyx_state()) as client:
        empty = await client.post("/sim/telnyx/accounts", json={"api_key": ""})
        missing = await client.post("/sim/telnyx/accounts", content=b"")
    assert empty.status_code == 422
    assert empty.json()["error"]["issues"][0]["path"] == "api_key"
    assert missing.status_code == 422
