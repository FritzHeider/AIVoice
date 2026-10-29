"""``Idempotency-Key`` claims, replays, and conflicts over HTTP (specs/04 §12; specs/31 ``IDEMPOTENCY_*``)."""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import Request
from idempotency_helpers import (
    IDEM_ENVELOPE_PREFIX,
    IDEM_LEGACY_PREFIX,
    IdempotencyContainer,
    idempotency_app,
)
from idempotency_store import MemoryIdempotencyStore
from service_helpers import json_log_stream, log_events, make_client

from va_api.idempotency import (
    IDEMPOTENCY_SCOPE_KEY,
    THREAD_PARSE_BYTES,
    ActiveClaim,
    IdempotencyMiddleware,
    IdempotentReplay,
    canonical_body,
    is_storable_status,
    request_hash,
    validate_key,
)
from va_api.main import CORS_EXPOSE_HEADERS
from va_common.errors import AppError
from va_common.time import FrozenClock
from va_db.repositories import IdempotencyClaim

PREFIXES = [IDEM_LEGACY_PREFIX, IDEM_ENVELOPE_PREFIX]
REPLAYED = "idempotent-replayed"


def _auth(container: IdempotencyContainer, *, key: str | None = None, **overrides: Any) -> dict[str, str]:
    plaintext, _ = container.store.issue(**overrides)
    headers = {"Authorization": f"Bearer {plaintext}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _error(response: httpx.Response) -> dict[str, Any]:
    item: dict[str, Any] = response.json()["errors"][0]
    return item


def _replays(app: Any) -> float:
    value = app.state.metrics.registry.get_sample_value("va_idempotent_replays_total")
    return 0.0 if value is None else float(value)


@pytest.mark.parametrize("raw", ["", "k" * 256, "bad\x7f", "café", "tab\there"])
def test_validate_key_rejects(raw: str) -> None:
    with pytest.raises(AppError) as caught:
        validate_key(raw)
    assert caught.value.to_error_item() == {
        "error": "INVALID_INPUT",
        "message": "Idempotency-Key must be 1-255 printable ASCII characters.",
        "field": "Idempotency-Key",
    }


@pytest.mark.parametrize("raw", ["a", "k" * 255, "with space", "~!@#$%^&*()"])
def test_validate_key_accepts(raw: str) -> None:
    assert validate_key(raw) == raw


def test_canonical_body_ignores_key_order_and_whitespace() -> None:
    assert canonical_body(b'{ "b": 1,\n "a": [1, {"d": 2, "c": "\xc3\xa9"}] }') == (
        '{"a":[1,{"c":"é","d":2}],"b":1}'.encode()
    )


def test_non_json_body_hashed_raw() -> None:
    assert canonical_body(b"name=x&count=1") == b"name=x&count=1"
    assert canonical_body(b"\xff\xfe") == b"\xff\xfe"
    assert canonical_body(b"") == b""


async def test_hash_includes_method_and_path() -> None:
    base = await request_hash("POST", "/v1/a", b'{"x":1}')
    assert base == await request_hash("POST", "/v1/a", b'{ "x" : 1 }')
    assert base != await request_hash("PUT", "/v1/a", b'{"x":1}')
    assert base != await request_hash("POST", "/v1/b", b'{"x":1}')
    assert base != await request_hash("POST", "/v1/a", b'{"x":2}')
    assert len(base) == 64


async def test_large_body_hash_matches_inline_hash() -> None:
    items = [{"i": index, "pad": "x" * 64} for index in range(THREAD_PARSE_BYTES // 64)]
    body = json.dumps({"items": items}).encode()
    assert len(body) > THREAD_PARSE_BYTES
    reordered = json.dumps({"items": items}, indent=2).encode()
    assert await request_hash("POST", "/p", body) == await request_hash("POST", "/p", reordered)


@pytest.mark.parametrize(
    ("status", "stored"), [(200, True), (201, True), (404, True), (422, True), (429, False), (500, False)]
)
def test_storable_statuses(status: int, stored: bool) -> None:
    assert is_storable_status(status) is stored


@pytest.mark.parametrize("prefix", PREFIXES)
async def test_replay_returns_identical_status_and_body(prefix: str) -> None:
    container = IdempotencyContainer()
    app = idempotency_app(container)
    headers = _auth(container, key="order-1")
    async with make_client(app) as client:
        first = await client.post(f"{prefix}/status/201", headers=headers, json={"to": "+15550100"})
        second = await client.post(f"{prefix}/status/201", headers=headers, json={"to": "+15550100"})
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json() == {"n": 1, "code": 201}
    assert REPLAYED not in first.headers
    assert second.headers[REPLAYED] == "true"
    assert second.headers["content-type"] == "application/json"
    assert container.hits["status"] == 1
    assert _replays(app) == 1.0


@pytest.mark.parametrize("prefix", PREFIXES)
async def test_4xx_is_stored_and_replayed(prefix: str) -> None:
    container = IdempotencyContainer()
    headers = _auth(container, key="k-404")
    async with make_client(idempotency_app(container)) as client:
        first = await client.post(f"{prefix}/status/404", headers=headers)
        second = await client.post(f"{prefix}/status/404", headers=headers)
    assert first.status_code == second.status_code == 404
    assert second.json() == first.json() == {"n": 1, "code": 404}
    assert second.headers[REPLAYED] == "true"
    assert container.hits["status"] == 1


@pytest.mark.parametrize("prefix", PREFIXES)
async def test_different_body_returns_422_key_reused(prefix: str) -> None:
    container = IdempotencyContainer()
    headers = _auth(container, key="k-body")
    async with make_client(idempotency_app(container)) as client:
        await client.post(f"{prefix}/status/200", headers=headers, json={"a": 1})
        response = await client.post(f"{prefix}/status/200", headers=headers, json={"a": 2})
    assert response.status_code == 422
    assert _error(response) == {
        "error": "IDEMPOTENCY_KEY_REUSED",
        "message": "Idempotency-Key was already used with a different request.",
    }
    if prefix == IDEM_LEGACY_PREFIX:
        assert response.json()["status"] == "error"
    else:
        assert response.json()["data"] is None
    assert REPLAYED not in response.headers
    assert container.hits["status"] == 1


async def test_same_body_with_reordered_keys_is_replayed() -> None:
    container = IdempotencyContainer()
    headers = {**_auth(container, key="k-order"), "Content-Type": "application/json"}
    async with make_client(idempotency_app(container)) as client:
        await client.post(f"{IDEM_LEGACY_PREFIX}/status/200", headers=headers, content=b'{"a":1,"b":2}')
        response = await client.post(
            f"{IDEM_LEGACY_PREFIX}/status/200", headers=headers, content=b'{ "b": 2, "a": 1 }'
        )
    assert response.headers[REPLAYED] == "true"


async def test_same_key_different_path_is_reused() -> None:
    container = IdempotencyContainer()
    headers = _auth(container, key="k-path")
    async with make_client(idempotency_app(container)) as client:
        await client.post(f"{IDEM_LEGACY_PREFIX}/status/201", headers=headers)
        response = await client.post(f"{IDEM_LEGACY_PREFIX}/status/200", headers=headers)
    assert response.status_code == 422
    assert _error(response)["error"] == "IDEMPOTENCY_KEY_REUSED"


async def test_in_progress_returns_409() -> None:
    container = IdempotencyContainer()
    headers = _auth(container, key="k-gate")
    path = f"{IDEM_ENVELOPE_PREFIX}/gate"
    async with make_client(idempotency_app(container)) as client:
        first = asyncio.create_task(client.post(path, headers=headers))
        await container.entered.wait()
        second = await client.post(path, headers=headers)
        container.gate.set()
        first_response = await first
        third = await client.post(path, headers=headers)
    assert second.status_code == 409
    assert _error(second) == {
        "error": "IDEMPOTENCY_IN_PROGRESS",
        "message": "A request with this Idempotency-Key is still in progress.",
    }
    assert first_response.status_code == 200
    assert third.headers[REPLAYED] == "true"
    assert third.json() == first_response.json() == {"n": 1}
    assert container.hits["gate"] == 1


async def test_concurrent_duplicates_run_endpoint_once() -> None:
    container = IdempotencyContainer()
    headers = _auth(container, key="k-burst")
    async with make_client(idempotency_app(container)) as client:
        responses = await asyncio.gather(
            *(client.post(f"{IDEM_LEGACY_PREFIX}/status/201", headers=headers) for _ in range(10))
        )
    assert container.hits["status"] == 1
    ran = [r for r in responses if r.status_code == 201 and REPLAYED not in r.headers]
    assert len(ran) == 1
    for response in responses:
        assert response.status_code in {201, 409}
        if response.status_code == 201:
            assert response.json() == {"n": 1, "code": 201}


@pytest.mark.parametrize("prefix", PREFIXES)
async def test_5xx_not_stored(prefix: str) -> None:
    container = IdempotencyContainer()
    headers = _auth(container, key="k-503")
    async with make_client(idempotency_app(container)) as client:
        first = await client.post(f"{prefix}/status/503", headers=headers)
        second = await client.post(f"{prefix}/status/503", headers=headers)
    assert first.status_code == second.status_code == 503
    assert second.json() == {"n": 2, "code": 503}
    assert REPLAYED not in second.headers
    assert container.idempotency.rows == {}


async def test_unhandled_exception_releases_claim() -> None:
    container = IdempotencyContainer()
    headers = _auth(container, key="k-boom")
    async with make_client(idempotency_app(container)) as client:
        first = await client.post(f"{IDEM_LEGACY_PREFIX}/boom", headers=headers)
        assert container.idempotency.rows == {}
        second = await client.post(f"{IDEM_LEGACY_PREFIX}/boom", headers=headers)
    assert first.status_code == second.status_code == 500
    assert _error(second)["error"] == "INTERNAL_ERROR"
    assert container.hits["boom"] == 2
    assert container.idempotency.calls == ["claim", "release", "claim", "release"]


async def test_429_not_stored() -> None:
    container = IdempotencyContainer()
    plain = _auth(container, plan_id="start")
    keyed = {**plain, "Idempotency-Key": "k-throttle"}
    path = f"{IDEM_LEGACY_PREFIX}/send"
    async with make_client(idempotency_app(container)) as client:
        for _ in range(4):  # the start plan's calls_send capacity
            assert (await client.post(path, headers=plain)).status_code == 200
        throttled = await client.post(path, headers=keyed)
        assert container.idempotency.rows == {}
        container.clock.advance(1)
        retried = await client.post(path, headers=keyed)
        replayed = await client.post(path, headers=keyed)
    assert throttled.status_code == 429
    assert _error(throttled)["error"] == "RATE_LIMITED"
    assert retried.status_code == 200
    assert REPLAYED not in retried.headers
    assert replayed.headers[REPLAYED] == "true"
    assert container.hits["send"] == 5


async def test_non_json_response_not_stored() -> None:
    container = IdempotencyContainer()
    headers = _auth(container, key="k-text")
    async with make_client(idempotency_app(container)) as client:
        for _ in range(2):
            response = await client.post(f"{IDEM_LEGACY_PREFIX}/text", headers=headers)
            assert response.text == "ok"
            assert REPLAYED not in response.headers
    assert container.hits["text"] == 2
    assert container.idempotency.rows == {}


async def test_body_model_route_parses_after_claim() -> None:
    container = IdempotencyContainer()
    headers = _auth(container, key="k-echo")
    payload = {"name": "lead", "count": 3}
    async with make_client(idempotency_app(container)) as client:
        first = await client.post(f"{IDEM_ENVELOPE_PREFIX}/echo", headers=headers, json=payload)
        second = await client.post(f"{IDEM_ENVELOPE_PREFIX}/echo", headers=headers, json=payload)
    assert first.status_code == 200
    assert first.json() == second.json() == {"name": "lead", "count": 3, "n": 1}
    assert second.headers[REPLAYED] == "true"


async def test_validation_error_400_is_stored() -> None:
    container = IdempotencyContainer()
    headers = _auth(container, key="k-invalid")
    payload = {"name": "lead", "count": "many"}
    async with make_client(idempotency_app(container)) as client:
        first = await client.post(f"{IDEM_ENVELOPE_PREFIX}/echo", headers=headers, json=payload)
        second = await client.post(f"{IDEM_ENVELOPE_PREFIX}/echo", headers=headers, json=payload)
    assert first.status_code == second.status_code == 400
    assert _error(first)["field"] == "count"
    assert second.json() == first.json()  # the body keeps the original request_id
    assert second.headers[REPLAYED] == "true"
    assert second.headers["x-request-id"] != second.json()["request_id"]
    assert container.hits["echo"] == 0


async def test_auth_failure_and_forbidden_create_no_row() -> None:
    container = IdempotencyContainer()
    read_only = _auth(container, key="k-denied", scopes=("calls:read",))
    unauthenticated = {"Authorization": "va_live_" + "x" * 32, "Idempotency-Key": "k-denied"}
    async with make_client(idempotency_app(container)) as client:
        forbidden = await client.post(f"{IDEM_LEGACY_PREFIX}/status/201", headers=read_only)
        denied = await client.post(f"{IDEM_LEGACY_PREFIX}/status/201", headers=unauthenticated)
    assert forbidden.status_code == 403
    assert denied.status_code == 401
    assert container.idempotency.calls == []
    assert container.hits["status"] == 0


@pytest.mark.parametrize("raw", ["", "k" * 256])
async def test_invalid_key_400_invalid_input(raw: str) -> None:
    container = IdempotencyContainer()
    headers = _auth(container, key=raw)
    async with make_client(idempotency_app(container)) as client:
        response = await client.post(f"{IDEM_LEGACY_PREFIX}/status/201", headers=headers)
    assert response.status_code == 400
    assert _error(response) == {
        "error": "INVALID_INPUT",
        "message": "Idempotency-Key must be 1-255 printable ASCII characters.",
        "field": "Idempotency-Key",
    }
    assert container.idempotency.calls == []
    assert container.hits["status"] == 0


async def test_get_and_no_header_ignore_idempotency() -> None:
    container = IdempotencyContainer()
    plain = _auth(container)
    async with make_client(idempotency_app(container)) as client:
        for _ in range(2):
            response = await client.post(f"{IDEM_LEGACY_PREFIX}/status/201", headers=plain)
            assert REPLAYED not in response.headers
        listed = await client.get(
            f"{IDEM_LEGACY_PREFIX}/status/201", headers={**plain, "Idempotency-Key": "g"}
        )
    assert listed.status_code == 405
    assert container.hits["status"] == 2
    assert container.idempotency.calls == []


async def test_keys_are_per_org() -> None:
    container = IdempotencyContainer()
    org_a = _auth(container, key="shared")
    org_b = _auth(container, key="shared")
    async with make_client(idempotency_app(container)) as client:
        first = await client.post(f"{IDEM_LEGACY_PREFIX}/status/201", headers=org_a)
        second = await client.post(f"{IDEM_LEGACY_PREFIX}/status/201", headers=org_b)
    assert first.json() == {"n": 1, "code": 201}
    assert second.json() == {"n": 2, "code": 201}
    assert REPLAYED not in second.headers
    assert len(container.idempotency.rows) == 2


async def test_lock_expiry_allows_takeover() -> None:
    container = IdempotencyContainer()
    plaintext, record = container.store.issue()
    headers = {"Authorization": plaintext, "Idempotency-Key": "k-crashed"}
    path = f"{IDEM_LEGACY_PREFIX}/status/201"
    digest = await request_hash("POST", path, b"")
    abandoned = await container.idempotency.claim(
        record.org_id, key="k-crashed", method="POST", path=path, request_hash=digest
    )
    assert abandoned == IdempotencyClaim("k-crashed")
    async with make_client(idempotency_app(container)) as client:
        locked = await client.post(path, headers=headers)
        container.clock.advance(61)
        taken_over = await client.post(path, headers=headers)
    assert locked.status_code == 409
    assert taken_over.status_code == 201
    assert container.hits["status"] == 1


async def test_expired_row_reclaimed_after_24h() -> None:
    container = IdempotencyContainer()
    headers = _auth(container, key="k-old")
    async with make_client(idempotency_app(container)) as client:
        await client.post(f"{IDEM_LEGACY_PREFIX}/status/201", headers=headers, json={"v": 1})
        container.clock.advance(timedelta(hours=24, seconds=1))
        reused = await client.post(f"{IDEM_LEGACY_PREFIX}/status/201", headers=headers, json={"v": 2})
    assert reused.status_code == 201
    assert reused.json() == {"n": 2, "code": 201}
    assert REPLAYED not in reused.headers


async def test_store_failure_still_returns_response() -> None:
    container = IdempotencyContainer()
    container.idempotency.fail_complete = True
    headers = _auth(container, key="k-store-down")
    stream = json_log_stream()
    async with make_client(idempotency_app(container)) as client:
        response = await client.post(f"{IDEM_LEGACY_PREFIX}/status/201", headers=headers)
    assert response.status_code == 201
    assert response.json() == {"n": 1, "code": 201}
    assert container.idempotency.rows == {}
    assert container.idempotency.calls == ["claim", "complete", "release"]
    (event,) = [e for e in log_events(stream) if e["event"] == "idempotency_store_failed"]
    assert event["operation"] == "complete"
    assert event["status"] == 201
    assert event["error"] == "OSError"
    assert "k-store-down" not in stream.getvalue()


async def test_replay_has_rate_limit_and_request_id_headers() -> None:
    container = IdempotencyContainer()
    headers = _auth(container, key="k-headers")
    async with make_client(idempotency_app(container)) as client:
        first = await client.post(f"{IDEM_LEGACY_PREFIX}/status/201", headers=headers)
        second = await client.post(f"{IDEM_LEGACY_PREFIX}/status/201", headers=headers)
    assert second.headers[REPLAYED] == "true"
    assert second.headers["x-ratelimit-limit"] == first.headers["x-ratelimit-limit"]
    assert int(second.headers["x-ratelimit-remaining"]) == int(first.headers["x-ratelimit-remaining"]) - 1
    assert second.headers["x-request-id"] != first.headers["x-request-id"]
    assert second.headers["x-content-type-options"] == "nosniff"


def test_cors_exposes_idempotent_replayed() -> None:
    assert "Idempotent-Replayed" in CORS_EXPOSE_HEADERS


def _claimed_scope(store: MemoryIdempotencyStore, org_id: uuid.UUID, key: str) -> dict[str, Any]:
    return {
        "type": "http",
        "method": "POST",
        "path": "/raw",
        "headers": [],
        IDEMPOTENCY_SCOPE_KEY: ActiveClaim(org_id, key, store),
    }


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _claimed(store: MemoryIdempotencyStore, key: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    outcome = await store.claim(org_id, key=key, method="POST", path="/raw", request_hash="h")
    assert outcome == IdempotencyClaim(key)
    return org_id


async def test_middleware_releases_claim_when_app_raises_after_start() -> None:
    store = MemoryIdempotencyStore(FrozenClock())
    org_id = await _claimed(store, "k-raw")
    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b'{"part"', "more_body": True})
        msg = "stream broke"
        raise RuntimeError(msg)

    async def send(message: Any) -> None:
        sent.append(message)

    with pytest.raises(RuntimeError, match="stream broke"):
        await IdempotencyMiddleware(app)(_claimed_scope(store, org_id, "k-raw"), _receive, send)
    assert [message["type"] for message in sent] == ["http.response.start", "http.response.body"]
    assert store.rows == {}
    assert store.calls == ["claim", "release"]


async def test_middleware_settles_chunked_json_once() -> None:
    store = MemoryIdempotencyStore(FrozenClock())
    org_id = await _claimed(store, "k-chunks")

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b'{"a":', "more_body": True})
        await send({"type": "http.response.body", "body": b"1}", "more_body": False})

    async def send(message: Any) -> None:
        assert message["type"].startswith("http.response")

    await IdempotencyMiddleware(app)(_claimed_scope(store, org_id, "k-chunks"), _receive, send)
    row = store.rows[org_id, "k-chunks"]
    assert (row.response_status, row.response_body, row.locked_until) == (201, {"a": 1}, None)
    assert store.calls == ["claim", "complete"]


