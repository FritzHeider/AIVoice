"""Number and messaging requests: exact query strings and bodies, validation before I/O
(specs/06-telephony-telnyx.md §2.1; specs/21-messaging-sms.md §2 step 6; Decision 2026-11-01 P03-T08b)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx
from pydantic import ValidationError
from telnyx_client_helpers import ORG_ID, body_of, rig

from va_telnyx import (
    AvailableNumber,
    Message,
    MessageCreate,
    MessageRecipient,
    NumberOrder,
    NumberSearch,
    OrderedNumber,
    PhoneNumber,
    PhoneNumberMessaging,
    number_tags,
)

E164 = "+14155550123"
SMS_WEBHOOK = "https://edge.example.test/telnyx/messaging"


def _available(phone_number: str = E164) -> dict[str, Any]:
    return {
        "record_type": "available_phone_number",
        "phone_number": phone_number,
        "best_effort": False,
        "reservable": True,
        "features": [{"name": "voice"}, {"name": "sms"}],
        "cost_information": {"monthly_cost": "1.00", "upfront_cost": "1.00", "currency": "USD"},
        "region_information": [{"region_type": "state", "region_name": "CA"}],
    }


def _number(**fields: Any) -> dict[str, Any]:
    return {
        "id": "1293384261075731601",
        "record_type": "phone_number",
        "phone_number": E164,
        "status": "active",
        "connection_id": "77",
        "messaging_profile_id": None,
        "tags": [],
        "customer_reference": str(ORG_ID),
        "phone_number_type": "local",
        **fields,
    }


async def test_search_query_is_exact(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.get("/available_phone_numbers").respond(200, json={"data": [_available()]})
    search = NumberSearch(
        national_destination_code="415",
        locality="San Francisco",
        administrative_area="CA",
        contains="555",
        features=("voice", "sms"),
        phone_number_type="local",
        limit=3,
    )
    async with httpx.AsyncClient() as http:
        found = await rig(http).client.search_numbers(search)
    assert route.calls[0].request.url.params.multi_items() == [
        ("filter[country_code]", "US"),
        ("filter[national_destination_code]", "415"),
        ("filter[locality]", "San Francisco"),
        ("filter[administrative_area]", "CA"),
        ("filter[phone_number][contains]", "555"),
        ("filter[phone_number_type]", "local"),
        ("filter[features][]", "voice"),
        ("filter[features][]", "sms"),
        ("filter[limit]", "3"),
        ("filter[best_effort]", "false"),
    ]
    assert found == [
        AvailableNumber(
            phone_number=E164,
            features=("voice", "sms"),
            monthly_cost=Decimal("1.00"),
            upfront_cost=Decimal("1.00"),
            currency="USD",
            region_information=({"region_type": "state", "region_name": "CA"},),
            reservable=True,
        )
    ]


async def test_unset_filters_are_omitted(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.get("/available_phone_numbers").respond(200, json={"data": []})
    async with httpx.AsyncClient() as http:
        assert await rig(http).client.search_numbers(NumberSearch(country_code="CA")) == []
    assert route.calls[0].request.url.params.multi_items() == [
        ("filter[country_code]", "CA"),
        ("filter[limit]", "10"),
        ("filter[best_effort]", "false"),
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"limit": 0},
        {"limit": 51},
        {"features": ("fax",)},
        {"country_code": "usa"},
        {"phone_number_type": "x"},
    ],
)
def test_invalid_searches_fail_before_any_request(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        NumberSearch.model_validate(fields)


async def test_create_number_order_body(telnyx_api: respx.MockRouter) -> None:
    order = {
        "id": "ord-1",
        "status": "pending",
        "phone_numbers": [{"id": "n-1", "phone_number": E164, "status": "pending"}],
        "connection_id": "77",
        "messaging_profile_id": None,
        "customer_reference": str(ORG_ID),
    }
    route = telnyx_api.post("/number_orders").respond(200, json={"data": order})
    async with httpx.AsyncClient() as http:
        result = await rig(http).client.create_number_order(
            [E164], connection_id="77", customer_reference=str(ORG_ID)
        )
    assert body_of(route.calls[0].request) == {
        "phone_numbers": [{"phone_number": E164}],
        "connection_id": "77",
        "customer_reference": str(ORG_ID),
    }
    assert result == NumberOrder(
        id="ord-1",
        status="pending",
        phone_numbers=(OrderedNumber(id="n-1", phone_number=E164, status="pending"),),
        connection_id="77",
        customer_reference=str(ORG_ID),
    )


async def test_create_number_order_with_messaging_profile(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.post("/number_orders").respond(200, json={"data": {"id": "o", "status": "pending"}})
    async with httpx.AsyncClient() as http:
        client = rig(http).client
        await client.create_number_order(
            [E164, "+14155550124"], connection_id="77", customer_reference="org", messaging_profile_id="mp"
        )
        with pytest.raises(ValueError, match="at least one"):
            await client.create_number_order([], connection_id="77", customer_reference="org")
    assert route.call_count == 1
    assert body_of(route.calls[0].request) == {
        "phone_numbers": [{"phone_number": E164}, {"phone_number": "+14155550124"}],
        "connection_id": "77",
        "customer_reference": "org",
        "messaging_profile_id": "mp",
    }


async def test_get_number_order(telnyx_api: respx.MockRouter) -> None:
    telnyx_api.get("/number_orders/ord-1").respond(200, json={"data": {"id": "ord-1", "status": "success"}})
    async with httpx.AsyncClient() as http:
        order = await rig(http).client.get_number_order("ord-1")
    assert order.status == "success"


async def test_lookup_by_e164(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.get("/phone_numbers").mock(
        side_effect=[httpx.Response(200, json={"data": [_number()]}), httpx.Response(200, json={"data": []})]
    )
    async with httpx.AsyncClient() as http:
        client = rig(http).client
        found = await client.get_phone_number_by_e164(E164)
        missing = await client.get_phone_number_by_e164("+14155550999")
    assert found is not None
    assert found.id == "1293384261075731601"
    assert missing is None
    assert route.calls[0].request.url.params.multi_items() == [("filter[phone_number]", E164)]
    assert "filter%5Bphone_number%5D=%2B14155550123" in str(route.calls[0].request.url)


async def test_update_phone_number_bodies(telnyx_api: respx.MockRouter) -> None:
    tags = number_tags(ORG_ID)
    route = telnyx_api.patch("/phone_numbers/n1").respond(200, json={"data": _number(id="n1", tags=tags)})
    async with httpx.AsyncClient() as http:
        client = rig(http).client
        updated = await client.update_phone_number("n1", connection_id="77", tags=tags)
        await client.update_phone_number("n1", tags=[])
        with pytest.raises(ValueError, match="connection_id or tags"):
            await client.update_phone_number("n1")
    assert [body_of(call.request) for call in route.calls] == [
        {"connection_id": "77", "tags": ["va", f"org:{ORG_ID}"]},
        {"tags": []},
    ]
    assert updated == PhoneNumber.model_validate(_number(id="n1", tags=tags))


def test_number_tags() -> None:
    org_id = uuid.UUID("0192f3a0-0000-7000-8000-0000000000aa")
    assert number_tags(org_id) == ["va", "org:0192f3a0-0000-7000-8000-0000000000aa"]


async def test_messaging_update_and_delete(telnyx_api: respx.MockRouter) -> None:
    messaging = telnyx_api.patch("/phone_numbers/n1/messaging").respond(
        200, json={"data": {"id": "n1", "phone_number": E164, "messaging_profile_id": None}}
    )
    delete = telnyx_api.delete("/phone_numbers/n1").respond(200, json={"data": _number(status="deleted")})
    async with httpx.AsyncClient() as http:
        client = rig(http).client
        assigned = await client.update_phone_number_messaging("n1", None)
        deleted = await client.delete_phone_number("n1")
    assert body_of(messaging.calls[0].request) == {"messaging_profile_id": None}
    assert assigned == PhoneNumberMessaging(id="n1", phone_number=E164)
    assert deleted.status == "deleted"
    assert delete.calls[0].request.content == b""


async def test_send_message_body(telnyx_api: respx.MockRouter) -> None:
    record = {
        "id": "msg-1",
        "record_type": "message",
        "direction": "outbound",
        "type": "SMS",
        "from": {"phone_number": E164, "carrier": "Telnyx"},
        "to": [{"phone_number": "+14155550199", "status": "queued"}],
        "text": "hello",
        "parts": 1,
        "encoding": "GSM-7",
        "messaging_profile_id": "mp",
    }
    route = telnyx_api.post("/messages").respond(200, json={"data": record})
    message = MessageCreate(
        from_=E164, to="+14155550199", text="hello", messaging_profile_id="mp", webhook_url=SMS_WEBHOOK
    )
    async with httpx.AsyncClient() as http:
        sent = await rig(http).client.send_message(message)
    assert body_of(route.calls[0].request) == {
        "from": E164,
        "to": "+14155550199",
        "text": "hello",
        "messaging_profile_id": "mp",
        "webhook_url": SMS_WEBHOOK,
        "use_profile_webhooks": False,
    }
    assert sent == Message(
        id="msg-1",
        direction="outbound",
        type="SMS",
        from_=E164,
        to=(MessageRecipient(phone_number="+14155550199", status="queued"),),
        text="hello",
        parts=1,
        encoding="GSM-7",
        messaging_profile_id="mp",
    )


def test_mms_body_and_message_validation() -> None:
    mms = MessageCreate(
        from_=E164,
        to="+14155550199",
        media_urls=("https://cdn.example.test/a.png",),
        subject="Hi",
        messaging_profile_id="mp",
        webhook_url=SMS_WEBHOOK,
        webhook_failover_url="https://edge2.example.test/telnyx/messaging",
    )
    assert mms.body() == {
        "from": E164,
        "to": "+14155550199",
        "media_urls": ["https://cdn.example.test/a.png"],
        "subject": "Hi",
        "messaging_profile_id": "mp",
        "webhook_url": SMS_WEBHOOK,
        "webhook_failover_url": "https://edge2.example.test/telnyx/messaging",
        "use_profile_webhooks": False,
    }
    base: dict[str, Any] = {
        "from_": E164,
        "to": "+14155550199",
        "messaging_profile_id": "mp",
        "webhook_url": SMS_WEBHOOK,
    }
    with pytest.raises(ValidationError, match="text or media_urls is required"):
        MessageCreate.model_validate(base)
    with pytest.raises(ValidationError):
        MessageCreate.model_validate({**base, "text": "x", "to": "4155550199"})
    with pytest.raises(ValidationError):
        MessageCreate.model_validate(
            {**base, "media_urls": [f"https://cdn.example.test/{n}" for n in range(11)]}
        )
