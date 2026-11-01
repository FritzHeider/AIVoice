"""Recording download: absolute URL without auth, streaming to a file, retries, size cap, cleanup
(specs/06-telephony-telnyx.md §2.1 ``download``, §11 step 2; Decision 2026-11-01 P03-T08b rule 5)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import respx
from prometheus_client import CollectorRegistry
from telnyx_client_helpers import rig

from va_telnyx import (
    CallAlreadyEnded,
    TelnyxApiError,
    TelnyxDownloadTooLarge,
    TelnyxMetrics,
    TelnyxTransportError,
)

URL = "https://recordings.telnyx.test/abc/recording.wav?X-Amz-Signature=sig"
BODY = b"RIFF" + bytes(range(256)) * 64


class _Broken(httpx.AsyncByteStream):
    """A body that fails after its first chunk."""

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield b"partial-bytes"
        reason = "connection reset"
        raise httpx.ReadError(reason)


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk


async def test_streams_to_the_file_without_auth(tmp_path: Path) -> None:
    target = tmp_path / "raw.wav"
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(URL).respond(200, content=BODY)
        async with httpx.AsyncClient() as http:
            written = await rig(http).client.download(URL, target)
    assert written == len(BODY)
    assert target.read_bytes() == BODY
    request = route.calls[0].request
    assert "authorization" not in request.headers
    assert str(request.url) == URL


async def test_503_then_200_keeps_only_the_second_body(tmp_path: Path) -> None:
    target = tmp_path / "raw.wav"
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(URL).mock(
            side_effect=[httpx.Response(503, content=b"busy"), httpx.Response(200, content=BODY)]
        )
        async with httpx.AsyncClient() as http:
            harness = rig(http)
            assert await harness.client.download(URL, target) == len(BODY)
    assert route.call_count == 2
    assert [round(s, 6) for s in harness.clock.sleeps] == [0.15]
    assert target.read_bytes() == BODY


async def test_a_mid_body_read_error_is_retried_and_the_file_truncated(tmp_path: Path) -> None:
    target = tmp_path / "raw.wav"
    with respx.mock(assert_all_mocked=True) as router:
        router.get(URL).mock(
            side_effect=[
                httpx.Response(200, stream=_Broken()),
                httpx.Response(200, stream=_Chunks(b"ab", b"cd")),
            ]
        )
        async with httpx.AsyncClient() as http:
            assert await rig(http).client.download(URL, target) == 4
    assert target.read_bytes() == b"abcd"


async def test_404_is_a_carrier_error_and_leaves_no_file(tmp_path: Path) -> None:
    target = tmp_path / "raw.wav"
    target.write_bytes(b"stale")
    error_body = {"errors": [{"code": "10005", "title": "Resource not found"}]}
    with respx.mock(assert_all_mocked=True) as router:
        router.get(URL).respond(404, json=error_body)
        async with httpx.AsyncClient() as http:
            with pytest.raises(TelnyxApiError) as caught:
                await rig(http).client.download(URL, target)
    assert not isinstance(caught.value, CallAlreadyEnded)
    assert caught.value.status == 404
    assert caught.value.errors[0].code == "10005"
    assert not target.exists()


async def test_a_large_error_body_is_read_only_partly(tmp_path: Path) -> None:
    target = tmp_path / "raw.wav"
    huge = b"x" * (200 * 1024)
    with respx.mock(assert_all_mocked=True) as router:
        router.get(URL).respond(403, content=huge)
        async with httpx.AsyncClient() as http:
            with pytest.raises(TelnyxApiError) as caught:
                await rig(http).client.download(URL, target)
    assert caught.value.errors[0].code == "http_403"
    assert caught.value.errors[0].detail == "x" * 200


async def test_exceeding_max_bytes_raises_and_removes_the_file(tmp_path: Path) -> None:
    target = tmp_path / "raw.wav"
    registry = CollectorRegistry()
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(URL).mock(return_value=httpx.Response(200, stream=_Chunks(b"a" * 600, b"b" * 600)))
        async with httpx.AsyncClient() as http:
            with pytest.raises(TelnyxDownloadTooLarge) as caught:
                await rig(http, metrics=TelnyxMetrics(registry)).client.download(URL, target, max_bytes=1000)
    assert route.call_count == 1
    assert not target.exists()
    error = caught.value.to_app_error()
    assert error.code == "CARRIER_ERROR"
    assert error.details == {"telnyx_errors": [], "max_bytes": 1000}
    labels = {"operation": "download", "outcome": "carrier_error"}
    assert registry.get_sample_value("va_telnyx_requests_total", labels) == 1.0


async def test_transport_failure_after_retries_removes_the_file(tmp_path: Path) -> None:
    target = tmp_path / "raw.wav"
    with respx.mock(assert_all_mocked=True) as router:
        route = router.get(URL).mock(side_effect=httpx.ReadError("reset"))
        async with httpx.AsyncClient() as http:
            with pytest.raises(TelnyxTransportError):
                await rig(http).client.download(URL, target)
    assert route.call_count == 4
    assert not target.exists()


async def test_a_relative_url_is_rejected(tmp_path: Path) -> None:
    async with httpx.AsyncClient() as http:
        with pytest.raises(ValueError, match="absolute URL"):
            await rig(http).client.download("/recordings/abc.wav", tmp_path / "raw.wav")
    assert not (tmp_path / "raw.wav").exists()
