"""The shared HTTP executor (specs/13 §5 steps 4-6, Decision P07-T07) over respx."""

from __future__ import annotations

import json
import secrets
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import respx
from service_helpers import json_log_stream, log_events
from tools_helpers import API, call, plain_executor

from va_events.webhook_signing import SIGNATURE_HEADER, signature_headers
from va_tools.http import USER_AGENT


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


async def test_json_post_with_headers_and_extraction(api: respx.MockRouter) -> None:
    route = api.post("/lookup").respond(200, json={"account": {"status": "active"}, "items": [1, 2]})
    async with plain_executor() as (executor, _clock):
        outcome = await executor.execute(
            call(
                headers={"Authorization": "Bearer t", "User-Agent": "spoofed"},
                body={"name": "Ada", "n": 1},
                extract=(("status", "$.account.status"), ("items", "$.items[*]"), ("none", "$.nope")),
            )
        )

    request = route.calls.last.request
    assert json.loads(request.content) == {"name": "Ada", "n": 1}
    assert request.headers["authorization"] == "Bearer t"
    assert request.headers["user-agent"] == USER_AGENT
    assert request.headers["x-va-call-id"] == "call-7"
    assert request.headers["content-type"] == "application/json"
    assert (outcome.kind, outcome.status, outcome.attempts, outcome.truncated) == ("ok", 200, 1, False)
    assert outcome.body == {"account": {"status": "active"}, "items": [1, 2]}
    assert outcome.extracted == {"status": "active", "items": [1, 2], "none": None}
    assert outcome.status_class() == "2xx"


async def test_get_sends_no_body_and_no_call_id_without_one(api: respx.MockRouter) -> None:
    route = api.get("/slots").respond(204)
    async with plain_executor(call_id=None) as (executor, _clock):
        outcome = await executor.execute(call("/slots", method="GET", body={"ignored": True}))

    request = route.calls.last.request
    assert request.content == b""
    assert "x-va-call-id" not in request.headers
    assert "content-type" not in request.headers
    assert (outcome.kind, outcome.body) == ("ok", None)


async def test_delete_may_carry_a_body(api: respx.MockRouter) -> None:
    route = api.delete("/item").respond(200, text="gone")
    async with plain_executor() as (executor, _clock):
        outcome = await executor.execute(call("/item", method="DELETE", body=["a"], extract=(("x", "$.x"),)))

    assert json.loads(route.calls.last.request.content) == ["a"]
    assert outcome.body == "gone"
    assert outcome.extracted == {"x": None}


async def test_signature_covers_the_exact_body(api: respx.MockRouter) -> None:
    route = api.post("/lookup").respond(200)
    secret = secrets.token_hex(16)

    def signer(body: bytes) -> dict[str, str]:
        return signature_headers([secret], body, 1_700_000_000)

    async with plain_executor(signer=signer) as (executor, _clock):
        await executor.execute(call(body={"b": [1, 2]}))

    request = route.calls.last.request
    expected = signature_headers([secret], request.content, 1_700_000_000)
    assert request.headers[SIGNATURE_HEADER] == expected[SIGNATURE_HEADER]


async def test_retries_on_5xx_and_timeouts_with_backoff(api: respx.MockRouter) -> None:
    route = api.post("/lookup").mock(
        side_effect=[httpx.Response(503), httpx.ReadTimeout("slow"), httpx.Response(200, json={"ok": True})]
    )
    async with plain_executor() as (executor, clock):
        outcome = await executor.execute(call(retries=3))

    assert route.call_count == 3
    assert clock.sleeps == [0.25, 0.5]
    assert (outcome.kind, outcome.status, outcome.attempts) == ("ok", 200, 3)
    assert outcome.duration_ms == 750


async def test_retries_are_bounded(api: respx.MockRouter) -> None:
    route = api.post("/lookup").respond(
        500,
        json={"error": "boom"},
    )
    async with plain_executor() as (executor, clock):
        outcome = await executor.execute(call(retries=1, extract=(("e", "$.error"),)))

    assert route.call_count == 2
    assert clock.sleeps == [0.25]
    assert (outcome.kind, outcome.status, outcome.attempts) == ("http_error", 500, 2)
    assert outcome.status_class() == "5xx"
    assert outcome.extracted == {"e": "boom"}


