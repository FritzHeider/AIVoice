"""``pathway_restaurant_template`` (specs/30 §5; Decision P07-T12c): an outbound call runs the bundled
restaurant reservation template (``tests_e2e/pathways/restaurant_template.json``) on its published version.

The caller asks for a table, so the router (FakeLLM's edge-label heuristic) moves Greeting → Take
Reservation; the caller's name answers it, the extraction (a FakeLLM rule keyed on ``extract_variables`` and
that name) fills ``party_size``/``time``/``name``, the ``alwaysPick`` edge enters Book Table, whose Webhook
node posts the values to the receiver and routes on the 2xx to Goodbye, which speaks the templated
confirmation and hangs up.
"""

from __future__ import annotations

from typing import Final

import pytest
from e2e_assertions import REQUIRED_CALL_EVENTS, alternating_roles, event_order_failures, event_types
from e2e_harness import Harness
from e2e_pathways import (
    events_of,
    load_template,
    route_targets,
    run_pathway_call,
    webhook_node_data,
    with_webhook_url,
)
from e2e_receiver import Reply, ServedReceiver

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]

OPENER: Final = "Thank you for calling Bella Pasta, this is Ava. How can I help you today?"
CONFIRMATION: Final = "You are all set, Marisol Quintana. Your table for 4 at 7pm is booked. Goodbye!"
BOOKING: Final = {"party_size": 4, "time": "7pm", "name": "Marisol Quintana"}
RECEIPT: Final = {"status": "booked", "ref": "R7"}
# The call is not recorded, so no ``call.recording_ready``.
UNRECORDED_EVENTS: Final = REQUIRED_CALL_EVENTS - {"call.recording_ready"}


async def test_pathway_restaurant_template(harness: Harness, receiver: ServedReceiver) -> None:
    org = await harness.create_org()
    scenario = await harness.load_scenario("pathway_restaurant_template")
    path = f"/bookings/{harness.run_token}"
    receiver.receiver.script(path, Reply(200, RECEIPT))
    graph = with_webhook_url(load_template("restaurant_template"), f"{receiver.url}{path}")

    finished = await run_pathway_call(harness, org, scenario, name="Bella Pasta reservations", graph=graph)

    assert not finished.failures(scenario), (finished.call, finished.timeline)
    transcripts = finished.call["transcripts"]
    assert (transcripts[0]["user"], transcripts[0]["text"]) == ("assistant", OPENER)
    assert (transcripts[-1]["user"], transcripts[-1]["text"]) == ("assistant", CONFIRMATION)
    assert alternating_roles(transcripts), transcripts
    assert route_targets(finished.events) == ["reserve", "book", "goodbye"], finished.events
    (extraction,) = events_of(finished.events, "call.extraction")
    assert extraction["data"]["node_id"] == "reserve", extraction
    assert finished.call["variables"].items() >= {**BOOKING, **RECEIPT}.items(), finished.call["variables"]

    [booked] = receiver.receiver.of(path)
    assert booked.json() == BOOKING
    webhook = webhook_node_data(finished.events)
    assert (webhook["node_id"], webhook["outcome"], webhook["status_code"]) == ("book", "ok", 200), webhook
    assert set(webhook["variables_set"]) == set(RECEIPT), webhook
    assert webhook["route_to"] == "goodbye", webhook
    assert CONFIRMATION in await harness.tts_texts()
    types = event_types(finished.events)
    failures = event_order_failures(types, UNRECORDED_EVENTS)
    assert not failures, (failures, types)
