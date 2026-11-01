"""Every Send Call parameter boundary (specs/05-calls-api.md §3): one valid/invalid table, one field each."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from va_domain.calls import SendCallRequest
from va_domain.org import field_path

BASE: dict[str, Any] = {"phone_number": "+14155550100", "task": "Hi"}
UUID_A = "0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6b7a"
TL = f"TL-{UUID_A}"
KB = f"KB-{UUID_A}"


def _sized_object(total: int) -> dict[str, str]:
    """A `{"k": "xxx…"}` object whose compact JSON is exactly ``total`` bytes."""
    overhead = len(json.dumps({"k": ""}, separators=(",", ":")))
    return {"k": "x" * (total - overhead)}


def _tool(**fields: Any) -> dict[str, Any]:
    return {"name": "lookup", "description": "Looks things up.", "url": "https://api.acme.test/x", **fields}


def _rail(**fields: Any) -> dict[str, Any]:
    return {"type": "custom", "name": "angry", "description": "Caller is angry", **fields}


VALID: list[tuple[str, Any]] = [
    ("task", "x" * 16_000),
    ("pathway_version", 0),
    ("pathway_version", 7),
    ("pathway_version", "staging"),
    ("pathway_version", "draft"),
    ("persona_version", "draft"),
    ("persona_version", 3),
    ("persona_settings", {"voice": "Karen", "from": "+14155550101"}),
    ("agent_version", "1.2.3"),
    ("agent_version", "1.2.3-rc.1"),
    ("agent_version", "branch:feature/x_1.2"),
    ("agent_version", "latest"),
    ("start_node_id", "n" * 128),
    ("voice", "v" * 100),
    ("voice_settings", {"speed": 0.7}),
    ("voice_settings", {"speed": 1.2, "stability": 0, "style": 1}),
    ("first_sentence", "f" * 1000),
    ("language", "EN-us"),
    ("language", "babel"),
    ("language", "fluent"),
    ("model", "turbo"),
    ("temperature", 0),
    ("temperature", 1),
    ("max_tokens", 32),
    ("max_tokens", 1024),
    ("pronunciation_guide", [{"word": "w" * 100, "pronunciation": "p" * 200}] * 200),
    ("interruption_threshold", 50),
    ("interruption_threshold", 5000),
    ("interruption_threshold", None),
    ("interruptibility", 0),
    ("interruptibility", 4),
    ("background_track", "restaurant"),
    ("background_track", None),
    ("max_duration", 0.5),
    ("max_duration", 240),
    ("from", "+14155550101"),
    ("dialing_strategy", {"type": "local"}),
    ("dialing_strategy", {"type": "custom_pooling", "pool_id": UUID_A}),
    ("timezone", "Europe/Berlin"),
    ("start_time", "2026-11-20 09:30:00"),
    ("transfer_phone_number", "+14155550199"),
    ("transfer_phone_number", "sip:frontdesk@pbx.acme.test"),
    ("transfer_list", {}),
    ("transfer_list", {"default": "+14155550199", **{f"l{i}": "+14155550198" for i in range(19)}}),
    ("warm_transfer", {"enabled": True, "timeout_s": 10, "hold_audio": "agent"}),
    ("warm_transfer", {"timeout_s": 120, "briefing_prompt": "b" * 2000}),
    ("country", "gb"),
    ("tools", [TL, KB, _tool()] + [TL] * 27),
    ("tools", [_tool(method="GET", headers={"X-Api-Key": "{{request_data.k}}"}, timeout_ms=100, retries=3)]),
    ("knowledge_base_ids", [KB] * 10),
    ("memory_id", UUID_A),
    (
        "voicemail",
        {"action": "leave_message_and_sms", "sms": {"message": "m" * 1600, "from": "+14155550101"}},
    ),
    ("voicemail", {"message": "m" * 1000, "sensitive": True}),
    ("voicemail_detect_timeout_ms", 3000),
    ("voicemail_detect_timeout_ms", 30_000),
    ("summary_prompt", "s" * 2000),
    ("dispositions", ["INTERESTED", "A_1"]),
    ("available_tags", ["X" * 64]),
    ("analysis_schema", {f"k{i}": "string" for i in range(50)}),
    ("analysis_prompt", "a" * 2000),
    ("citation_schema_ids", [UUID_A] * 10),
    ("post_call_evals", {"workbench_setup_id": UUID_A}),
    ("post_call_evals", {"workbench_setup_version_id": UUID_A}),
    ("retry", {"wait": 60, "max_attempts": 5}),
    ("retry", {"wait": 86_400, "voicemail_action": "leave_message", "on_no_answer": True}),
    ("request_data", _sized_object(65_536)),
    ("request_data", {"_a1": [1, {"b": None}]}),
    ("metadata", _sized_object(65_536)),
    ("external_id", "crm-42 ~!"),
    ("external_id", "e" * 256),
    ("webhook", "http://localhost:8080/hook"),
    ("webhook_events", ["queue", "post_transfer_transcript"]),
    ("dynamic_data", [{"url": "https://crm.acme.test/c?p={{to}}", "cache_ttl_seconds": 86_400}] * 10),
    ("keywords", ["Acme", "Bella Pasta:10", "x:1"]),
    ("precall_dtmf_sequence", "1w2#*"),
    ("guard_rails", [{"type": "tcpa:opt_out", "action": {"type": "end_call"}}] * 20),
    ("guard_rails", [_rail(action={"type": "move_to_node", "node_id": "escalation"})]),
    ("guard_rails", [{"type": "tcpa:recording_disclosure", "config": {"end_seconds": 30}}]),
]

INVALID: list[tuple[str, Any, str]] = [
    ("task", "x" * 16_001, "task"),
    ("task", "   ", "task"),
    ("pathway_id", "not-a-uuid", "pathway_id"),
    ("pathway_version", -1, "pathway_version"),
    ("pathway_version", "latest", "pathway_version"),
    ("pathway_version", True, "pathway_version"),
    ("persona_version", "staging", "persona_version"),
    ("persona_settings", {"not_a_param": 1}, "persona_settings.not_a_param"),
    ("persona_settings", {"task": "x"}, "persona_settings.task"),
    ("agent_version", "1.2", "agent_version"),
    ("agent_version", "branch:", "agent_version"),
    ("agent_version", "branch:" + "b" * 65, "agent_version"),
    ("start_node_id", "n" * 129, "start_node_id"),
    ("phone_number", "", "phone_number"),
    ("phone_number", "1" * 65, "phone_number"),
    ("voice", "v" * 101, "voice"),
    ("voice_settings", {"speed": 1.3}, "voice_settings.speed"),
    ("voice_settings", {"stability": -0.1}, "voice_settings.stability"),
    ("voice_settings", {"pitch": 1}, "voice_settings.pitch"),
    ("first_sentence", "f" * 1001, "first_sentence"),
    ("language", "xx", "language"),
    ("model", "", "model"),
    ("temperature", 1.01, "temperature"),
    ("max_tokens", 31, "max_tokens"),
    ("max_tokens", 1025, "max_tokens"),
    ("pronunciation_guide", [{"word": "w", "pronunciation": "p"}] * 201, "pronunciation_guide"),
    ("pronunciation_guide", [{"word": "", "pronunciation": "p"}], "pronunciation_guide[0].word"),
    ("interruption_threshold", 49, "interruption_threshold"),
    ("interruption_threshold", 5001, "interruption_threshold"),
    ("interruptibility", 5, "interruptibility"),
    ("background_track", "jungle", "background_track"),
    ("max_duration", 0.49, "max_duration"),
    ("max_duration", 240.1, "max_duration"),
    ("from", "4155550101", "from"),
    ("dialing_strategy", {"type": "custom_pooling"}, "dialing_strategy"),
    ("dialing_strategy", {"type": "local", "pool_id": UUID_A}, "dialing_strategy"),
    ("timezone", "Mars/Olympus", "timezone"),
    ("start_time", "next tuesday", "start_time"),
    ("start_time", 1_790_000_000, "start_time"),
    ("transfer_phone_number", "415-555-0199", "transfer_phone_number"),
    ("transfer_phone_number", "sip:nobody", "transfer_phone_number"),
    ("transfer_list", {"sales": "+14155550199"}, "transfer_list"),
    (
        "transfer_list",
        {"default": "+14155550199", **{f"l{i}": "+14155550198" for i in range(20)}},
        "transfer_list",
    ),
    ("transfer_list", {"default": "4155550199"}, "transfer_list.default"),
    ("warm_transfer", {"timeout_s": 9}, "warm_transfer.timeout_s"),
    ("warm_transfer", {"hold_audio": "jazz"}, "warm_transfer.hold_audio"),
    ("country", "USA", "country"),
    ("tools", [TL] * 31, "tools"),
    ("tools", [f"TX-{UUID_A}"], "tools[0]"),
    ("tools", [_tool(secret_headers={"Authorization": "x"})], "tools[0]"),
    ("tools", [_tool(name="1bad")], "tools[0]"),
    ("tools", [_tool(url="ftp://x.test")], "tools[0]"),
    ("tools", [_tool(headers={"Bad Header": "x"})], "tools[0]"),
    ("tools", [_tool(timeout_ms=99)], "tools[0]"),
    ("tools", [_tool(retries=4)], "tools[0]"),
    ("tools", [_tool(input_schema={"type": "array"})], "tools[0]"),
    ("tools", [_tool(body={"k": "x" * 65_536})], "tools[0]"),
    ("knowledge_base_ids", [KB] * 11, "knowledge_base_ids"),
    ("knowledge_base_ids", [TL], "knowledge_base_ids[0]"),
    ("memory_id", "m", "memory_id"),
    ("voicemail", {"action": "leave_message_and_sms"}, "voicemail"),
    ("voicemail", {"message": "m" * 1001}, "voicemail.message"),
    ("voicemail", {"action": "scream"}, "voicemail.action"),
    (
        "voicemail",
        {"action": "leave_message_and_sms", "sms": {"message": "m" * 1601}},
        "voicemail.sms.message",
    ),
    ("voicemail_detect_timeout_ms", 2999, "voicemail_detect_timeout_ms"),
    ("voicemail_detect_timeout_ms", 30_001, "voicemail_detect_timeout_ms"),
    ("summary_prompt", "s" * 2001, "summary_prompt"),
    ("dispositions", ["interested"], "dispositions[0]"),
    ("dispositions", ["A"] * 51, "dispositions"),
    ("available_tags", ["9LIVES"], "available_tags[0]"),
    ("analysis_schema", {f"k{i}": "string" for i in range(51)}, "analysis_schema"),
    ("analysis_schema", {"k": ""}, "analysis_schema.k"),
    ("analysis_prompt", "a" * 2001, "analysis_prompt"),
    ("citation_schema_ids", [UUID_A] * 11, "citation_schema_ids"),
    ("post_call_evals", {}, "post_call_evals"),
    (
        "post_call_evals",
        {"workbench_setup_id": UUID_A, "workbench_setup_version_id": UUID_A},
        "post_call_evals",
    ),
    ("retry", {"wait": 59}, "retry.wait"),
    ("retry", {"wait": 86_401}, "retry.wait"),
    ("retry", {"wait": 60, "max_attempts": 6}, "retry.max_attempts"),
    ("retry", {"wait": 60, "voicemail_action": "leave_message_and_sms"}, "retry.voicemail_action"),
    ("request_data", _sized_object(65_537), "request_data"),
    ("request_data", {"1bad": 1}, "request_data.1bad"),
    ("request_data", {"has-dash": 1}, "request_data.has-dash"),
    ("metadata", _sized_object(65_537), "metadata"),
    ("external_id", "café", "external_id"),
    ("external_id", "e" * 257, "external_id"),
    ("webhook", "ftp://hooks.acme.test", "webhook"),
    ("webhook", "https://" + "h" * 2048, "webhook"),
    ("webhook_events", ["calls"], "webhook_events[0]"),
    ("webhook_events", ["call", "call"], "webhook_events"),
    ("dynamic_data", [{"url": "https://x.test"}] * 11, "dynamic_data"),
    ("dynamic_data", [{"url": "https://x.test", "timing": "post_call"}], "dynamic_data[0].timing"),
    (
        "dynamic_data",
        [{"url": "https://x.test", "cache_ttl_seconds": 86_401}],
        "dynamic_data[0].cache_ttl_seconds",
    ),
    (
        "dynamic_data",
        [{"url": "https://x.test", "response_data": [{"name": "1x", "data": "$"}]}],
        "dynamic_data[0]",
    ),
    ("keywords", ["word:11"], "keywords[0]"),
    ("keywords", ["word:0"], "keywords[0]"),
    ("keywords", [":boost"], "keywords[0]"),
    ("keywords", ["k"] * 101, "keywords"),
    ("precall_dtmf_sequence", "12a", "precall_dtmf_sequence"),
    ("precall_dtmf_sequence", "1" * 65, "precall_dtmf_sequence"),
    ("guard_rails", [{"type": "tcpa:opt_out"}] * 21, "guard_rails"),
    ("guard_rails", [{"type": "custom", "name": "x"}], "guard_rails[0]"),
    ("guard_rails", [_rail(action={"type": "transfer"})], "guard_rails[0].action"),
    ("guard_rails", [_rail(action={"type": "move_to_node"})], "guard_rails[0].action"),
    ("guard_rails", [{"type": "tcpa:unknown"}], "guard_rails[0].type"),
    ("record", "maybe", "record"),
]


def _parse(field: str, value: Any) -> SendCallRequest:
    return SendCallRequest.model_validate({**BASE, field: value})


@pytest.mark.parametrize(("field", "value"), VALID, ids=[f"{f}-{i}" for i, (f, _) in enumerate(VALID)])
def test_valid_boundary_is_accepted(field: str, value: Any) -> None:
    request = _parse(field, value)
    assert ("from_" if field == "from" else field) in request.model_fields_set


@pytest.mark.parametrize(
    ("field", "value", "path"), INVALID, ids=[f"{f}-{i}" for i, (f, _, _) in enumerate(INVALID)]
)
def test_invalid_boundary_is_rejected_at_its_path(field: str, value: Any, path: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _parse(field, value)
    paths = {field_path(error["loc"]) for error in caught.value.errors()}
    assert any(p == path or p.startswith(f"{path}.") for p in paths), paths


def test_every_parameter_has_a_boundary_case() -> None:
    covered = {field for field, _ in VALID} | {field for field, _, _ in INVALID}
    names = {"from" if name == "from_" else name for name in SendCallRequest.model_fields}
    plain_flags = {
        "wait_for_greeting",
        "block_interruptions",
        "noise_cancellation",
        "hd_audio",
        "memory_enabled",
        "ivr_mode",
        "amd",
        "answered_by_enabled",
        "stream_post_transfer_transcript",
        "ignore_button_press",
        "test_call",
        "local_dialing",
        "geospatial_dialing",
        "test_mode",
        "agent_id",
        "persona_id",
    }
    assert names - covered - plain_flags == set()


@pytest.mark.parametrize(
    "flag", ["wait_for_greeting", "hd_audio", "ivr_mode", "amd", "test_call", "test_mode"]
)
def test_boolean_flags_reject_non_booleans(flag: str) -> None:
    assert getattr(_parse(flag, True), flag) is True
    with pytest.raises(ValidationError):
        _parse(flag, "sometimes")


def test_defaults_match_the_spec_tables() -> None:
    request = SendCallRequest.model_validate(BASE)
    assert request.language == "babel-en"
    assert request.model == "base"
    assert (request.temperature, request.max_tokens, request.interruptibility) == (0.7, 250, 2)
    assert request.noise_cancellation is True
    assert request.answered_by_enabled is True
    assert request.max_duration == 30
    assert request.timezone == "America/Los_Angeles"
    assert request.country == "US"
    assert request.voicemail_detect_timeout_ms == 12_000
    assert request.background_track is None
    assert request.request_data == {}
    assert request.metadata == {}


def test_country_is_upper_cased_and_strings_trimmed() -> None:
    request = SendCallRequest.model_validate({**BASE, "country": " gb ", "voice": "  Karen ", "task": " Hi "})
    assert (request.country, request.voice, request.task) == ("GB", "Karen", "Hi")


def test_request_json_schema_builds_with_an_example() -> None:
    schema = SendCallRequest.model_json_schema(by_alias=True)
    assert "from" in schema["properties"]
    assert schema["required"] == ["phone_number"]
    example = schema["examples"][0]
    SendCallRequest.model_validate(example).check_rules()