async def test_no_retry_on_4xx(api: respx.MockRouter) -> None:
    route = api.post("/lookup").respond(404)
    async with plain_executor() as (executor, clock):
        outcome = await executor.execute(call(retries=3))

    assert route.call_count == 1
    assert clock.sleeps == []
    assert (outcome.kind, outcome.status_class()) == ("http_error", "4xx")


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (httpx.ConnectTimeout("connect"), "timeout"),
        (httpx.ConnectError("refused"), "connection"),
        (httpx.RemoteProtocolError("bad"), "connection"),
        (httpx.InvalidURL("bad"), "invalid_url"),
    ],
)
async def test_transport_failures(api: respx.MockRouter, error: Exception, kind: str) -> None:
    api.post("/lookup").mock(side_effect=error)
    async with plain_executor() as (executor, _clock):
        outcome = await executor.execute(call(retries=1, extract=(("x", "$.x"),)))

    assert outcome.kind == kind
    assert outcome.status is None
    assert outcome.status_class() == "timeout"
    assert outcome.extracted == {"x": None}
    assert outcome.attempts == (2 if kind in {"timeout", "connection"} else 1)


async def test_timeouts_come_from_the_call(api: respx.MockRouter) -> None:
    route = api.post("/lookup").respond(200)
    async with plain_executor() as (executor, _clock):
        await executor.execute(call(timeout_ms=1500))
        await executor.execute(call(timeout_ms=30_000))

    first, second = (entry.request.extensions["timeout"] for entry in route.calls)
    assert first == {"connect": 1.5, "read": 1.5, "write": 1.5, "pool": 1.5}
    assert second == {"connect": 2.0, "read": 30.0, "write": 30.0, "pool": 30.0}


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


async def test_large_bodies_are_truncated(api: respx.MockRouter) -> None:
    api.post("/lookup").mock(return_value=httpx.Response(200, stream=_Chunks(b"a" * 6, b"b" * 6, b"c" * 6)))
    async with plain_executor(max_bytes=10) as (executor, _clock):
        outcome = await executor.execute(call(extract=(("x", "$.x"),)))

    assert outcome.truncated is True
    assert outcome.body == "aaaaaabbbb"
    assert outcome.extracted == {"x": None}


async def test_exact_size_is_not_truncated(api: respx.MockRouter) -> None:
    api.post("/lookup").mock(return_value=httpx.Response(200, stream=_Chunks(b'{"x":', b"1}")))
    async with plain_executor(max_bytes=7) as (executor, _clock):
        outcome = await executor.execute(call(extract=(("x", "$.x"),)))

    assert (outcome.truncated, outcome.body, outcome.extracted) == (False, {"x": 1}, {"x": 1})


async def test_json_without_a_json_content_type_is_parsed(api: respx.MockRouter) -> None:
    api.post("/lookup").respond(200, text='{"x": 2}', headers={"content-type": "text/plain"})
    async with plain_executor() as (executor, _clock):
        outcome = await executor.execute(call(extract=(("x", "$.x"),)))
    assert outcome.extracted == {"x": 2}


@pytest.mark.parametrize(
    ("url", "kind", "allow_http"),
    [
        ("http://api.acme.test/x", "blocked", False),
        ("ftp://api.acme.test/x", "blocked", True),
        ("api.acme.test/x", "invalid_url", True),
        ("https://", "invalid_url", True),
        ("https://[::1/x", "invalid_url", True),
        ("   ", "invalid_url", True),
    ],
)
async def test_preflight_refusals_send_nothing(
    api: respx.MockRouter, url: str, kind: str, allow_http: bool
) -> None:
    async with plain_executor(allow_http=allow_http) as (executor, _clock):
        outcome = await executor.execute(call("", base=url, extract=(("x", "$.x"),)))

    assert api.calls.call_count == 0
    assert (outcome.kind, outcome.attempts, outcome.extracted) == (kind, 0, {"x": None})


async def test_http_is_allowed_when_configured() -> None:
    with respx.mock(base_url="http://local.acme.test") as router:
        router.post("/x").respond(200)
        async with plain_executor(allow_http=True) as (executor, _clock):
            outcome = await executor.execute(call("/x", base="http://local.acme.test"))
    assert outcome.kind == "ok"


async def test_logs_carry_host_but_no_path_query_headers_or_bodies(api: respx.MockRouter) -> None:
    api.post("/secret-path").respond(200, json={"ssn": "123-45-6789"})
    stream = json_log_stream()
    async with plain_executor() as (executor, _clock):
        await executor.execute(
            call("/secret-path?token=abc", headers={"Authorization": "Bearer zzz"}, body={"pin": "9876"})
        )

    text = stream.getvalue()
    for secret in ("secret-path", "token=abc", "zzz", "9876", "123-45-6789"):
        assert secret not in text
    request_logs = [event for event in log_events(stream) if event["event"] == "tools.http_request"]
    assert request_logs[0]["host"] == "api.acme.test"
    assert request_logs[0]["outcome"] == "ok"
