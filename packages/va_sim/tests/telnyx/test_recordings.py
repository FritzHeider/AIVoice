"""``DELETE /v2/recordings/{id}`` (specs/06 §2.1 ``delete_recording``; specs/30 §3.2 Decision P05-T14)."""

from __future__ import annotations

from telnyx_helpers import error_of, telnyx_client, telnyx_state


async def test_delete_removes_the_stored_wav() -> None:
    state = telnyx_state()
    state.store_recording("rec-1", b"RIFF-wav-bytes")
    async with telnyx_client(state) as client:
        assert (await client.get("/sim/recordings/rec-1.wav")).status_code == 200
        response = await client.delete("/v2/recordings/rec-1")
        assert response.status_code == 200
        body = response.json()["data"]
        assert (body["id"], body["record_type"], body["status"]) == ("rec-1", "recording", "deleted")
        assert (await client.get("/sim/recordings/rec-1.wav")).status_code == 404
        assert "rec-1" not in state.recordings
        again = await client.delete("/v2/recordings/rec-1")
        assert error_of(again)[0] == 404


async def test_unknown_recording_is_404_and_auth_is_required() -> None:
    state = telnyx_state()
    state.store_recording("rec-2", b"RIFF")
    async with telnyx_client(state) as client:
        assert error_of(await client.delete("/v2/recordings/missing"))[0] == 404
    async with telnyx_client(state, api_key="not-a-key") as stranger:
        assert (await stranger.delete("/v2/recordings/rec-2")).status_code == 401
    assert "rec-2" in state.recordings
