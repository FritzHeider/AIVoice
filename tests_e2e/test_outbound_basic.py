"""``outbound_basic`` (specs/30 §5): a human answers, a short task-mode exchange, the agent ends the call."""

from __future__ import annotations

from typing import Any, Final

import pytest
from e2e_assertions import (
    CALL_OBJECT_FIELDS,
    alternating_roles,
    event_order_failures,
    event_types,
    latency_summaries,
    nearest_rank,
    turn_overheads,
)
from e2e_harness import FinishedCall, Harness, wav_channels

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]

FIRST_SENTENCE: Final = "Hi, this is Maya from Bella Pasta calling about your reservation."
TASK: Final = "You are Maya from Bella Pasta. Ask when the caller would like to come in, then end the call."
OVERHEAD_P95_MS: Final = 250


def user_turns(call: dict[str, Any]) -> list[dict[str, Any]]:
    return [turn for turn in call["transcripts"] if turn["user"] == "user"]


def check_transcript(call: dict[str, Any]) -> None:
    assert set(call) >= CALL_OBJECT_FIELDS, sorted(CALL_OBJECT_FIELDS - set(call))
    transcripts = call["transcripts"]
    assert (transcripts[0]["user"], transcripts[0]["text"]) == ("assistant", FIRST_SENTENCE)
    assert alternating_roles(transcripts), transcripts


def check_latency(finished: FinishedCall) -> None:
    """One latency summary per caller turn; platform overhead p95 within budget (specs/30 §5)."""
    turns = user_turns(finished.call)
    summaries = latency_summaries(finished.events)
    assert len(summaries) == len(turns), (summaries, turns)
    overheads = turn_overheads(finished.timeline, finished.events)
    assert len(overheads) == len(turns), (overheads, finished.timeline["utterances"])
    print(f"outbound_basic latency: {summaries} {overheads}")  # noqa: T201
    assert nearest_rank([turn.overhead_ms for turn in overheads], 95) <= OVERHEAD_P95_MS, overheads


async def test_outbound_basic(harness: Harness) -> None:
    org = await harness.create_org()
    scenario = await harness.load_scenario("outbound_basic")
    call_id = await harness.send_task_call(org, scenario, TASK, first_sentence=FIRST_SENTENCE, record=True)
    finished = await harness.finished(org, call_id, number=scenario.number)

    assert not finished.failures(scenario), (finished.call, finished.timeline)
    check_transcript(finished.call)
    assert finished.call["recording_url"], finished.call
    channels, frames = wav_channels(await harness.recording_wav(finished.call["recording_url"]))
    assert (channels, frames > 0) == (2, True)
    check_latency(finished)
    types = event_types(finished.events)
    assert not event_order_failures(types), (event_order_failures(types), types)
