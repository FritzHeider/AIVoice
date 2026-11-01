"""Metrics (specs/27-observability.md §3.6) and the ``telnyx.<operation>`` span of every operation."""

from __future__ import annotations

import fakeredis
import httpx
import pytest
import respx
from obs_helpers import memory_provider, vendor_metric_rows
from opentelemetry.trace import SpanKind, StatusCode
from prometheus_client import CollectorRegistry
from telnyx_client_helpers import API_KEY, BASE_URL, CCID, RecordingClock, rig

from va_common.metrics import LAT_MS, new_registry
from va_common.token_bucket import BucketLimit
from va_telnyx import (
    CallAlreadyEnded,
    DialRequest,
    TelnyxApiError,
    TelnyxClient,
    TelnyxMetrics,
    TelnyxRateLimited,
    TelnyxRateLimiter,
    TelnyxTransportError,
)
from va_telnyx.metrics import DURATION_METRIC, REQUESTS_METRIC

OK = {"data": {"result": "ok"}}
HANGUP = f"/calls/{CCID}/actions/hangup"


def test_metrics_match_spec_section_3_6() -> None:
    assert vendor_metric_rows("va_telnyx_") == {
        (REQUESTS_METRIC, "counter", ("operation", "outcome")),
        (DURATION_METRIC, "histogram", ("operation",)),
    }
    metrics = TelnyxMetrics(new_registry(process_collectors=False))
    assert metrics.duration._upper_bounds[:-1] == [float(b) for b in LAT_MS]


def _count(registry: CollectorRegistry, operation: str, outcome: str) -> float | None:
    return registry.get_sample_value(REQUESTS_METRIC, {"operation": operation, "outcome": outcome})


async def test_outcomes_and_durations_are_recorded(telnyx_api: respx.MockRouter) -> None:
    registry = new_registry(process_collectors=False)
    telnyx_api.post(HANGUP).mock(side_effect=[httpx.Response(503), httpx.Response(200, json=OK)])
    telnyx_api.post(f"/calls/{CCID}/actions/record_stop").respond(
        422, json={"errors": [{"code": "90018", "title": "ended"}]}
    )
    telnyx_api.post(f"/calls/{CCID}/actions/send_dtmf").respond(
        400, json={"errors": [{"code": "1", "title": "x"}]}
    )
    telnyx_api.post(f"/calls/{CCID}/actions/playback_stop").mock(side_effect=httpx.ConnectError("refused"))
    async with httpx.AsyncClient() as http:
        client = rig(http, metrics=TelnyxMetrics(registry)).client
        assert await client.hangup(CCID, command_id="h1")
        with pytest.raises(CallAlreadyEnded):
            await client.record_stop(CCID, command_id="r", client_state="cw==")
        with pytest.raises(TelnyxApiError):
            await client.send_dtmf(CCID, "1", command_id="d")
        with pytest.raises(TelnyxTransportError):
            await client.playback_stop(CCID, command_id="p")
    assert _count(registry, "hangup", "ok") == 1
    assert _count(registry, "record_stop", "already_ended") == 1
    assert _count(registry, "send_dtmf", "carrier_error") == 1
    assert _count(registry, "playback_stop", "transport_error") == 1
    labels = {"operation": "hangup"}
    assert registry.get_sample_value(f"{DURATION_METRIC}_count", labels) == 1
    assert registry.get_sample_value(f"{DURATION_METRIC}_sum", labels) == pytest.approx(150.0)
    assert registry.get_sample_value(
        f"{DURATION_METRIC}_sum", {"operation": "playback_stop"}
    ) == pytest.approx(1850.0)


async def test_rate_limited_outcome(telnyx_api: respx.MockRouter) -> None:
    registry = new_registry(process_collectors=False)
    telnyx_api.post(HANGUP).respond(200, json=OK)
    limiter = TelnyxRateLimiter(
        fakeredis.FakeAsyncRedis(),
        clock=RecordingClock(),
        max_wait_s=0.0,
        limits={"call_command": BucketLimit(1, 1)},
    )
    async with httpx.AsyncClient() as http:
        client = TelnyxClient(
            API_KEY, BASE_URL, http, limiter, metrics=TelnyxMetrics(registry), clock=RecordingClock()
        )
        await client.hangup(CCID, command_id="h")
        with pytest.raises(TelnyxRateLimited):
            await client.hangup(CCID, command_id="h")
    assert (_count(registry, "hangup", "ok"), _count(registry, "hangup", "rate_limited")) == (1, 1)


async def test_spans(telnyx_api: respx.MockRouter) -> None:
    provider, exporter = memory_provider()
    telnyx_api.post(HANGUP).mock(side_effect=[httpx.Response(503), httpx.Response(200, json=OK)])
    telnyx_api.post(f"/calls/{CCID}/actions/send_dtmf").respond(
        400, json={"errors": [{"code": "1", "title": "x"}]}
    )
    telnyx_api.post("/calls").respond(
        200, json={"data": {"call_control_id": CCID, "call_leg_id": "l", "call_session_id": "s"}}
    )
    async with httpx.AsyncClient() as http:
        client = rig(http, tracer_provider=provider).client
        await client.hangup(CCID, command_id="h1")
        with pytest.raises(TelnyxApiError):
            await client.send_dtmf(CCID, "1", command_id="d1")
        await client.dial(
            DialRequest(
                connection_id="app",
                to="+14155550101",
                from_="+14155552000",
                client_state="cw==",
                command_id="dial-1",
            )
        )
    hangup, dtmf, dial = exporter.get_finished_spans()
    assert (hangup.name, hangup.kind) == ("telnyx.hangup", SpanKind.CLIENT)
    assert dict(hangup.attributes or {}) == {
        "telnyx.operation": "hangup",
        "telnyx.call_control_id": CCID,
        "telnyx.command_id": "h1",
        "telnyx.attempts": 2,
        "telnyx.status_code": 200,
    }
    assert hangup.status.status_code is StatusCode.UNSET
    assert dtmf.status.status_code is StatusCode.ERROR
    assert (dtmf.attributes or {})["telnyx.status_code"] == 400
    assert [event.name for event in dtmf.events] == ["exception"]
    assert dial.name == "telnyx.dial"
    assert "telnyx.call_control_id" not in (dial.attributes or {})
    assert (dial.attributes or {})["telnyx.command_id"] == "dial-1"
    provider.shutdown()
