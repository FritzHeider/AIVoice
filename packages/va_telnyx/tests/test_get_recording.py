"""``get_recording`` (specs/06-telephony-telnyx.md §2.1; §11 Decision P05-T14 rule 7): path, parsing, errors,
and the contract against FakeTelnyx."""

from __future__ import annotations

import httpx
import pytest
import respx
from telnyx_client_helpers import rig, sim_clients
from telnyx_helpers import telnyx_state

from va_telnyx import TelnyxApiError


async def test_reads_the_fresh_download_urls(telnyx_api: respx.MockRouter) -> None:
    route = telnyx_api.get("/recordings/rec%2F1").respond(
        200,
        json={
            "data": {
                "id": "rec/1",
                "record_type": "recording",
                "channels": "dual",
                "download_urls": {"wav": "https://s3.example.test/rec.wav?sig=x", "mp3": None},
                "duration_millis": 4200,
            }
        },
    )
    async with httpx.AsyncClient() as http:
        recording = await rig(http).client.get_recording("rec/1")
    assert (recording.id, recording.channels) == ("rec/1", "dual")
    assert recording.download_urls.wav == "https://s3.example.test/rec.wav?sig=x"
    assert recording.download_urls.mp3 is None
    assert route.calls[0].request.headers["authorization"].startswith("Bearer ")


async def test_missing_download_urls_parse_as_none(telnyx_api: respx.MockRouter) -> None:
    telnyx_api.get("/recordings/r").respond(200, json={"data": {"id": "r"}})
    async with httpx.AsyncClient() as http:
        recording = await rig(http).client.get_recording("r")
    assert recording.download_urls.wav is None


async def test_a_missing_recording_raises_404(telnyx_api: respx.MockRouter) -> None:
    telnyx_api.get("/recordings/gone").respond(
        404, json={"errors": [{"code": "10005", "title": "Resource not found"}]}
    )
    async with httpx.AsyncClient() as http:
        with pytest.raises(TelnyxApiError) as caught:
            await rig(http).client.get_recording("gone")
    assert caught.value.status == 404


async def test_contract_get_against_fake_telnyx() -> None:
    state = telnyx_state()
    state.store_recording("rec-contract", b"RIFF")
    async with sim_clients(state) as (raw, client):
        recording = await client.get_recording("rec-contract")
        assert recording.download_urls.wav is not None
        assert (await raw.get(recording.download_urls.wav)).content == b"RIFF"
