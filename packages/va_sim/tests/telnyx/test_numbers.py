"""Number search, number orders, and owned phone numbers (specs/06 §2.1, specs/30 §3.2)."""

from __future__ import annotations

import pytest
from telnyx_helpers import (
    APP_URL,
    VOICE_URL,
    RecordingTransport,
    advance,
    create_app,
    create_messaging_profile,
    error_of,
    own_number,
    telnyx_client,
    telnyx_state,
    verify_signature,
)
from telnyx_shapes import (
    AvailablePhoneNumber,
    MessagingPhoneNumber,
    NumberOrder,
    PhoneNumber,
    WebhookEnvelope,
    assert_shape,
    assert_shapes,
)

from va_sim.telnyx.inventory import seed_inventory

SEARCH = "/v2/available_phone_numbers"
ORDERS = "/v2/number_orders"
NUMBERS = "/v2/phone_numbers"
SF_FIRST = "+14155552000"


def test_inventory_is_deterministic_and_spans_the_four_blocks() -> None:
    inventory = seed_inventory()
    assert inventory == seed_inventory()
    assert len(inventory) == 200
    assert {number.phone_number[:8] for number in inventory} == {
        "+1415555",
        "+1212555",
        "+1888555",
        "+4420794",
    }
    assert inventory[0].phone_number == SF_FIRST
    assert inventory[-1].phone_number == "+442079460049"


@pytest.mark.parametrize(
    ("params", "expected_count", "first"),
    [
        ({}, 10, SF_FIRST),
        ({"filter[country_code]": "GB", "filter[limit]": "50"}, 50, "+442079460000"),
        ({"filter[national_destination_code]": "212", "filter[limit]": "3"}, 3, "+12125552000"),
        ({"filter[phone_number][contains]": "5552049"}, 3, "+14155552049"),
        ({"filter[phone_number_type]": "toll_free"}, 10, "+18885552000"),
        ({"filter[locality]": "new york", "filter[administrative_area]": "ny"}, 10, "+12125552000"),
        (
            {"filter[features][]": ["sms", "mms"], "filter[country_code]": "us", "filter[limit]": "50"},
            50,
            SF_FIRST,
        ),
        ({"filter[features][]": "voice", "filter[phone_number_type]": "national"}, 10, "+442079460000"),
        ({"filter[locality]": "Paris"}, 0, None),
    ],
)
async def test_search_filters(
    params: dict[str, str | list[str]], expected_count: int, first: str | None
) -> None:
    async with telnyx_client(telnyx_state()) as client:
        response = await client.get(SEARCH, params=params)
    body = response.json()
    found = assert_shapes(AvailablePhoneNumber, body["data"])
    assert len(found) == expected_count
    assert body["meta"] == {"total_results": expected_count, "best_effort_results": 0}
    if first is not None:
        assert found[0].phone_number == first


async def test_search_record_shape() -> None:
    async with telnyx_client(telnyx_state()) as client:
        response = await client.get(SEARCH, params={"filter[limit]": "1"})
    [record] = response.json()["data"]
    assert record["features"] == [{"name": "voice"}, {"name": "sms"}, {"name": "mms"}]
    assert record["cost_information"] == {"monthly_cost": "1.00", "upfront_cost": "1.00", "currency": "USD"}
    assert record["region_information"] == [
        {"region_type": "country_code", "region_name": "US"},
        {"region_type": "state", "region_name": "CA"},
        {"region_type": "location", "region_name": "San Francisco"},
        {"region_type": "rate_center", "region_name": "SAN FRANCISCO"},
    ]


@pytest.mark.parametrize(
    ("params", "parameter"),
    [
        ({"filter[limit]": "51"}, "filter[limit]"),
        ({"filter[limit]": "0"}, "filter[limit]"),
        ({"filter[phone_number_type]": "landline"}, "filter[phone_number_type]"),
        ({"filter[features][]": "fax"}, "filter[features][]"),
    ],
)
async def test_search_rejects_bad_filters(params: dict[str, str], parameter: str) -> None:
    async with telnyx_client(telnyx_state()) as client:
        response = await client.get(SEARCH, params=params)
    assert error_of(response) == (422, "10015", parameter)


