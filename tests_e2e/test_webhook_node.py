"""``webhook_node`` (specs/30 §5; Decision P07-T12c): a pathway Webhook node posts to the receiver from the
media worker, extracts ``status``/``ref`` from the JSON answer with ``responseData``, and speaks them in its
reply before waiting for the caller, who then says goodbye."""

from __future__ import annotations

from typing import Final

import pytest
from e2e_harness import Harness
from e2e_pathways import route_targets, run_pathway_call, webhook_graph, webhook_node_data
from e2e_receiver import Reply, ServedReceiver

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]

CONFIRMED: Final = "Your booking is confirmed, reference A1."
RECEIPT: Final = {"status": "confirmed", "ref": "A1"}


async def test_webhook_node(harness: Harness, receiver: ServedReceiver) -> None:
    org = await harness.create_org()
    scenario = await harness.load_scenario("webhook_node")
    path = f"/confirm/{harness.run_token}"
    receiver.receiver.script(path, Reply(200, RECEIPT))

    graph = webhook_graph(f"{receiver.url}{path}")
    finished = await run_pathway_call(harness, org, scenario, name="Booking confirmation", graph=graph)

    assert not finished.failures(scenario), (finished.call, finished.timeline)
    [request] = receiver.receiver.of(path)
    assert request.json() == {"call": finished.call_id}
    turns = [(turn["user"], turn["text"]) for turn in finished.call["transcripts"]]
    assert turns[1:3] == [("user", "Yes, please confirm my booking."), ("assistant", CONFIRMED)], turns
    assert CONFIRMED in await harness.tts_texts()
    webhook = webhook_node_data(finished.events)
    assert (webhook["node_id"], webhook["outcome"], webhook["status_code"]) == ("confirm", "ok", 200), webhook
    assert set(webhook["variables_set"]) == set(RECEIPT), webhook
    assert finished.call["variables"].items() >= RECEIPT.items(), finished.call["variables"]
    assert route_targets(finished.events) == ["confirm", "bye"], finished.events
