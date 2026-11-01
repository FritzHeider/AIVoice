"""Retry and rate-limit rules of the provisioning, number, and messaging operations
(specs/06-telephony-telnyx.md §2.2; Decisions 2026-11-01 P03-T08a rules 1/3 and P03-T08b rule 1)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
import respx
from telnyx_client_helpers import Rig, rig

from va_telnyx import MessageCreate, NumberSearch, TelnyxApiError, TelnyxClient, TelnyxTransportError

E164 = "+14155550123"
MESSAGE = MessageCreate(
    from_=E164, to="+14155550199", text="hi", messaging_profile_id="mp", webhook_url="https://edge.test/m"
)
ORDER_OK = httpx.Response(200, json={"data": {"id": "o1", "status": "pending"}})
MESSAGE_OK = httpx.Response(
    200, json={"data": {"id": "m1", "direction": "outbound", "from": {"phone_number": E164}}}
)

type Call = Callable[[TelnyxClient], Awaitable[Any]]


async def _order(client: TelnyxClient) -> Any:
    return await client.create_number_order([E164], connection_id="77", customer_reference="org")


async def _message(client: TelnyxClient) -> Any:
    return await client.send_message(MESSAGE)


async def _create_app(client: TelnyxClient) -> Any:
    return await client.create_call_control_app("a", "https://edge.test/v", outbound_voice_profile_id="1")


NON_IDEMPOTENT: list[tuple[str, str, Call]] = [
    ("/number_orders", "order", _order),
    ("/messages", "message", _message),
    ("/call_control_applications", "app", _create_app),
]


@pytest.mark.parametrize(("path", "name", "call"), NON_IDEMPOTENT, ids=[n for _, n, _ in NON_IDEMPOTENT])
async def test_503_is_not_retried(telnyx_api: respx.MockRouter, path: str, name: str, call: Call) -> None:
    route = telnyx_api.post(path).respond(503, json={"errors": [{"code": "x", "title": "busy"}]})
    async with httpx.AsyncClient() as http:
        harness = rig(http)
        with pytest.raises(TelnyxApiError) as caught:
            await call(harness.client)
    assert route.call_count == 1
    assert harness.clock.sleeps == []
    assert caught.value.to_app_error().code == "CARRIER_ERROR"
    assert name


@pytest.mark.parametrize(("path", "name", "call"), NON_IDEMPOTENT, ids=[n for _, n, _ in NON_IDEMPOTENT])
async def test_read_timeout_is_not_retried(
    telnyx_api: respx.MockRouter, path: str, name: str, call: Call
) -> None:
    route = telnyx_api.post(path).mock(side_effect=httpx.ReadTimeout("slow"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelnyxTransportError):
            await call(rig(http).client)
    assert route.call_count == 1
    assert name


async def test_connect_error_before_bytes_is_retried(telnyx_api: respx.MockRouter) -> None:
    orders = telnyx_api.post("/number_orders").mock(side_effect=[httpx.ConnectError("refused"), ORDER_OK])
    messages = telnyx_api.post("/messages").mock(side_effect=[httpx.ConnectTimeout("slow"), MESSAGE_OK])
    async with httpx.AsyncClient() as http:
        harness = rig(http)
        assert (await _order(harness.client)).id == "o1"
        assert (await _message(harness.client)).id == "m1"
    assert (orders.call_count, messages.call_count) == (2, 2)
    assert [round(s, 6) for s in harness.clock.sleeps] == [0.15, 0.15]


async def test_idempotent_number_operations_retry_503(telnyx_api: respx.MockRouter) -> None:
    order = telnyx_api.get("/number_orders/o1").mock(side_effect=[httpx.Response(503), ORDER_OK])
    numbers = telnyx_api.get("/available_phone_numbers").mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json={"data": []})]
    )
    async with httpx.AsyncClient() as http:
        harness = rig(http)
        assert (await harness.client.get_number_order("o1")).status == "pending"
        assert await harness.client.search_numbers(NumberSearch()) == []
    assert (order.call_count, numbers.call_count) == (2, 2)


async def _bucket_keys(harness: Rig) -> set[str]:
    return {key.decode() if isinstance(key, bytes) else key for key in await harness.redis.keys("rl:*")}


async def test_rate_limit_categories(telnyx_api: respx.MockRouter) -> None:
    telnyx_api.get("/available_phone_numbers").respond(200, json={"data": []})
    telnyx_api.post("/number_orders").mock(return_value=ORDER_OK)
    telnyx_api.post("/messages").mock(return_value=MESSAGE_OK)
    telnyx_api.get("/balance").respond(
        200, json={"data": {"balance": "1", "credit_limit": "0", "available_credit": "1", "currency": "USD"}}
    )
    telnyx_api.get("/phone_numbers").respond(200, json={"data": []})
    async with httpx.AsyncClient() as http:
        harness = rig(http)
        await harness.client.get_balance()
        await harness.client.get_phone_number_by_e164(E164)
        assert await _bucket_keys(harness) == set()
        await harness.client.search_numbers(NumberSearch())
        await _order(harness.client)
        await _message(harness.client)
        assert await _bucket_keys(harness) == {
            "rl:telnyx:platform:number_search",
            "rl:telnyx:platform:number_order",
            "rl:telnyx:platform:messaging",
        }
