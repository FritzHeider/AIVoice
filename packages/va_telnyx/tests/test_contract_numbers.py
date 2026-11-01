"""Contract tests: numbers, messaging, and recording download against FakeTelnyx (specs/06-telephony-telnyx.md
§2.1, §11; specs/21-messaging-sms.md §2 step 6; specs/30-testing-and-evals.md §3.2)."""

from __future__ import annotations

import wave
from pathlib import Path

import httpx
from telnyx_client_helpers import ORG_ID, leg_state, sim_clients
from telnyx_helpers import CALLEE, CALLER, VOICE_URL, RecordingTransport, at, payloads, telnyx_state

from va_sim.state import SimState
from va_telnyx import DialRequest, MessageCreate, NumberSearch, TelnyxClient, number_tags

SMS = "https://edge.example.test/telnyx/messaging"


async def _resources(client: TelnyxClient) -> tuple[str, str]:
    """(call control app id, messaging profile id)."""
    profile = await client.create_outbound_voice_profile("voiceagents-test")
    app = await client.create_call_control_app(
        "voiceagents-test", VOICE_URL, outbound_voice_profile_id=profile.id
    )
    messaging = await client.create_messaging_profile("voiceagents-test", SMS)
    return app.id, messaging.id


async def test_search_order_manage_and_delete_a_number() -> None:
    state = telnyx_state(RecordingTransport())
    async with sim_clients(state) as (_raw, client):
        app_id, profile_id = await _resources(client)
        found = await client.search_numbers(
            NumberSearch(country_code="US", phone_number_type="local", features=("voice", "sms"), limit=3)
        )
        e164 = found[0].phone_number
        order = await client.create_number_order([e164], connection_id=app_id, customer_reference=str(ORG_ID))
        assert order.status == "pending"
        assert await client.get_phone_number_by_e164(e164) is None
        await state.tasks.drain()
        done = await client.get_number_order(order.id)
        owned = await client.get_phone_number_by_e164(e164)
        assert owned is not None
        tagged = await client.update_phone_number(owned.id, connection_id=app_id, tags=number_tags(ORG_ID))
        assigned = await client.update_phone_number_messaging(owned.id, profile_id)
        deleted = await client.delete_phone_number(owned.id)
        gone = await client.get_phone_number_by_e164(e164)
    assert len(found) == 3
    assert {"voice", "sms"} <= set(found[0].features)
    assert found[0].monthly_cost is not None
    assert done.status == "success"
    assert [(n.phone_number, n.status) for n in done.phone_numbers] == [(e164, "success")]
    assert (owned.connection_id, owned.customer_reference) == (app_id, str(ORG_ID))
    assert tagged.tags == ("va", f"org:{ORG_ID}")
    assert assigned.messaging_profile_id == profile_id
    assert deleted.status == "deleted"
    assert gone is None


async def test_a_failing_order_reports_failure() -> None:
    state = telnyx_state(RecordingTransport())
    async with sim_clients(state) as (raw, client):
        app_id, _ = await _resources(client)
        assert (await raw.patch("/sim/telnyx/config", json={"fail_number_orders": True})).status_code == 200
        [number] = await client.search_numbers(NumberSearch(limit=1))
        order = await client.create_number_order(
            [number.phone_number], connection_id=app_id, customer_reference=str(ORG_ID)
        )
        await state.tasks.drain()
        failed = await client.get_number_order(order.id)
    assert failed.status == "failure"


async def test_send_message_from_an_owned_number() -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with sim_clients(state) as (raw, client):
        app_id, profile_id = await _resources(client)
        [number] = await client.search_numbers(NumberSearch(features=("sms",), limit=1))
        await client.create_number_order(
            [number.phone_number],
            connection_id=app_id,
            customer_reference=str(ORG_ID),
            messaging_profile_id=profile_id,
        )
        await state.tasks.drain()
        sent = await client.send_message(
            MessageCreate(
                from_=number.phone_number,
                to=CALLEE,
                text="Your appointment is confirmed.",
                messaging_profile_id=profile_id,
                webhook_url=SMS,
            )
        )
        await state.tasks.drain()
        listed = (await raw.get("/sim/telnyx/messages")).json()["messages"]
    assert sent.direction == "outbound"
    assert sent.from_ == number.phone_number
    assert [(r.phone_number, r.status) for r in sent.to] == [(CALLEE, "queued")]
    assert sent.parts == 1
    assert [m["id"] for m in listed] == [sent.id]
    finalized = [item for item in transport.received if item.event_type == "message.finalized"]
    assert [item.url for item in finalized] == [SMS]


async def _recorded_call(client: TelnyxClient, state: SimState, app_id: str) -> None:
    dial = DialRequest(
        connection_id=app_id,
        to=CALLEE,
        from_=CALLER,
        client_state=leg_state(),
        command_id="dial-rec",
        webhook_url=VOICE_URL,
    )
    ccid = (await client.dial(dial)).call_control_id
    await at(state, 1.0)
    await client.record_start(ccid, command_id="rec-1", client_state=leg_state())
    await at(state, 3.0)
    await client.record_stop(ccid, command_id="rec-stop-1", client_state=leg_state())
    await client.hangup(ccid, command_id="hangup-1")
    await at(state, 4.0)


async def test_download_a_saved_recording(tmp_path: Path) -> None:
    transport = RecordingTransport()
    state = telnyx_state(transport)
    async with sim_clients(state) as (raw, client):
        app_id, _ = await _resources(client)
        await _recorded_call(client, state, app_id)
        [saved] = payloads(transport, "call.recording.saved")
        url = saved["recording_urls"]["wav"]
        target = tmp_path / "raw.wav"
        written = await client.download(url, target)
        served = await raw.get(httpx.URL(url).raw_path.decode())
    assert url.startswith("http://sim/")
    assert written == target.stat().st_size == len(served.content)
    assert target.read_bytes() == served.content
    with wave.open(str(target), "rb") as recording:
        assert recording.getnchannels() == 2
        assert recording.getframerate() == 8000
