"""FakeStripe customers (Decision P03-T07a rule 7)."""

from __future__ import annotations

from stripe_helpers import post, stripe_client, stripe_state


async def test_create_and_retrieve() -> None:
    state = stripe_state()
    async with stripe_client(state) as client:
        created = await post(
            client,
            "/v1/customers",
            {"email": "owner@acme.test", "name": "Acme", "metadata": {"org_id": "org_1"}},
        )
        fetched = await client.get(f"/v1/customers/{created.json()['id']}")
    body = created.json()
    assert created.status_code == 200
    assert body["id"].startswith("cus_")
    assert len(body["id"]) == len("cus_") + 14
    assert body["object"] == "customer"
    assert body["livemode"] is False
    assert (body["email"], body["name"], body["metadata"]) == ("owner@acme.test", "Acme", {"org_id": "org_1"})
    assert body["invoice_settings"]["default_payment_method"] is None
    assert body["created"] == int(state.clock.now().timestamp())
    assert fetched.json() == body


async def test_update_merges_metadata_and_unsets_with_empty_strings() -> None:
    async with stripe_client(stripe_state()) as client:
        created = await post(
            client, "/v1/customers", {"name": "Acme", "phone": "+15550001", "metadata": {"a": "1", "b": "2"}}
        )
        customer_id = created.json()["id"]
        updated = await post(
            client, f"/v1/customers/{customer_id}", {"phone": "", "metadata": {"a": "", "c": "3"}}
        )
        cleared = await post(client, f"/v1/customers/{customer_id}", {"metadata": ""})
    assert updated.json()["metadata"] == {"b": "2", "c": "3"}
    assert updated.json()["phone"] is None
    assert updated.json()["name"] == "Acme"
    assert cleared.json()["metadata"] == {}


async def test_unknown_parameters_and_ids() -> None:
    async with stripe_client(stripe_state()) as client:
        unknown = await post(client, "/v1/customers", {"nickname": "x"})
        nested_unknown = await post(client, "/v1/customers", {"invoice_settings": {"footer_text": "x"}})
        missing = await client.get("/v1/customers/cus_nope")
        missing_update = await post(client, "/v1/customers/cus_nope", {"name": "x"})
        query_param = await client.get("/v1/customers/cus_nope", params={"foo": "1"})
    assert unknown.status_code == 400
    assert unknown.json()["error"] | {"doc_url": None} == {
        "type": "invalid_request_error",
        "code": "parameter_unknown",
        "message": "Received unknown parameter: nickname",
        "param": "nickname",
        "doc_url": None,
    }
    assert nested_unknown.json()["error"]["param"] == "invoice_settings[footer_text]"
    for response in (missing, missing_update):
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "resource_missing"
        assert response.json()["error"]["message"] == "No such customer: 'cus_nope'"
        assert response.json()["error"]["doc_url"] == "https://docs.stripe.com/error-codes/resource-missing"
    assert query_param.json()["error"]["code"] == "parameter_unknown"
    assert unknown.json()["error"]["doc_url"] == "https://docs.stripe.com/error-codes/parameter-unknown"


async def test_default_payment_method_must_be_attached_to_the_customer() -> None:
    state = stripe_state()
    async with stripe_client(state) as client:
        first = (await post(client, "/v1/customers")).json()["id"]
        second = (await post(client, "/v1/customers")).json()["id"]
        pm = (await post(client, "/v1/payment_methods/pm_card_visa/attach", {"customer": first})).json()["id"]
        refused = await post(
            client, f"/v1/customers/{second}", {"invoice_settings": {"default_payment_method": pm}}
        )
        accepted = await post(
            client, f"/v1/customers/{first}", {"invoice_settings": {"default_payment_method": pm}}
        )
        unset = await post(
            client, f"/v1/customers/{first}", {"invoice_settings": {"default_payment_method": ""}}
        )
        unchanged = await post(client, f"/v1/customers/{first}", {"invoice_settings": {}})
    assert refused.status_code == 400
    assert refused.json()["error"]["param"] == "invoice_settings[default_payment_method]"
    assert state.stripe.customers[second]["invoice_settings"]["default_payment_method"] is None
    assert accepted.json()["invoice_settings"]["default_payment_method"] == pm
    assert unset.json()["invoice_settings"]["default_payment_method"] is None
    assert unchanged.status_code == 200


async def test_create_with_a_magic_payment_method_as_default() -> None:
    state = stripe_state()
    async with stripe_client(state) as client:
        created = await post(
            client,
            "/v1/customers",
            {
                "payment_method": "pm_card_visa",
                "invoice_settings": {"default_payment_method": "pm_card_visa"},
            },
        )
    customer = created.json()
    default = customer["invoice_settings"]["default_payment_method"]
    assert default.startswith("pm_")
    assert default != "pm_card_visa"
    assert state.stripe.payment_methods[default]["customer"] == customer["id"]


async def test_a_rejected_create_leaves_nothing_behind() -> None:
    state = stripe_state()
    async with stripe_client(state) as client:
        other = (await post(client, "/v1/customers")).json()["id"]
        detached = (await post(client, "/v1/payment_methods/pm_card_visa/attach", {"customer": other})).json()
        state.stripe.payment_methods[detached["id"]]["customer"] = None
        rejected = await post(
            client,
            "/v1/customers",
            {"payment_method": detached["id"], "invoice_settings": {"default_payment_method": "pm_unknown"}},
        )
        magic_rejected = await post(
            client,
            "/v1/customers",
            {"payment_method": "pm_card_visa", "invoice_settings": {"default_payment_method": "pm_unknown"}},
        )
    assert (rejected.status_code, magic_rejected.status_code) == (400, 400)
    assert list(state.stripe.customers) == [other]
    assert list(state.stripe.payment_methods) == [detached["id"]]
    assert state.stripe.payment_methods[detached["id"]]["customer"] is None
