"""Zero unverified webhooks accepted (specs/06-telephony-telnyx.md §14 item 4): random signatures, timestamps,
and bodies never reach storage. Verification fails before any database access, so the edge runs on an
unreachable database here: any attempt to store would answer ``503`` instead of ``401``.
"""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncIterator

import httpx
import pytest
from edge_helpers import Keypair, metric, probe_edge, signed_headers
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

PLATFORM = Keypair.generate()
_PATHS = st.sampled_from(["/telnyx/voice", "/telnyx/messaging"])


@pytest.fixture
async def edge() -> AsyncIterator[tuple[httpx.AsyncClient, object]]:
    async with probe_edge(PLATFORM) as pair:
        yield pair


_FUZZ = settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])


@_FUZZ
@given(
    body=st.binary(max_size=512),
    signature=st.binary(min_size=0, max_size=80),
    skew=st.integers(min_value=-299, max_value=299),
    path=_PATHS,
)
async def test_random_signatures_are_rejected(
    edge: tuple[httpx.AsyncClient, object], body: bytes, signature: bytes, skew: int, path: str
) -> None:
    client, _app = edge
    headers = {
        "telnyx-signature-ed25519": base64.b64encode(signature).decode("ascii"),
        "telnyx-timestamp": str(int(time.time()) + skew),
    }
    response = await client.post(path, content=body, headers=headers)
    assert response.status_code == 401


@_FUZZ
@given(body=st.binary(min_size=1, max_size=512), flip=st.integers(min_value=0), path=_PATHS)
async def test_any_tampered_byte_is_rejected(
    edge: tuple[httpx.AsyncClient, object], body: bytes, flip: int, path: str
) -> None:
    client, _app = edge
    headers = signed_headers(body, PLATFORM)
    index = flip % len(body)
    tampered = body[:index] + bytes([body[index] ^ 0x01]) + body[index + 1 :]
    response = await client.post(path, content=tampered, headers=headers)
    assert response.status_code == 401


@_FUZZ
@given(text=st.text(max_size=64), path=_PATHS)
async def test_random_header_values_are_rejected(
    edge: tuple[httpx.AsyncClient, object], text: str, path: str
) -> None:
    client, app = edge
    before = metric(app, "unverified", "invalid_signature")
    value = "".join(char for char in text if char.isascii() and char.isprintable())
    headers = {"telnyx-signature-ed25519": value, "telnyx-timestamp": value}
    response = await client.post(path, content=b"{}", headers=headers)
    assert response.status_code == 401
    assert metric(app, "unverified", "invalid_signature") == before + 1
