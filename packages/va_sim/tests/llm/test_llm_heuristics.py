"""FakeLLM structured-call heuristics (specs/30 §3.4; Decision P03-T06b rules 1-5)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import jsonschema
import orjson
import pytest
from llm_helpers import add_rules, chat_body, llm_client, llm_state
from llm_schemas import (
    ANALYSIS_SCHEMA,
    ANSWERS_SCHEMA,
    DISPOSITION_SCHEMA,
    DISPOSITION_SYSTEM,
    GUARD_RAILS_SCHEMA,
    ROUTER_SCHEMA,
    SENTIMENT_SCHEMA,
    SPEC_CALLS,
    SpecCall,
    forced_body,
    router_system,
    router_user,
    transcript_user,
)

from va_sim.llm.heuristics import STRUCTURED_TOOL_NAMES, TOOL_ALIASES, structured_arguments, summary_reply
from va_sim.llm.params import ChatRequest

SPECS = Path(__file__).resolve().parents[4] / "specs"


def run(name: str, schema: dict[str, Any] | None, system: str, user: str) -> dict[str, Any]:
    request = ChatRequest.model_validate(forced_body(name, schema, system, user))
    arguments = structured_arguments(name, schema, request)
    if schema is not None:
        jsonschema.Draft202012Validator(schema).validate(arguments)
    return arguments


def run_call(call: SpecCall) -> dict[str, Any]:
    return run(call.name, call.schema, call.system, call.user)


def by_name(name: str) -> SpecCall:
    return next(call for call in SPEC_CALLS if call.name == name)


def test_registered_names_match_specs_10() -> None:
    text = (SPECS / "10-llm-gateway.md").read_text(encoding="utf-8")
    sentence = re.search(r"Registered names: (.*?)\. A test", text, re.DOTALL)
    assert sentence is not None
    assert set(re.findall(r"`([a-z_]+)`", sentence.group(1))) == STRUCTURED_TOOL_NAMES
    assert len(STRUCTURED_TOOL_NAMES) == 14
    assert {call.name for call in SPEC_CALLS} == STRUCTURED_TOOL_NAMES


@pytest.mark.parametrize("call", SPEC_CALLS, ids=lambda call: call.name)
def test_every_registered_name_yields_schema_valid_arguments(call: SpecCall) -> None:
    arguments = run_call(call)
    assert arguments


def test_router_picks_the_best_overlap() -> None:
    system = router_system(("edge_book", "caller wants to book a table"), ("edge_hours", "caller asks hours"))
    chosen = run("select_path", ROUTER_SCHEMA, system, router_user("I want to book a table, hours too"))
    assert chosen == {
        "path_id": "edge_book",
        "confidence": 8,
        "alternatives": [{"path_id": "edge_hours", "confidence": 2}],
        "reason": "Simulated routing: 2 shared words.",
    }


def test_router_ties_go_to_the_first_option_and_zero_overlap_stays() -> None:
    system = router_system(("edge_book", "table booking"), ("edge_hours", "table hours"))
    assert run("select_path", ROUTER_SCHEMA, system, router_user("a table"))["path_id"] == "edge_book"
    stayed = run("select_path", ROUTER_SCHEMA, system, router_user("hmm, let me think"))
    assert (stayed["path_id"], stayed["confidence"], stayed["alternatives"]) == ("stay", 5, [])


def test_router_without_stay_falls_back_to_a_fallback_option_then_none() -> None:
    schema = {**ROUTER_SCHEMA, "properties": {**ROUTER_SCHEMA["properties"]}}
    schema["properties"]["path_id"] = {"type": "string", "enum": ["edge_a", "edge_fallback"]}
    system = router_system(("edge_a", "billing"), ("edge_fallback", "anything else"))
    assert run("select_path", schema, system, router_user("weather"))["path_id"] == "edge_fallback"
    schema["properties"]["path_id"] = {"type": "string", "enum": ["edge_a", "none"]}
    assert run("select_path", schema, system, router_user("weather"))["path_id"] == "none"
    schema["properties"]["path_id"] = {"type": "string", "enum": ["edge_a", "edge_b"]}
    assert run("select_path", schema, system, router_user("weather"))["path_id"] == "edge_a"


def test_loop_condition_scores_eight_or_two() -> None:
    call = by_name("score_condition")
    assert run_call(call) == {"score": 8, "met": True, "missing": ""}
    missing = run(call.name, call.schema, call.system, router_user("hello there"))
    assert missing == {"score": 2, "met": False, "missing": "Simulated: condition not detected."}


def test_extraction_by_type() -> None:
    call = by_name("extract_variables")
    assert run_call(call) == {"party_size": 12, "caller_name": "Dana", "wants_patio": True}
    nothing = run(call.name, call.schema, call.system, router_user("hmm"))
    assert nothing == {"party_size": None, "caller_name": "hmm", "wants_patio": None}


@pytest.mark.parametrize(
    ("heard", "label", "confidence"),
    [
        ("You've reached Dana, leave a message after the tone", "voicemail", 0.9),
        ("Press 1 for sales", "ivr", 0.9),
        ("Hello? Who is this?", "human", 0.9),
        ("", "unknown", 0.5),
    ],
)
def test_answer_classifier(heard: str, label: str, confidence: float) -> None:
    call = by_name("classify_answer")
    system = f'Decide who answered.\n\nTranscript so far: "{heard}"\nCall classify_answer exactly once.'
    assert run(call.name, call.schema, system, "x") == {"label": label, "confidence": confidence}


def test_answer_classifier_boolean_legacy_schema_and_latest_message() -> None:
    schema = {
        "type": "object",
        "properties": {"is_voicemail": {"type": "boolean"}},
        "required": ["is_voicemail"],
    }
    assert run("classify_answer", schema, "Decide.", "The mailbox is full") == {"is_voicemail": True}
    assert run("classify_answer", schema, "Decide.", "Hello?") == {"is_voicemail": False}


def test_guard_rails_opt_out_triggers_and_custom_rules_do_not() -> None:
    call = by_name("evaluate_guard_rails")
    assert run_call(call)["results"] == [
        {"name": "tcpa_opt_out", "triggered": True, "confidence": 0.95, "evidence": "Stop calling"},
        {"name": "competitor_mention", "triggered": False, "confidence": 0.05, "evidence": ""},
    ]
    calm = run(call.name, GUARD_RAILS_SCHEMA, call.system, router_user("Tuesday works"))
    assert [result["triggered"] for result in calm["results"]] == [False, False]


def test_disposition_overlap_and_follow_up_fallback() -> None:
    interested = transcript_user("user: Yes, I agreed to the next step, I'm interested in the offer.")
    assert run("select_disposition", DISPOSITION_SCHEMA, DISPOSITION_SYSTEM, interested) == {
        "tag": "INTERESTED",
        "confidence": 0.7,
        "reason": "Simulated disposition.",
    }
    unclear = run(
        "classify_disposition", DISPOSITION_SCHEMA, DISPOSITION_SYSTEM, transcript_user("user: hmm")
    )
    assert unclear["tag"] == "FOLLOW_UP_REQUIRED"
    schema = {**DISPOSITION_SCHEMA, "properties": {**DISPOSITION_SCHEMA["properties"]}}
    schema["properties"]["tag"] = {"type": "string", "enum": ["NOT_INTERESTED", "TRANSFERRED"]}
    assert (
        run("select_disposition", schema, "Classify.", transcript_user("user: hmm"))["tag"]
        == "NOT_INTERESTED"
    )


def test_summary_is_the_first_thirty_transcript_words() -> None:
    summary = run_call(by_name("summarize"))["summary"]
    assert summary.startswith("Summary: Hi, this is Ava from Bistro. How can I help? Hi, my name is Dana")
    assert len(summary.removeprefix("Summary: ").split()) == 30


def test_analysis_fields_by_type() -> None:
    assert run_call(by_name("extract_analysis_schema")) == {
        "email_address": "dana@mail.test",
        "appointment_time": None,
        "wants_demo": True,
        "budget_usd": 4,
        "callback_phone": None,
        "plan": None,
    }
    stated = transcript_user(
        "user: The enterprise plan. Call me on +1 (415) 555-0100 at 2026-11-02 14:00:00, budget 1500.50."
    )
    analysis = run("record_analysis", ANALYSIS_SCHEMA, "x", stated)
    assert analysis["plan"] == "enterprise"
    assert analysis["callback_phone"] == "+1 (415) 555-0100"
    assert analysis["appointment_time"] == "2026-11-02 14:00:00"
    assert analysis["budget_usd"] == 1


def test_answers_are_positional() -> None:
    assert run_call(by_name("answer_questions")) == {"answers": [True, "human", 4]}
    voicemail = run("answer_questions", ANSWERS_SCHEMA, "x", transcript_user("user: leave a message"))
    assert voicemail["answers"][1] == "voicemail"


@pytest.mark.parametrize(
    ("said", "overall", "score", "frustrated"),
    [
        ("user: great, thanks, perfect, awesome", "very_positive", 1.0, False),
        ("user: good", "positive", 0.25, False),
        ("user: okay", "neutral", 0.0, False),
        ("user: this is bad and I am annoyed", "negative", -0.5, True),
        ("user: terrible, awful, I hate this, so frustrated", "very_negative", -1.0, True),
    ],
)
def test_sentiment(said: str, overall: str, score: float, frustrated: bool) -> None:
    assert run("record_sentiment", SENTIMENT_SCHEMA, "x", transcript_user(said)) == {
        "overall": overall,
        "score": score,
        "caller_frustration_detected": frustrated,
        "notable_moments": [],
    }


def test_citations_cite_the_first_supporting_turn() -> None:
    assert run_call(by_name("extract_citations")) == {
        "citations": [
            {
                "variable_name": "party_size",
                "value": 4,
                "turn_numbers": [1],
                "topics": [],
                "utterance_type": "statement",
            },
            {
                "variable_name": "wants_patio",
                "value": 4,
                "turn_numbers": [1],
                "topics": [],
                "utterance_type": "statement",
            },
        ]
    }


def test_citation_values_follow_the_value_type() -> None:
    call = by_name("extract_citations")
    schema = orjson.loads(orjson.dumps(call.schema))
    schema["$defs"]["Citation"]["properties"]["value"] = {"type": ["boolean", "null"]}
    cited = run("record_citations", schema, call.system, call.user)["citations"]
    assert [(item["value"], item["turn_numbers"]) for item in cited] == [(True, [3]), (True, [3])]
    nothing = run("record_citations", schema, call.system, "[T0|0-10|agent] Hello.")["citations"]
    assert [(item["value"], item["turn_numbers"]) for item in nothing] == [(None, []), (None, [])]


def test_grade_passes_with_score_eighty_for_every_criterion() -> None:
    assert run_call(by_name("grade")) == {"pass": True, "rationale": "Simulated evaluation."}
    criteria = {
        "type": "object",
        "required": ["criteria", "overall_score"],
        "properties": {
            "overall_score": {"type": "integer", "minimum": 0, "maximum": 50},
            "criteria": {
                "type": "array",
                "minItems": 2,
                "items": {
                    "type": "object",
                    "required": ["score", "rationale"],
                    "properties": {"score": {"type": "number"}, "rationale": {"type": "string"}},
                },
            },
        },
    }
    assert run("grade", criteria, "Grade.", "x") == {
        "overall_score": 50,
        "criteria": [{"score": 80, "rationale": "Simulated evaluation."}] * 2,
    }


def test_update_memory_keeps_the_stated_name() -> None:
    memory = run_call(by_name("update_memory"))
    assert memory["facts_set"] == {"name": "Dana Smith"}
    assert memory["summary"].startswith("Summary: Hi, this is Ava")
    assert (memory["facts_delete"], memory["open_items_add"], memory["open_items_close"]) == ([], [], [])


def test_aliases_and_unknown_names() -> None:
    assert TOOL_ALIASES == {
        "select_disposition": "classify_disposition",
        "record_citations": "extract_citations",
    }
    schema = {
        "type": "object",
        "required": ["flag", "count", "label"],
        "properties": {
            "flag": {"type": "boolean"},
            "count": {"type": "integer", "minimum": 3},
            "label": {"type": "string", "minLength": 2},
        },
    }
    assert run("lookup_order", schema, "x", "y") == {"flag": False, "count": 3, "label": "xx"}
    assert run("lookup_order", None, "x", "y") == {}


def test_candidate_keys_outside_the_schema_are_dropped() -> None:
    schema = {"type": "object", "properties": {"label": {"type": "string"}}}
    assert run("classify_answer", schema, "Decide.", "Hello?") == {"label": "human"}


def test_summary_reply_answers_the_plain_text_summary_request() -> None:
    system = "You write concise, factual summaries of phone calls between an AI agent and a person."
    request = ChatRequest.model_validate(chat_body(transcript_user("user: one two three"), system=system))
    assert summary_reply(request) == "Summary: one two three"
    tools = [{"type": "function", "function": {"name": "f"}}]
    assert summary_reply(ChatRequest.model_validate(chat_body("x", system=system, tools=tools))) is None
    assert summary_reply(ChatRequest.model_validate(chat_body("x", system="Other."))) is None


async def test_forced_and_required_calls_use_the_heuristics_and_rules_still_win() -> None:
    call = by_name("score_condition")
    body = forced_body(call.name, call.schema, call.system, call.user)
    required = {**body, "tool_choice": "required"}
    summary_request = chat_body(
        transcript_user("user: please book it"), system="You write concise, factual summaries of phone calls."
    )
    async with llm_client(llm_state()) as client:
        forced = (await client.post("/v1/chat/completions", json=body)).json()
        by_required = (await client.post("/v1/chat/completions", json=required)).json()
        summary = (await client.post("/v1/chat/completions", json=summary_request)).json()
        await add_rules(
            client,
            "rules:\n  - id: r\n    match: {tool_name: score_condition}\n"
            "    respond: {tool_calls: [{name: score_condition, arguments: {score: 1}}]}\n",
        )
        ruled = (await client.post("/v1/chat/completions", json=body)).json()

    def arguments(completion: dict[str, Any]) -> Any:
        return orjson.loads(completion["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"])

    assert arguments(forced) == {"score": 8, "met": True, "missing": ""}
    assert arguments(by_required) == arguments(forced)
    assert arguments(ruled) == {"score": 1}
    assert summary["choices"][0]["message"]["content"] == "Summary: please book it"
