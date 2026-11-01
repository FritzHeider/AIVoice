"""FakeStripe authentication and the request pipeline (Decision P03-T07a rules 1-3)."""

from __future__ import annotations

import base64

import pytest
from stripe_helpers import API_KEY, post, stripe_client, stripe_state

from va_sim.stripe.objects import DEFAULT_API_VERSION


async def test_bearer_and_basic_keys_are_accepted() -> None:
    async with stripe_client(stripe_state(), key=None) as client:
        bearer = await post(client, "/v1/customers", authorization=f"Bearer {API_KEY}")
        basic_token = base64.b64encode(f"{API_KEY}:".encode()).decode()
        basic = await post(client, "/v1/customers", authorization=f"Basic {basic_token}")
    assert (bearer.status_code, basic.status_code) == (200, 200)
    assert bearer.json()["object"] == "customer"


@pytest.mark.parametrize(
    ("authorization", "message"),
    [
        (None, "You did not provide an API key."),
        ("Bearer sk_test_wrongkey1234", "Invalid API Key provided: sk_test_****1234"),
        ("Basic !!!notbase64", "You did not provide an API key."),
        ("Token sk_test_sim", "You did not provide an API key."),
    ],
)
async def test_missing_or_unknown_keys_are_401(authorization: str | None, message: str) -> None:
    headers = {"authorization": authorization} if authorization is not None else {}
    async with stripe_client(stripe_state(), key=None) as client:
        response = await client.get("/v1/events", headers=headers)
    assert response.status_code == 401
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["message"].startswith(message)
    assert response.headers["request-id"].startswith("req_")


async def test_a_key_added_through_the_control_api_authenticates() -> None:
    async with stripe_client(stripe_state(), key="sk_test_other") as client:
        before = await client.get("/v1/events")
        added = await client.post("/sim/stripe/api-keys", json={"api_key": "sk_test_other"})
        after = await client.get("/v1/events")
    assert (before.status_code, added.status_code, after.status_code) == (401, 204, 200)


async def test_every_answer_has_request_id_and_stripe_version() -> None:
    async with stripe_client(stripe_state()) as client:
        default = await client.get("/v1/events")
        pinned = await client.get("/v1/events", headers={"stripe-version": "2024-06-20"})
    assert len(default.headers["request-id"]) == len("req_") + 14
    assert default.headers["stripe-version"] == DEFAULT_API_VERSION
    assert pinned.headers["stripe-version"] == "2024-06-20"
    assert default.headers["request-id"] != pinned.headers["request-id"]


async def test_unknown_urls_and_wrong_methods_are_unrecognized() -> None:
    async with stripe_client(stripe_state()) as client:
        unknown = await post(client, "/v1/charges")
        wrong_method = await client.delete("/v1/customers")
    for response, method, path in (
        (unknown, "POST", "/v1/charges"),
        (wrong_method, "DELETE", "/v1/customers"),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["message"].startswith(f"Unrecognized request URL ({method}: {path})")


async def test_malformed_bodies_are_400() -> None:
    async with stripe_client(stripe_state()) as client:
        conflict = await client.post(
            "/v1/customers",
            content="metadata=x&metadata[a]=b",
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        not_utf8 = await client.post("/v1/customers", content=b"name=\xff")
    assert conflict.status_code == 400
    assert conflict.json()["error"]["param"] == "metadata"
    assert not_utf8.status_code == 400


async def test_expand_is_accepted_and_ignored() -> None:
    async with stripe_client(stripe_state()) as client:
        response = await post(client, "/v1/customers", {"expand": ["default_source"], "name": "Ada"})
    assert response.status_code == 200
    assert response.json()["name"] == "Ada"
