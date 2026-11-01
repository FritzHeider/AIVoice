"""Retry policy: delays with jitter, Retry-After, retryable statuses and transport errors, non-idempotent
rules (specs/06-telephony-telnyx.md §2.2, Decision 2026-11-01 P03-T08a)."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx
from telnyx_client_helpers import CCID, MidJitter, Rig, rig

from va_telnyx import TelnyxApiError, TelnyxTransportError
from va_telnyx.retry import RetryPolicy, retry_after_seconds
from va_telnyx.transport import Operation

OK = httpx.Response(200, json={"data": {"result": "ok"}})
HANGUP = f"/calls/{CCID}/actions/hangup"
ORDER = Operation("create_number_order", "POST", None, idempotent=False)


async def _hangup(rig_: Rig) -> bool:
    return await rig_.client.hangup(CCID, command_id="hangup-1")


def _rounded(sleeps: list[float]) -> list[float]:
    return [round(s, 6) for s in sleeps]


async def test_503_twice_then_success_waits_100_and_400_ms_plus_jitter(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.post(HANGUP).mock(side_effect=[httpx.Response(503), httpx.Response(503), OK])
    async with httpx.AsyncClient() as http:
        harness = rig(http)
        assert await _hangup(harness)
    assert route.call_count == 3
    assert _rounded(harness.clock.sleeps) == [0.15, 0.45]
    assert {call.request.content for call in route.calls} == {route.calls[0].request.content}


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_four_failures_exhaust_three_retries(telnyx_api: respx.MockRouter, status: int) -> None:
    route = telnyx_api.post(HANGUP).respond(status, json={"errors": [{"code": "x", "title": "busy"}]})
    async with httpx.AsyncClient() as http:
        harness = rig(http)
        with pytest.raises(TelnyxApiError) as caught:
            await _hangup(harness)
    assert route.call_count == 4
    assert _rounded(harness.clock.sleeps) == [0.15, 0.45, 1.25]
    assert caught.value.status == status
    assert caught.value.to_app_error().code == "CARRIER_ERROR"


async def test_retry_after_seconds_replaces_a_shorter_delay(telnyx_api: respx.MockRouter) -> None:
    telnyx_api.post(HANGUP).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "2"}), OK])
    async with httpx.AsyncClient() as http:
        harness = rig(http)
        assert await _hangup(harness)
    assert harness.clock.sleeps == [2.0]


async def test_retry_after_http_date(telnyx_api: respx.MockRouter) -> None:
    async with httpx.AsyncClient() as http:
        harness = rig(http)
        when = format_datetime(harness.clock.now() + timedelta(seconds=3), usegmt=True)
        telnyx_api.post(HANGUP).mock(side_effect=[httpx.Response(503, headers={"Retry-After": when}), OK])
        assert await _hangup(harness)
    assert harness.clock.sleeps == [3.0]


async def test_retry_after_beyond_the_cap_stops_retrying(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.post(HANGUP).respond(429, headers={"Retry-After": "30"})
    async with httpx.AsyncClient() as http:
        harness = rig(http)
        with pytest.raises(TelnyxApiError) as caught:
            await _hangup(harness)
    assert (route.call_count, harness.clock.sleeps, caught.value.status) == (1, [], 429)


@pytest.mark.parametrize("status", [400, 401, 403, 409, 422])
async def test_other_errors_are_not_retried(telnyx_api: respx.MockRouter, status: int) -> None:
    route = telnyx_api.post(HANGUP).respond(status, json={"errors": [{"code": "10015", "title": "Invalid"}]})
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelnyxApiError):
            await _hangup(rig(http))
    assert route.call_count == 1


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("slow"),
        httpx.ReadTimeout("slow"),
        httpx.RemoteProtocolError("x"),
    ],
)
async def test_transport_errors_are_retried_for_commands(
    telnyx_api: respx.MockRouter, error: httpx.TransportError
) -> None:
    route = telnyx_api.post(HANGUP).mock(side_effect=[error, OK])
    async with httpx.AsyncClient() as http:
        harness = rig(http)
        assert await _hangup(harness)
    assert (route.call_count, _rounded(harness.clock.sleeps)) == (2, [0.15])


async def test_persistent_transport_failure_raises_transport_error(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.post(HANGUP).mock(side_effect=httpx.ConnectError("refused"))
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelnyxTransportError) as caught:
            await _hangup(rig(http))
    assert route.call_count == 4
    assert isinstance(caught.value.__cause__, httpx.ConnectError)
    assert caught.value.to_app_error().details["transport"] == "ConnectError"


async def _order(harness: Rig) -> dict[str, object]:
    return await harness.client._transport.request(ORDER, "/number_orders", json={"phone_numbers": []})


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_non_idempotent_operations_never_retry_statuses(
    telnyx_api: respx.MockRouter, status: int
) -> None:
    route = telnyx_api.post("/number_orders").respond(status)
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelnyxApiError):
            await _order(rig(http))
    assert route.call_count == 1


@pytest.mark.parametrize(
    "error", [httpx.ReadTimeout("slow"), httpx.WriteError("x"), httpx.RemoteProtocolError("x")]
)
async def test_non_idempotent_operations_never_retry_after_bytes_were_sent(
    telnyx_api: respx.MockRouter, error: httpx.TransportError
) -> None:
    route = telnyx_api.post("/number_orders").mock(side_effect=error)
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelnyxTransportError):
            await _order(rig(http))
    assert route.call_count == 1


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("refused"), httpx.ConnectTimeout("x"), httpx.PoolTimeout("x")]
)
async def test_non_idempotent_operations_retry_when_no_bytes_were_sent(
    telnyx_api: respx.MockRouter, error: httpx.TransportError
) -> None:
    route = telnyx_api.post("/number_orders").mock(
        side_effect=[error, httpx.Response(200, json={"data": {}})]
    )
    async with httpx.AsyncClient() as http:
        assert await _order(rig(http)) == {"data": {}}
    assert route.call_count == 2


def test_retry_after_parsing() -> None:
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    assert retry_after_seconds(None, now) is None
    assert retry_after_seconds("  ", now) is None
    assert retry_after_seconds("7", now) == 7.0
    assert retry_after_seconds("soon", now) is None
    assert retry_after_seconds("Sun, 01 Nov 2026 12:00:04 GMT", now) == 4.0
    assert retry_after_seconds("Sun, 01 Nov 2026 11:00:00 GMT", now) == 0.0
    assert retry_after_seconds("Sun, 01 Nov 2026 12:00:02 -0000", now) == 2.0


def test_policy_delays_and_jitter_bounds() -> None:
    policy = RetryPolicy()
    assert policy.max_retries == 3
    rng = random.Random(1)  # noqa: S311 - jitter bounds, not a secret
    for index, base in enumerate((0.1, 0.4, 1.2)):
        delay = policy.delay(index, rng)
        assert delay is not None
        assert base <= delay <= base + 0.1
    assert policy.delay(3, rng) is None
    assert policy.delay(0, MidJitter(), retry_after_s=0.01) == pytest.approx(0.15)
    assert policy.delay(0, MidJitter(), retry_after_s=5.0) == 5.0
    assert policy.delay(0, MidJitter(), retry_after_s=5.01) is None
