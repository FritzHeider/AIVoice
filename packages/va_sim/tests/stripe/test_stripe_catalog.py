"""FakeStripe products and prices (specs/30 §4.1 Decision P03-T07b rule 2)."""

from __future__ import annotations

from typing import Any

import pytest
from stripe_helpers import ok, post, stripe_client, stripe_state


async def test_products_are_created_and_retrieved() -> None:
    async with stripe_client(stripe_state()) as client:
        product = ok(
            await post(
                client,
                "/v1/products",
                {"name": "Build", "description": "Build plan", "metadata": {"plan": "build"}},
            )
        )
        custom = ok(
            await post(client, "/v1/products", {"id": "prod_build", "name": "Build", "active": False})
        )
        duplicate = await post(client, "/v1/products", {"id": "prod_build", "name": "Again"})
        fetched = ok(await client.get(f"/v1/products/{product['id']}"))
        missing = await client.get("/v1/products/prod_nope")
        nameless = await post(client, "/v1/products", {})
    assert product["id"].startswith("prod_")
    assert len(product["id"]) == len("prod_") + 14
    assert (product["object"], product["active"], product["name"], product["livemode"]) == (
        "product",
        True,
        "Build",
        False,
    )
    assert product["metadata"] == {"plan": "build"}
    assert (custom["id"], custom["active"]) == ("prod_build", False)
    assert duplicate.status_code == 400
    assert duplicate.json()["error"]["code"] == "resource_already_exists"
    assert fetched == product
    assert missing.status_code == 404
    assert nameless.json()["error"]["param"] == "name"


async def test_prices_with_a_product_or_product_data() -> None:
    async with stripe_client(stripe_state()) as client:
        product = ok(await post(client, "/v1/products", {"name": "Credits"}))
        one_time = ok(
            await post(
                client,
                "/v1/prices",
                {"currency": "USD", "unit_amount": 500, "product": product["id"], "nickname": "credits"},
            )
        )
        recurring = ok(
            await post(
                client,
                "/v1/prices",
                {
                    "currency": "usd",
                    "unit_amount": 29900,
                    "product_data": {"name": "Scale", "metadata": {"plan": "scale"}},
                    "recurring": {"interval": "month", "interval_count": 3},
                    "lookup_key": "scale_quarterly",
                    "metadata": {"plan": "scale"},
                },
            )
        )
        fetched = ok(await client.get(f"/v1/prices/{recurring['id']}"))
        made = ok(await client.get(f"/v1/products/{recurring['product']}"))
    assert len(one_time["id"]) == len("price_") + 24
    assert (one_time["object"], one_time["type"], one_time["recurring"], one_time["currency"]) == (
        "price",
        "one_time",
        None,
        "usd",
    )
    assert (one_time["unit_amount"], one_time["unit_amount_decimal"], one_time["nickname"]) == (
        500,
        "500",
        "credits",
    )
    assert recurring["type"] == "recurring"
    assert recurring["recurring"]["interval"] == "month"
    assert recurring["recurring"]["interval_count"] == 3
    assert recurring["recurring"]["usage_type"] == "licensed"
    assert (recurring["lookup_key"], recurring["metadata"], recurring["billing_scheme"]) == (
        "scale_quarterly",
        {"plan": "scale"},
        "per_unit",
    )
    assert fetched == recurring
    assert (made["name"], made["metadata"]) == ("Scale", {"plan": "scale"})