async def test_middleware_logs_release_failure_and_keeps_response() -> None:
    class BrokenRelease(MemoryIdempotencyStore):
        async def release(self, org_id: uuid.UUID, key: str) -> None:
            await super().release(org_id, key)
            msg = "database down"
            raise OSError(msg)

    store = BrokenRelease(FrozenClock())
    org_id = await _claimed(store, "k-down")
    stream = json_log_stream()
    sent: list[int] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 503, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    async def send(message: Any) -> None:
        sent.append(int(message.get("status", 0)))

    await IdempotencyMiddleware(app)(_claimed_scope(store, org_id, "k-down"), _receive, send)
    assert sent == [503, 0]
    (event,) = [e for e in log_events(stream) if e["event"] == "idempotency_store_failed"]
    assert (event["operation"], event["status"], event["error"]) == ("release", 503, "OSError")


async def test_middleware_passes_through_lifespan_and_unclaimed_requests() -> None:
    seen: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])
        if scope["type"] == "http":
            msg = "unclaimed failure"
            raise RuntimeError(msg)

    async def send(message: Any) -> None:
        seen.append(message["type"])

    middleware = IdempotencyMiddleware(app)
    await middleware({"type": "lifespan"}, _receive, send)
    with pytest.raises(RuntimeError, match="unclaimed failure"):
        await middleware({"type": "http", "method": "POST", "path": "/x", "headers": []}, _receive, send)
    assert seen == ["lifespan", "http"]


async def test_replay_handler_reraises_other_exceptions() -> None:
    container = IdempotencyContainer()
    app = idempotency_app(container)
    handler = app.exception_handlers[IdempotentReplay]
    request = Request({"type": "http", "method": "POST", "path": "/x", "headers": [], "app": app})
    with pytest.raises(LookupError, match="not a replay"):
        result = handler(request, LookupError("not a replay"))
        assert inspect.isawaitable(result)
        await result


@pytest.mark.parametrize("size", [0, THREAD_PARSE_BYTES + 1])
async def test_middleware_empty_body_released_large_body_stored(size: int) -> None:
    store = MemoryIdempotencyStore(FrozenClock())
    org_id = await _claimed(store, "k-size")
    body = json.dumps("x" * max(size - 2, 0)).encode() if size else b""

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": body})

    async def send(message: Any) -> None:
        assert message["type"].startswith("http.response")

    await IdempotencyMiddleware(app)(_claimed_scope(store, org_id, "k-size"), _receive, send)
    if size:
        assert store.rows[org_id, "k-size"].response_body == "x" * (size - 2)
    else:
        assert store.rows == {}
