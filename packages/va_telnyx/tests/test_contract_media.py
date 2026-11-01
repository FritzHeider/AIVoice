"""Contract tests: the media WebSocket protocol of FakeTelnyx parsed and driven with ``va_telnyx.media`` and
``verify_media_ws`` (specs/06 §4.2, §8; specs/30 §3.2).

FakeTelnyx builds its frames from the spec on its own (``va_sim.telnyx.media_frames``), so a parsing mismatch
here means one side drifted from specs/06 §8.
"""

from __future__ import annotations

import orjson
import pytest
from media_agent import MediaAgent
from telnyx_helpers import RecordingTransport, answered_call, telnyx_client, telnyx_state

from va_telnyx import MediaFrame, WebhookVerificationError, parse_media_message, verify_media_ws
from va_telnyx.media import (
    MediaConnected,
    MediaDtmf,
    MediaMark,
    MediaStart,
    MediaStop,
    clear_message,
    mark_message,
    media_message,
    parse_segment_mark,
    segment_mark,
)

STREAM_AUTH = "stream-token-1"


async def test_handshake_frames_and_agent_messages(media_agent: MediaAgent) -> None:
    state = telnyx_state(RecordingTransport())
    media_agent.clock = state.clock
    async with telnyx_client(state) as client:
        _, ccid = await answered_call(
            client, state, stream_url=media_agent.url, stream_auth_token=STREAM_AUTH
        )
        await media_agent.wait_media(3)

        headers = media_agent.headers
        assert headers is not None
        public_key = state.signing_key.public_key_b64
        verify_media_ws(media_agent.url, headers, public_key, state.clock.now())
        assert headers["authorization"] == f"Bearer {STREAM_AUTH}"
        with pytest.raises(WebhookVerificationError) as other_url:
            verify_media_ws(
                media_agent.url.replace("call_id=1", "call_id=2"), headers, public_key, state.clock.now()
            )
        assert other_url.value.reason == "invalid_signature"

        parsed = [parse_media_message(orjson.dumps(frame)) for frame in media_agent.frames]
        connected, start, *rest = parsed
        assert isinstance(connected, MediaConnected)
        assert isinstance(start, MediaStart)
        assert start.start.call_control_id == ccid
        assert (start.start.media_format.encoding, start.start.media_format.sample_rate) == ("PCMU", 8000)
        frames = [item for item in rest if isinstance(item, MediaFrame)]
        assert len(frames) >= 3
        assert [frame.chunk for frame in frames] == list(range(1, len(frames) + 1))
        assert all(len(frame.payload) == 160 for frame in frames)
        assert frames[1].timestamp - frames[0].timestamp == 20

        response = await client.post(f"/sim/telnyx/calls/{ccid}/dtmf", json={"digits": "7"})
        assert response.status_code < 300, response.text
        await media_agent.wait_for(lambda agent: bool(agent.of("dtmf")))
        [dtmf] = [parse_media_message(orjson.dumps(frame)) for frame in media_agent.of("dtmf")]
        assert isinstance(dtmf, MediaDtmf)
        assert dtmf.dtmf.digit == "7"

        await media_agent.send_raw(media_message(b"\xff" * 160).decode())
        await media_agent.send_raw(mark_message(segment_mark(1, 0)).decode())
        await media_agent.wait_for(lambda agent: bool(agent.of("mark")))
        [mark] = [parse_media_message(orjson.dumps(frame)) for frame in media_agent.of("mark")]
        assert isinstance(mark, MediaMark)
        assert parse_segment_mark(mark.mark.name) == (1, 0)
        assert mark.stream_id == start.stream_id

        await media_agent.send_raw(clear_message().decode())
        await media_agent.wait_media(len(media_agent.media()) + 3)
        detail = (await client.get(f"/sim/telnyx/calls/{ccid}")).json()["stream"]
        assert detail["received"] == {"media": 1, "mark": 1, "clear": 1}
        assert media_agent.of("error") == []

        response = await client.post(f"/v2/calls/{ccid}/actions/hangup", json={})
        assert response.status_code == 200, response.text
        await media_agent.wait_closed()
        [stop] = [parse_media_message(orjson.dumps(frame)) for frame in media_agent.of("stop")]
        assert isinstance(stop, MediaStop)
        assert stop.stop.call_control_id == ccid