@pytest.mark.parametrize(
    ("params", "param"),
    [
        ({"unit_amount": 100, "product_data": {"name": "x"}}, "currency"),
        ({"currency": "us", "unit_amount": 100, "product_data": {"name": "x"}}, "currency"),
        ({"currency": "usd", "product_data": {"name": "x"}}, "unit_amount"),
        ({"currency": "usd", "unit_amount": -1, "product_data": {"name": "x"}}, "unit_amount"),
        ({"currency": "usd", "unit_amount": "ten", "product_data": {"name": "x"}}, "unit_amount"),
        ({"currency": "usd", "unit_amount": 100}, "product"),
        (
            {"currency": "usd", "unit_amount": 100, "product": "prod_x", "product_data": {"name": "x"}},
            "product",
        ),
        ({"currency": "usd", "unit_amount": 100, "product_data": {"description": "d"}}, "product_data[name]"),
        (
            {"currency": "usd", "unit_amount": 100, "product_data": {"name": "x", "color": "red"}},
            "product_data[color]",
        ),
        (
            {
                "currency": "usd",
                "unit_amount": 100,
                "product_data": {"name": "x"},
                "recurring": {"interval": "hour"},
            },
            "recurring[interval]",
        ),
        (
            {
                "currency": "usd",
                "unit_amount": 100,
                "product_data": {"name": "x"},
                "recurring": {"interval": "month", "interval_count": 13},
            },
            "recurring[interval_count]",
        ),
        (
            {
                "currency": "usd",
                "unit_amount": 100,
                "product_data": {"name": "x"},
                "recurring": {"interval": "month", "usage_type": "metered"},
            },
            "recurring[usage_type]",
        ),
        (
            {"currency": "usd", "unit_amount": 100, "product_data": {"name": "x"}, "recurring": "month"},
            "recurring",
        ),
        ({"currency": "usd", "unit_amount": 100, "product_data": {"name": "x"}, "tiers": "x"}, "tiers"),
    ],
)
async def test_invalid_prices_are_rejected(params: dict[str, Any], param: str) -> None:
    state = stripe_state()
    async with stripe_client(state) as client:
        response = await post(client, "/v1/prices", params)
    assert response.status_code == 400
    assert response.json()["error"]["param"] == param
    assert not state.stripe.prices
    assert not state.stripe.products


async def test_lookup_keys_are_unique_unless_transferred() -> None:
    async with stripe_client(stripe_state()) as client:
        base = {"currency": "usd", "unit_amount": 100, "product_data": {"name": "Plan"}, "lookup_key": "plan"}
        first = ok(await post(client, "/v1/prices", base))
        clash = await post(client, "/v1/prices", base)
        moved = ok(await post(client, "/v1/prices", {**base, "transfer_lookup_key": True}))
        old = ok(await client.get(f"/v1/prices/{first['id']}"))
    assert clash.status_code == 400
    assert first["id"] in clash.json()["error"]["message"]
    assert moved["lookup_key"] == "plan"
    assert old["lookup_key"] is None


def ids(body: dict[str, Any]) -> list[str]:
    return [item["id"] for item in body["data"]]


async def test_price_lists_filter_and_page_newest_first() -> None:
    async with stripe_client(stripe_state()) as client:
        product = ok(await post(client, "/v1/products", {"name": "P"}))["id"]
        a = ok(await post(client, "/v1/prices", {"currency": "usd", "unit_amount": 1, "product": product}))
        b = ok(
            await post(
                client,
                "/v1/prices",
                {"currency": "usd", "unit_amount": 2, "product": product, "recurring": {"interval": "year"}},
            )
        )
        c = ok(
            await post(
                client,
                "/v1/prices",
                {
                    "currency": "usd",
                    "unit_amount": 3,
                    "product_data": {"name": "Q"},
                    "active": False,
                    "lookup_key": "q",
                },
            )
        )
        everything = ok(await client.get("/v1/prices"))
        of_product = ok(await client.get("/v1/prices", params={"product": product}))
        active = ok(await client.get("/v1/prices", params={"active": "true"}))
        recurring = ok(await client.get("/v1/prices", params={"type": "recurring"}))
        by_key = ok(await client.get("/v1/prices", params={"lookup_keys[]": "q"}))
        page = ok(await client.get("/v1/prices", params={"limit": 1, "starting_after": c["id"]}))
        bad_type = await client.get("/v1/prices", params={"type": "metered"})
        bad_keys = await client.get("/v1/prices", params={"lookup_keys": "q"})
    assert ids(everything) == [c["id"], b["id"], a["id"]]
    assert everything["url"] == "/v1/prices"
    assert ids(of_product) == [b["id"], a["id"]]
    assert ids(active) == [b["id"], a["id"]]
    assert ids(recurring) == [b["id"]]
    assert ids(by_key) == [c["id"]]
    assert (ids(page), page["has_more"]) == ([b["id"]], True)
    assert bad_type.status_code == 400
    assert bad_keys.json()["error"]["param"] == "lookup_keys"


async def test_unknown_prices_and_products_are_404() -> None:
    async with stripe_client(stripe_state()) as client:
        response = await client.get("/v1/prices/price_nope")
        product = await post(
            client, "/v1/prices", {"currency": "usd", "unit_amount": 1, "product": "prod_nope"}
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_missing"
    assert (product.status_code, product.json()["error"]["param"]) == (404, "product")