async def test_order_completes_after_one_second_and_numbers_become_owned() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        app = await create_app(client)
        profile = await create_messaging_profile(client)
        created = await client.post(
            ORDERS,
            json={
                "phone_numbers": [{"phone_number": SF_FIRST}, {"phone_number": "+14155552001"}],
                "connection_id": app["id"],
                "messaging_profile_id": profile["id"],
                "customer_reference": "org-1",
            },
        )
        order = created.json()["data"]
        searched = await client.get(SEARCH, params={"filter[limit]": "1"})
        pending = await client.get(f"{ORDERS}/{order['id']}")
        await state.tasks.drain()
        done = await client.get(f"{ORDERS}/{order['id']}")
        owned = await client.get(NUMBERS)
        missing = await client.get(f"{ORDERS}/nope")
    assert created.status_code == 200
    parsed = assert_shape(NumberOrder, order)
    assert parsed.status == "pending"
    assert [entry.status for entry in parsed.phone_numbers] == ["pending", "pending"]
    assert searched.json()["data"][0]["phone_number"] == "+14155552002"
    assert pending.json()["data"]["status"] == "pending"
    finished = assert_shape(NumberOrder, done.json()["data"])
    assert finished.status == "success"
    assert [entry.status for entry in finished.phone_numbers] == ["success", "success"]
    assert finished.updated_at == "2026-01-01T00:00:01Z"
    numbers = assert_shapes(PhoneNumber, owned.json()["data"])
    assert [number.phone_number for number in numbers] == [SF_FIRST, "+14155552001"]
    assert {(n.connection_id, n.messaging_profile_id, n.customer_reference) for n in numbers} == {
        (app["id"], profile["id"], "org-1")
    }
    [webhook] = transport.of_type("number_order.complete")
    assert webhook.url == APP_URL
    assert webhook.virtual_ms == 1000
    verify_signature(state.signing_key.public_key_b64, webhook.headers, webhook.body)
    envelope = assert_shape(WebhookEnvelope, webhook.envelope)
    assert envelope.data.payload["status"] == "success"
    assert envelope.data.payload["id"] == order["id"]
    assert error_of(missing) == (404, "10005", None)


async def test_failing_orders_release_their_numbers() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with telnyx_client(state) as client:
        configured = await client.patch("/sim/telnyx/config", json={"fail_number_orders": True})
        order = (await client.post(ORDERS, json={"phone_numbers": [{"phone_number": SF_FIRST}]})).json()[
            "data"
        ]
        await state.tasks.drain()
        done = await client.get(f"{ORDERS}/{order['id']}")
        searched = await client.get(SEARCH, params={"filter[limit]": "1"})
        owned = await client.get(NUMBERS)
    assert configured.json()["fail_number_orders"] is True
    assert done.json()["data"]["status"] == "failure"
    assert done.json()["data"]["phone_numbers"][0]["status"] == "failure"
    assert searched.json()["data"][0]["phone_number"] == SF_FIRST
    assert owned.json()["data"] == []
    [webhook] = transport.received
    assert webhook.url == VOICE_URL
    assert webhook.envelope["data"]["payload"]["status"] == "failure"


@pytest.mark.parametrize(
    ("body", "error"),
    [
        (
            {"phone_numbers": [{"phone_number": "+19995550000"}]},
            (422, "85001", "/phone_numbers/0/phone_number"),
        ),
        (
            {"phone_numbers": [{"phone_number": SF_FIRST}, {"phone_number": SF_FIRST}]},
            (422, "10015", "/phone_numbers/1/phone_number"),
        ),
        (
            {"phone_numbers": [{"phone_number": "4155552000"}]},
            (422, "10015", "/phone_numbers/0/phone_number"),
        ),
        ({"phone_numbers": []}, (422, "10015", "/phone_numbers")),
        (
            {"phone_numbers": [{"phone_number": SF_FIRST}], "connection_id": "1"},
            (422, "10015", "/connection_id"),
        ),
        (
            {"phone_numbers": [{"phone_number": SF_FIRST}], "messaging_profile_id": "x"},
            (422, "10015", "/messaging_profile_id"),
        ),
    ],
)
async def test_invalid_orders(body: dict[str, object], error: tuple[int, str, str]) -> None:
    async with telnyx_client(telnyx_state()) as client:
        response = await client.post(ORDERS, json=body)
    assert error_of(response) == error


