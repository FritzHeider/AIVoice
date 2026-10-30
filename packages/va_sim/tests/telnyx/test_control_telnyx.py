"""FakeTelnyx control routes: public key, config, and their absence on the other simulators."""

from __future__ import annotations

import httpx
from starlette.applications import Starlette
from telnyx_helpers import telnyx_client, telnyx_state

from va_sim.server import simulator_routes


async def test_public_key_matches_the_state_key() -> None:
    state = telnyx_state()
    async with telnyx_client(state) as client:
        response = await client.get("/sim/telnyx/public-key")
    assert response.json() == {"public_key": state.signing_key.public_key_b64}


async def test_config_read_patch_validation_and_reset() -> None:
    async with telnyx_client(telnyx_state()) as client:
        initial = await client.get("/sim/telnyx/config")
        patched = await client.patch("/sim/telnyx/config", json={"force_duplicate_webhooks": True})
        partial = await client.patch(
            "/sim/telnyx/config", json={"failing_message_destinations": ["+14155550123"]}
        )
        unknown = await client.patch("/sim/telnyx/config", json={"drop_everything": True})
        bad_number = await client.patch("/sim/telnyx/config", json={"failing_message_destinations": ["123"]})
        await client.post("/sim/reset")
        after_reset = await client.get("/sim/telnyx/config")
    defaults = {
        "force_duplicate_webhooks": False,
        "fail_number_orders": False,
        "failing_message_destinations": [],
    }
    assert initial.json() == defaults
    assert patched.json() == {**defaults, "force_duplicate_webhooks": True}
    assert partial.json() == {
        **defaults,
        "force_duplicate_webhooks": True,
        "failing_message_destinations": ["+14155550123"],
    }
    assert unknown.status_code == 422
    assert unknown.json()["error"]["code"] == "invalid_request"
    assert unknown.json()["error"]["issues"][0]["path"] == "drop_everything"
    assert bad_number.status_code == 422
    assert after_reset.json() == defaults


async def test_state_snapshot_counts_telnyx_resources() -> None:
    state = telnyx_state()
    async with telnyx_client(state) as client:
        await client.post("/sim/telnyx/accounts", json={"api_key": "byo"})
        snapshot = await client.get("/sim/state")
    assert snapshot.json()["telnyx"] == {
        "accounts": 2,
        "messages": 0,
        "calls": 0,
        "conferences": 0,
        "phone_numbers": 0,
        "webhook_deliveries": 0,
    }


async def test_telnyx_routes_are_absent_on_other_simulators() -> None:
    state = telnyx_state()
    app = Starlette(routes=simulator_routes("llm", state))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sim") as client:
        public_key = await client.get("/sim/telnyx/public-key")
        balance = await client.get("/v2/balance", headers={"authorization": "Bearer sim-telnyx-key"})
    assert (public_key.status_code, balance.status_code) == (404, 404)
    await state.aclose()
