"""FakeStripe billing portal sessions (specs/30 §4.1; Decision P03-T07a rule 9)."""

from __future__ import annotations

from stripe_helpers import post, stripe_client, stripe_state


async def test_create_a_session_and_open_its_page() -> None:
    async with stripe_client(stripe_state()) as client:
        cus = (await post(client, "/v1/customers")).json()["id"]
        created = await post(
            client,
            "/v1/billing_portal/sessions",
            {"customer": cus, "return_url": "http://app/settings?a=1&b=2"},
        )
        session = created.json()
        page = await client.get(session["url"].removeprefix("http://sim:9400"))
    assert created.status_code == 200
    assert session["id"].startswith("bps_")
    assert session["object"] == "billing_portal.session"
    assert session["url"] == f"http://sim:9400/sim/stripe/portal/{session['id']}"
    assert (session["customer"], session["return_url"]) == (cus, "http://app/settings?a=1&b=2")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert cus in page.text
    assert 'href="http://app/settings?a=1&amp;b=2"' in page.text


async def test_a_session_without_return_url_has_no_link() -> None:
    async with stripe_client(stripe_state()) as client:
        cus = (await post(client, "/v1/customers")).json()["id"]
        session = (await post(client, "/v1/billing_portal/sessions", {"customer": cus})).json()
        page = await client.get(f"/sim/stripe/portal/{session['id']}")
    assert session["return_url"] is None
    assert "href" not in page.text


async def test_customer_is_required_and_must_exist() -> None:
    async with stripe_client(stripe_state()) as client:
        missing = await post(client, "/v1/billing_portal/sessions")
        unknown = await post(client, "/v1/billing_portal/sessions", {"customer": "cus_nope"})
        page = await client.get("/sim/stripe/portal/bps_nope")
    assert (missing.status_code, missing.json()["error"]["code"]) == (400, "parameter_missing")
    assert (unknown.status_code, unknown.json()["error"]["param"]) == (404, "customer")
    assert page.status_code == 404
    assert page.json()["error"]["code"] == "not_found"
