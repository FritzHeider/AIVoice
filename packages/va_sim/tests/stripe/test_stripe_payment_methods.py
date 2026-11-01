"""FakeStripe payment methods (Decision P03-T07a rule 6)."""

from __future__ import annotations

import httpx
import pytest
from stripe_helpers import post, stripe_client, stripe_state

from va_sim.stripe.objects import TEST_CARDS


async def customer(client: httpx.AsyncClient) -> str:
    return str((await post(client, "/v1/customers")).json()["id"])


@pytest.mark.parametrize("magic", sorted(TEST_CARDS))
async def test_attaching_a_magic_id_creates_a_new_card(magic: str) -> None:
    state = stripe_state()
    async with stripe_client(state) as client:
        cus = await customer(client)
        attached = await post(client, f"/v1/payment_methods/{magic}/attach", {"customer": cus})
        fetched = await client.get(f"/v1/payment_methods/{attached.json()['id']}")
    method = attached.json()
    spec = TEST_CARDS[magic]
    assert attached.status_code == 200
    assert method["id"].startswith("pm_")
    assert len(method["id"]) == len("pm_") + 24
    assert (method["object"], method["type"], method["customer"]) == ("payment_method", "card", cus)
    assert (method["card"]["brand"], method["card"]["last4"], method["card"]["funding"]) == (
        spec.brand,
        spec.last4,
        spec.funding,
    )
    assert method["card"]["exp_year"] == state.clock.now().year + 3
    assert fetched.json() == method


async def test_list_per_customer_newest_first_with_limit_and_paging() -> None:
    async with stripe_client(stripe_state()) as client:
        cus = await customer(client)
        other = await customer(client)
        ids = [
            (await post(client, "/v1/payment_methods/pm_card_visa/attach", {"customer": cus})).json()["id"]
            for _ in range(3)
        ]
        await post(client, "/v1/payment_methods/pm_card_visa/attach", {"customer": other})
        full = await client.get(f"/v1/customers/{cus}/payment_methods", params={"type": "card"})
        first_page = await client.get(f"/v1/customers/{cus}/payment_methods", params={"limit": 2})
        second_page = await client.get(
            f"/v1/customers/{cus}/payment_methods", params={"limit": 2, "starting_after": ids[1]}
        )
        bad_cursor = await client.get(
            f"/v1/customers/{cus}/payment_methods", params={"starting_after": "pm_x"}
        )
        ending = await client.get(f"/v1/customers/{cus}/payment_methods", params={"ending_before": ids[0]})
        bad_type = await client.get(f"/v1/customers/{cus}/payment_methods", params={"type": "sepa_debit"})
        missing = await client.get("/v1/customers/cus_nope/payment_methods")
    assert [m["id"] for m in full.json()["data"]] == ids[::-1]
    assert full.json()["object"] == "list"
    assert full.json()["url"] == f"/v1/customers/{cus}/payment_methods"
    assert [m["id"] for m in first_page.json()["data"]] == [ids[2], ids[1]]
    assert first_page.json()["has_more"] is True
    assert [m["id"] for m in second_page.json()["data"]] == [ids[0]]
    assert second_page.json()["has_more"] is False
    assert [r.status_code for r in (bad_cursor, ending, bad_type, missing)] == [400, 400, 400, 404]


async def test_attach_errors() -> None:
    async with stripe_client(stripe_state()) as client:
        first = await customer(client)
        second = await customer(client)
        pm = (await post(client, "/v1/payment_methods/pm_card_visa/attach", {"customer": first})).json()["id"]
        again = await post(client, f"/v1/payment_methods/{pm}/attach", {"customer": first})
        stolen = await post(client, f"/v1/payment_methods/{pm}/attach", {"customer": second})
        unknown_magic = await post(
            client, "/v1/payment_methods/pm_card_amex_nope/attach", {"customer": first}
        )
        no_customer = await post(client, f"/v1/payment_methods/{pm}/attach")
        bad_customer = await post(client, f"/v1/payment_methods/{pm}/attach", {"customer": "cus_nope"})
        missing = await client.get("/v1/payment_methods/pm_nope")
    assert again.status_code == 200
    assert stolen.status_code == 400
    assert unknown_magic.status_code == 404
    assert unknown_magic.json()["error"]["message"] == "No such PaymentMethod: 'pm_card_amex_nope'"
    assert no_customer.json()["error"]["code"] == "parameter_missing"
    assert bad_customer.json()["error"]["param"] == "customer"
    assert missing.status_code == 404
