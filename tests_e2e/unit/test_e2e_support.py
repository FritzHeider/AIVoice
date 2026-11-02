"""Pure parts of the e2e harness: the assertion evaluator, scenario/rule rewriting, and helpers (no stack)."""

from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Any

import phonenumbers
import pytest
import yaml
from e2e_assertions import (
    alternating_roles,
    evaluate,
    event_present,
    latency_summaries,
    nearest_rank,
    turn_overheads,
)
from e2e_harness import (
    SCENARIOS_DIR,
    RegisteredScenario,
    StackUrls,
    prepare_rules,
    prepare_scenario,
    random_e164,
    scenario_marker,
    wav_channels,
)

from va_sim.llm.rules import load_rules
from va_sim.scenario import Assertions, ScenarioError

SCENARIO_FILES = sorted(SCENARIOS_DIR.glob("*.yaml"))
RULE_FILES = sorted((SCENARIOS_DIR / "rules").glob("*.yaml"))


def turn(user: str, text: str) -> dict[str, Any]:
    return {"user": user, "text": text}


def event(category: str, message: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"category": category, "message": message, "data": data or {}}


def good_call() -> dict[str, Any]:
    return {
        "status": "completed",
        "end_reason": "agent_end_call",
        "answered_by": "human",
        "transcripts": [turn("assistant", "Hi"), turn("user", "This is Ana"), turn("assistant", "Bye")],
        "variables": {"name": "Ana", "party": 4},
    }


def timeline(*latencies: int | None) -> dict[str, Any]:
    return {"utterances": [{"latency_ms": value} for value in latencies]}


COMPLETED = event("call", "Call completed.")


def test_nearest_rank() -> None:
    assert nearest_rank([5], 95) == 5
    assert nearest_rank([1, 2, 3, 4], 50) == 2
    assert nearest_rank(range(1, 21), 95) == 19
    assert nearest_rank([3, 1, 2], 100) == 3
    with pytest.raises(ValueError, match="at least one value"):
        nearest_rank([], 95)


def test_alternating_roles() -> None:
    assert alternating_roles([turn("assistant", "a"), turn("user", "b"), turn("assistant", "c")])
    assert alternating_roles([turn("assistant", "a"), turn("agent-action", "x"), turn("user", "b")])
    assert not alternating_roles([turn("assistant", "a"), turn("assistant", "b")])
    assert alternating_roles([])


def test_event_presence_by_message_or_unique_category() -> None:
    events = [COMPLETED, event("latency", "E2E: 900ms"), event("call", "Call ended.")]
    assert event_present("call.completed", events) is True
    assert event_present("call.ended", events) is True
    assert event_present("call.latency", events) is True
    assert event_present("call.interrupt", events) is False
    assert event_present("call.completed", events[1:]) is False
    # `call.answered` shares the `call` category with other types and has no fixed message.
    assert event_present("call.answered", events) is None
    assert event_present("call.nonexistent", events) is None


def test_latency_summaries_and_overheads() -> None:
    summary = {"e2e_ms": 950, "eot_ms": 350, "stt_final_ms": 40, "llm_ttft_ms": 250, "tts_ttfa_ms": 250}
    partial = {"e2e_ms": 700, "eot_ms": 350, "llm_ttft_ms": 250}
    events = [
        event("latency", "E2E: 950ms", summary),
        event("latency", "STT: 40ms", {"stt_final_ms": 40}),
        event("call", "E2E: not a latency event"),
        event("latency", "E2E: 700ms", partial),
    ]
    assert latency_summaries(events) == [summary, partial]
    overheads = turn_overheads(timeline(1000, None, 800), events)
    assert [(t.sim_ms, t.vendor_ms, t.overhead_ms) for t in overheads] == [(1000, 890, 110), (800, 600, 200)]
    assert "overhead_ms=110" in repr(overheads[0])


def test_evaluate_passes_a_matching_call() -> None:
    assertions = Assertions(
        call_status="completed",
        answered_by="human",
        end_reason="agent_end_call",
        min_turns=3,
        transcript_contains=("ana",),
        variables={"name": "Ana"},
        max_e2e_ms_p95=1200,
        webhook_events=("call.completed",),
    )
    assert evaluate(assertions, good_call(), [COMPLETED], timeline(900, 1100, None)) == []
    assert evaluate(Assertions(), {}, [], timeline()) == []


