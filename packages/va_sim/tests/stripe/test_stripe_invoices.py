"""FakeStripe invoices (specs/30 §4.1 Decision P03-T07b rule 6)."""

from __future__ import annotations

from stripe_helpers import (
    event_types,
    events_of,
    monthly_price,
    ok,
    post,
    renew,
    stripe_client,
    stripe_state,
    subscribed_customer,
)
from telnyx_helpers import RecordingTransport


async def test_invoices_are_retrieved_and_listed() -> None:
    async with stripe_client(stripe_state()) as client:
        price = await monthly_price(client)
        cus_a, sub_a = await subscribed_customer(client, price)
        _cus_b, sub_b = await subscribed_customer(client, price, card="pm_card_chargeDeclined")
        renewed = ok(await renew(client, sub_a["id"]))
        invoice = ok(await client.get(f"/v1/invoices/{sub_a['latest_invoice']}"))

        async def ids(**query: str) -> list[str]:
            return [inv["id"] for inv in ok(await client.get("/v1/invoices", params=query))["data"]]

        everything = await ids()
        of_customer = await ids(customer=cus_a)
        of_subscription = await ids(subscription=sub_b["id"])
        open_only = await ids(status="open")
        page = ok(await client.get("/v1/invoices", params={"limit": 1}))
        bad = await client.get("/v1/invoices", params={"status": "late"})
        missing = await client.get("/v1/invoices/in_nope")
    cycle = renewed["invoice"]["id"]
    assert invoice["id"].startswith("in_")
    assert (invoice["object"], invoice["customer"], invoice["currency"], invoice["livemode"]) == (
        "invoice",
        cus_a,
        "usd",
        False,
    )
    assert invoice["lines"]["url"] == f"/v1/invoices/{invoice['id']}/lines"
    line = invoice["lines"]["data"][0]
    assert (line["object"], line["invoice"], line["amount"], line["quantity"]) == (
        "line_item",
        invoice["id"],
        2000,
        1,
    )
    assert line["parent"]["subscription_item_details"]["subscription"] == sub_a["id"]
    assert line["pricing"]["unit_amount_decimal"] == "2000"
    assert everything == [cycle, sub_b["latest_invoice"], sub_a["latest_invoice"]]
    assert of_customer == [cycle, sub_a["latest_invoice"]]
    assert of_subscription == open_only == [sub_b["latest_invoice"]]
    assert page["has_more"] is True
    assert bad.status_code == 400
    assert missing.status_code == 404


async def test_paying_an_open_invoice_reactivates_the_subscription() -> None:
    hooks = RecordingTransport()
    async with stripe_client(stripe_state(hooks)) as client:
        price = await monthly_price(client)
        cus, sub = await subscribed_customer(client, price, card="pm_card_chargeDeclined")
        declined = await post(client, f"/v1/invoices/{sub['latest_invoice']}/pay")
        after_decline = ok(await client.get(f"/v1/invoices/{sub['latest_invoice']}"))
        hooks.received.clear()
        paid = ok(
            await post(
                client, f"/v1/invoices/{sub['latest_invoice']}/pay", {"payment_method": "pm_card_visa"}
            )
        )
        active = ok(await client.get(f"/v1/subscriptions/{sub['id']}"))
        again = await post(client, f"/v1/invoices/{sub['latest_invoice']}/pay")
        methods = ok(await client.get(f"/v1/customers/{cus}/payment_methods"))
    assert declined.status_code == 402
    assert declined.json()["error"]["invoice"] == sub["latest_invoice"]
    assert after_decline["attempt_count"] == 2
    assert (paid["status"], paid["amount_paid"], paid["attempt_count"]) == ("paid", 2000, 3)
    assert active["status"] == "active"
    assert event_types(hooks) == ["invoice.paid", "customer.subscription.updated"]
    assert events_of(hooks)[1]["data"]["previous_attributes"] == {"status": "incomplete"}
    assert again.status_code == 400
    assert again.json()["error"]["message"] == "Invoice is already paid"
    assert len(methods["data"]) == 2


async def test_paying_needs_a_payment_method() -> None:
    async with stripe_client(stripe_state()) as client:
        price = await monthly_price(client)
        bare = ok(await post(client, "/v1/customers"))["id"]
        sub = ok(
            await post(client, "/v1/subscriptions", {"customer": bare, "items": [{"price": price["id"]}]})
        )
        response = await post(client, f"/v1/invoices/{sub['latest_invoice']}/pay")
        foreign = await post(
            client, f"/v1/invoices/{sub['latest_invoice']}/pay", {"payment_method": "pm_nope"}
        )
    assert response.status_code == 400
    assert "no attached payment source" in response.json()["error"]["message"]
    assert foreign.status_code == 404
