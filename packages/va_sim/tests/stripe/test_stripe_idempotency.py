"""FakeStripe ``Idempotency-Key`` handling (Decision P03-T07a rule 4)."""

from __future__ import annotations

import asyncio

import pytest
from stripe_helpers import events_of, post, stripe_client, stripe_state
from telnyx_helpers import RecordingTransport

from va_sim.stripe import payment_intents, routes
from va_sim.stripe.http import Handler, StripeCall


def patch_create(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    """Serve ``POST /v1/payment_intents`` with ``handler`` (routes built afterwards pick it up)."""
    original = payment_intents.create_payment_intent
    api = tuple((path, method, handler if h is original else h) for path, method, h in routes._API)
    monkeypatch.setattr(routes, "_API", api)


async def test_a_replay_returns_the_stored_answer_and_creates_nothing() -> None:
    hooks = RecordingTransport()
    state = stripe_state(hooks)
    async with stripe_client(state) as client:
        params = {"amount": 2000, "currency": "usd", "payment_method": "pm_card_visa", "confirm": True}
        first = await post(client, "/v1/payment_intents", params, idempotency_key="ar:o1:2026110112:1")
        replay = await post(client, "/v1/payment_intents", params, idempotency_key="ar:o1:2026110112:1")
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers["idempotent-replayed"] == "true"
    assert replay.headers["original-request"] == first.headers["request-id"]
    assert "idempotent-replayed" not in first.headers
    assert len(state.stripe.payment_intents) == 1
    [event] = events_of(hooks)
    assert event["request"] == {"id": first.headers["request-id"], "idempotency_key": "ar:o1:2026110112:1"}


async def test_the_same_key_with_other_parameters_is_an_idempotency_error() -> None:
    async with stripe_client(stripe_state()) as client:
        await post(client, "/v1/customers", {"name": "a"}, idempotency_key="k1")
        mismatch = await post(client, "/v1/customers", {"name": "b"}, idempotency_key="k1")
        other_path = await post(client, "/v1/payment_intents", {"name": "a"}, idempotency_key="k1")
    for response in (mismatch, other_path):
        assert response.status_code == 400
        assert response.json()["error"]["type"] == "idempotency_error"
        assert "'k1'" in response.json()["error"]["message"]


async def test_keys_are_scoped_per_api_key_and_errors_are_replayed() -> None:
    state = stripe_state()
    async with stripe_client(state) as client:
        await client.post("/sim/stripe/api-keys", json={"api_key": "sk_test_second"})
        first = await post(client, "/v1/customers", {"name": "a"}, idempotency_key="shared")
        second = await post(
            client,
            "/v1/customers",
            {"name": "a"},
            idempotency_key="shared",
            authorization="Bearer sk_test_second",
        )
        error = await post(
            client, "/v1/payment_intents", {"amount": 10, "currency": "usd"}, idempotency_key="e"
        )
        error_replay = await post(
            client, "/v1/payment_intents", {"amount": 10, "currency": "usd"}, idempotency_key="e"
        )
        get_ignores_key = await client.get("/v1/events", headers={"idempotency-key": "shared"})
    assert first.json()["id"] != second.json()["id"]
    assert len(state.stripe.customers) == 2
    assert error.status_code == error_replay.status_code == 400
    assert error_replay.json() == error.json()
    assert error_replay.headers["idempotent-replayed"] == "true"
    assert get_ignores_key.status_code == 200


async def test_a_concurrent_request_with_the_same_key_is_409(monkeypatch: pytest.MonkeyPatch) -> None:
    release = asyncio.Event()
    entered = asyncio.Event()
    original = payment_intents.create_payment_intent

    async def slow(call: StripeCall) -> dict[str, object]:
        entered.set()
        await release.wait()
        return await original(call)

    patch_create(monkeypatch, slow)
    async with stripe_client(stripe_state()) as client:
        params = {"amount": 100, "currency": "usd"}
        first = asyncio.ensure_future(post(client, "/v1/payment_intents", params, idempotency_key="busy"))
        await entered.wait()
        busy = await post(client, "/v1/payment_intents", params, idempotency_key="busy")
        release.set()
        done = await first
    assert busy.status_code == 409
    assert busy.json()["error"]["code"] == "idempotency_key_in_use"
    assert done.status_code == 200


async def test_a_crashing_handler_releases_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0
    original = payment_intents.create_payment_intent

    async def flaky(call: StripeCall) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            msg = "boom"
            raise RuntimeError(msg)
        return await original(call)

    patch_create(monkeypatch, flaky)
    state = stripe_state()
    async with stripe_client(state) as client:
        params = {"amount": 100, "currency": "usd"}
        try:
            await post(client, "/v1/payment_intents", params, idempotency_key="crash")
        except RuntimeError:
            crashed = True
        else:
            crashed = False
        retried = await post(client, "/v1/payment_intents", params, idempotency_key="crash")
    assert crashed
    assert retried.status_code == 200