def test_evaluate_reports_every_failed_check() -> None:
    assertions = Assertions(
        call_status="failed",
        answered_by="machine",
        end_reason="max_duration",
        min_turns=5,
        transcript_contains=("Tomorrow",),
        variables={"name": "Bob"},
        max_e2e_ms_p95=1000,
        webhook_events=("call.completed", "call.answered"),
    )
    failures = evaluate(assertions, good_call(), [], timeline(900, 1300))
    assert failures == [
        "status: expected 'failed', got 'completed'",
        "end_reason: expected 'max_duration', got 'agent_end_call'",
        "answered_by: expected 'voicemail', got 'human'",
        "min_turns: expected at least 5, got 3",
        "transcript_contains: 'Tomorrow' not in the transcript",
        "variables.name: expected 'Bob', got 'Ana'",
        "max_e2e_ms_p95: p95 1300 ms > 1000 ms ([900, 1300])",
        "webhook_events: no 'call.completed' event",
        "webhook_events: 'call.answered' cannot be identified in the event log",
    ]


def test_evaluate_needs_a_measured_latency_for_the_p95() -> None:
    failures = evaluate(Assertions(max_e2e_ms_p95=1200), good_call(), [], timeline(None))
    assert failures == ["max_e2e_ms_p95: the timeline has no measured utterance latency"]


@pytest.mark.parametrize("path", SCENARIO_FILES, ids=lambda path: path.stem)
def test_every_scenario_rewrites_to_a_run_unique_id_and_number(path: Path) -> None:
    number = random_e164()
    scenario = prepare_scenario(
        path.read_text(encoding="utf-8"), name=path.stem, run="ab12cd34", number=number
    )
    assert scenario.id == f"{path.stem}-ab12cd34"
    registered = RegisteredScenario(scenario=scenario, rule_ids=())
    assert registered.number == number
    assert registered.marker == f"[[SCENARIO:{path.stem}-ab12cd34]]"
    assert all((SCENARIOS_DIR / rule).is_file() for rule in scenario.llm_rules)


def test_an_inbound_scenario_keeps_its_inbound_match() -> None:
    text = (SCENARIOS_DIR / "inbound_basic.yaml").read_text(encoding="utf-8")
    scenario = prepare_scenario(text, name="inbound_basic", run="t", number="+14155550199")
    assert (scenario.match.inbound_from, scenario.match.to) == ("+14155550199", None)


def test_an_invalid_scenario_fails_with_its_position() -> None:
    with pytest.raises(ScenarioError, match=r"broken\.yaml:1:1"):
        prepare_scenario("id: x\n", name="broken", run="t", number="+14155550199")


@pytest.mark.parametrize("path", RULE_FILES, ids=lambda path: path.stem)
def test_every_rule_file_loads_once_substituted(path: Path) -> None:
    marker = scenario_marker("flow-ab12cd34")
    text = prepare_rules(path.read_text(encoding="utf-8"), run="ab12cd34", marker=marker)
    assert "{{" not in text
    rules = load_rules(text, source=path.name).rules
    assert rules
    assert all(rule.id.endswith("-ab12cd34") for rule in rules)
    assert all(marker in rule.match.system_contains for rule in rules)
    assert yaml.safe_load(text)["rules"]


def test_random_numbers_are_valid_and_in_the_area_code() -> None:
    numbers = {random_e164("628") for _ in range(20)}
    assert all(number.startswith("+1628") for number in numbers)
    assert all(phonenumbers.is_valid_number(phonenumbers.parse(number)) for number in numbers)


def test_wav_channels() -> None:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(2)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\x00\x00" * 2 * 160)
    assert wav_channels(buffer.getvalue()) == (2, 160)


def test_stack_urls_come_from_the_environment() -> None:
    defaults = StackUrls.from_env({})
    assert defaults.api == "http://localhost:8000"
    assert defaults.probes() == [
        "http://localhost:8000/readyz",
        "http://localhost:8100/readyz",
        "http://localhost:8200/readyz",
        "http://localhost:9100/sim/state",
    ]
    custom = StackUrls.from_env(
        {"E2E_API_URL": "http://api.test:8000/", "E2E_SIM_URL": "http://sim.test:9100"}
    )
    assert (custom.api, custom.telnyx) == ("http://api.test:8000", "http://sim.test:9100")