async def test_owned_and_reserved_numbers_cannot_be_ordered_again() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        await client.post(ORDERS, json={"phone_numbers": [{"phone_number": SF_FIRST}]})
        reserved = await client.post(ORDERS, json={"phone_numbers": [{"phone_number": SF_FIRST}]})
        await state.tasks.drain()
        owned = await client.post(ORDERS, json={"phone_numbers": [{"phone_number": SF_FIRST}]})
    assert error_of(reserved) == (422, "85001", "/phone_numbers/0/phone_number")
    assert error_of(owned) == (422, "85001", "/phone_numbers/0/phone_number")


async def test_phone_number_filters_patch_messaging_and_delete() -> None:
    state = telnyx_state(RecordingTransport())
    async with telnyx_client(state) as client:
        app = await create_app(client)
        profile = await create_messaging_profile(client)
        number = await own_number(client, state, SF_FIRST)
        await own_number(client, state, "+12125552000")
        advance(state, 10)
        patched = await client.patch(
            f"{NUMBERS}/{number['id']}", json={"connection_id": app["id"], "tags": ["va", "org:1"]}
        )
        cleared = await client.patch(
            f"{NUMBERS}/{number['id']}", json={"tags": None, "customer_reference": "r"}
        )
        await client.patch(f"{NUMBERS}/{number['id']}", json={"tags": ["va"]})
        tagged = await client.get(NUMBERS, params={"filter[tag]": "va"})
        bad_connection = await client.patch(f"{NUMBERS}/{number['id']}", json={"connection_id": "404"})
        unknown_field = await client.patch(f"{NUMBERS}/{number['id']}", json={"bogus": True})
        messaging = await client.patch(
            f"{NUMBERS}/{number['id']}/messaging", json={"messaging_profile_id": profile["id"]}
        )
        bad_profile = await client.patch(
            f"{NUMBERS}/{number['id']}/messaging", json={"messaging_profile_id": "x"}
        )
        fetched = await client.get(f"{NUMBERS}/{number['id']}")
        deleted = await client.delete(f"{NUMBERS}/{number['id']}")
        gone = await client.get(f"{NUMBERS}/{number['id']}")
        gone_patch = await client.patch(f"{NUMBERS}/{number['id']}", json={})
        gone_messaging = await client.patch(f"{NUMBERS}/{number['id']}/messaging", json={})
        gone_delete = await client.delete(f"{NUMBERS}/{number['id']}")
        searched = await client.get(SEARCH, params={"filter[limit]": "1"})
    assert patched.json()["data"]["connection_id"] == app["id"]
    assert patched.json()["data"]["tags"] == ["va", "org:1"]
    assert patched.json()["data"]["updated_at"] == "2026-01-01T00:00:12Z"
    assert (cleared.json()["data"]["tags"], cleared.json()["data"]["customer_reference"]) == ([], "r")
    assert [item["phone_number"] for item in tagged.json()["data"]] == [SF_FIRST]
    assert error_of(bad_connection) == (422, "10015", "/connection_id")
    assert error_of(unknown_field) == (422, "10015", "/bogus")
    settings = assert_shape(MessagingPhoneNumber, messaging.json()["data"])
    assert (settings.id, settings.messaging_profile_id) == (number["id"], profile["id"])
    assert error_of(bad_profile) == (422, "10015", "/messaging_profile_id")
    assert fetched.json()["data"]["messaging_profile_id"] == profile["id"]
    assert deleted.json()["data"]["status"] == "deleted"
    for response in (gone, gone_patch, gone_messaging, gone_delete):
        assert error_of(response) == (404, "10005", None)
    assert searched.json()["data"][0]["phone_number"] == SF_FIRST
