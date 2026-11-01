"""Webhook bodies are capped before verification (specs/06-telephony-telnyx.md §4.1 Decision P05-T01).

The edge runs on an unreachable database: a body that got past the cap would answer ``401``/``503``,
not ``413``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from edge_helpers import Keypair, envelope, metric, probe_edge, signed_headers
from service_helpers import json_log_stream, log_events

from va_edge.intake import MAX_BODY_BYTES

PLATFORM = Keypair.generate()
TOO_LARGE = (413, {"error": "payload_too_large"})


@pytest.fixture
async def edge() -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    async with probe_edge(PLATFORM) as pair:
        yield pair


class Chunks:
    """A chunked request body (no ``Content-Length``) that records how much of it was read."""

    def __init__(self, chunk: bytes, count: int) -> None:
        self.chunk = chunk
        self.count = count
        self.sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self.count):
            self.sent += 1
            yield self.chunk


@pytest.mark.parametrize("path", ["/telnyx/voice", "/telnyx/messaging/0195f0c2-7a51-7c3e-9a4b-2f6d1e8c9b01"])
async def test_declared_oversized_body_is_413(edge: tuple[httpx.AsyncClient, object], path: str) -> None:
    client, app = edge
    body = b"x" * (MAX_BODY_BYTES + 1)
    stream = json_log_stream()
    response = await client.post(path, content=body, headers=signed_headers(body, PLATFORM))
    assert (response.status_code, response.json()) == TOO_LARGE
    assert metric(app, "unverified", "error") == 1
    assert metric(app, "unverified", "invalid_signature") == 0
    assert "telnyx.webhook_too_large" in {line["event"] for line in log_events(stream)}


async def test_chunked_oversized_body_stops_reading_at_the_cap(
    edge: tuple[httpx.AsyncClient, object],
) -> None:
    client, app = edge
    chunk = b"y" * (64 * 1024)
    body = Chunks(chunk, 1000)
    response = await client.post("/telnyx/voice", content=body)
    assert (response.status_code, response.json()) == TOO_LARGE
    assert metric(app, "unverified", "error") == 1
    assert body.sent <= MAX_BODY_BYTES // len(chunk) + 2


async def test_understated_content_length_is_still_capped(edge: tuple[httpx.AsyncClient, object]) -> None:
    client, _app = edge
    body = Chunks(b"z" * (256 * 1024), 8)
    response = await client.post("/telnyx/voice", content=body, headers={"content-length": "10"})
    assert (response.status_code, response.json()) == TOO_LARGE


async def test_body_at_the_cap_reaches_verification(edge: tuple[httpx.AsyncClient, object]) -> None:
    client, app = edge
    body = envelope().ljust(MAX_BODY_BYTES, b" ")
    response = await client.post(
        "/telnyx/voice", content=body, headers=signed_headers(body, Keypair.generate())
    )
    assert (response.status_code, response.json()) == (401, {"error": "invalid_signature"})
    assert metric(app, "unverified", "error") == 0
