"""Send Call aliases, explicit fields, unknown fields, runner rules, start_time (specs/05 §3, §4.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from pydantic import ValidationError

from va_domain.calls import (
    PARAMETER_NAMES,
    CallParameters,
    CallRequestError,
    DialingStrategy,
    SendCallRequest,
    unknown_fields,
)

PHONE = {"phone_number": "+14155550100"}
BASE: dict[str, Any] = {**PHONE, "task": "Hi"}
ID_A = "0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6b7a"
ID_B = "0192f5a1-7c3e-7d4a-9b1c-2f3e4d5c6b7b"
KB_A = f"KB-{ID_A}"
KB_B = f"KB-{ID_B}"


def _explicit(**body: Any) -> dict[str, Any]:
    return SendCallRequest.model_validate({**BASE, **body}).explicit_fields()


def test_explicit_fields_hold_only_sent_parameters() -> None:
    assert _explicit() == {}
    assert _explicit(record=False, voice="Karen") == {"record": False, "voice": "Karen"}


def test_explicit_fields_exclude_runner_fields_and_destination() -> None:
    fields = _explicit(pathway_id=ID_A, persona_settings={"voice": "x"}, agent_version="dev")
    assert fields == {}


def test_from_is_reported_under_its_parameter_name() -> None:
    assert _explicit(**{"from": "+14155550101"}) == {"from": "+14155550101"}


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"amd": True}, {"ivr_mode": True}),
        ({"amd": False}, {"ivr_mode": False}),
        ({"amd": True, "ivr_mode": False}, {"ivr_mode": False}),
        ({"available_tags": ["HOT"]}, {"dispositions": ["HOT"]}),
        ({"available_tags": ["HOT"], "dispositions": ["COLD"]}, {"dispositions": ["COLD"]}),
        ({"local_dialing": True}, {"dialing_strategy": DialingStrategy.model_validate({"type": "local"})}),
        (
            {"geospatial_dialing": True},
            {"dialing_strategy": DialingStrategy.model_validate({"type": "local"})},
        ),
        ({"local_dialing": False}, {}),
        (
            {"local_dialing": True, "dialing_strategy": {"type": "default"}},
            {"dialing_strategy": DialingStrategy()},
        ),
    ],
)
def test_legacy_aliases_fold_into_the_canonical_parameter(
    body: dict[str, Any], expected: dict[str, Any]
) -> None:
    assert _explicit(**body) == expected


def test_knowledge_base_ids_join_the_kb_entries_of_tools() -> None:
    tool = f"TL-{ID_A}"
    fields = _explicit(tools=[tool, KB_A], knowledge_base_ids=[KB_A, KB_B, KB_B])
    assert fields == {"tools": [tool, KB_A, KB_B]}
    assert _explicit(knowledge_base_ids=[KB_B]) == {"tools": [KB_B]}


def test_voicemail_reports_sent_keys_and_implied_action() -> None:
    assert _explicit(voicemail={"message": "Call us back"}) == {
        "voicemail": {"message": "Call us back", "action": "leave_message"}
    }
    assert _explicit(voicemail={"message": "m", "action": "hangup"}) == {
        "voicemail": {"message": "m", "action": "hangup"}
    }
    assert _explicit(voicemail={"sensitive": True}) == {"voicemail": {"sensitive": True}}


def test_lower_layers_parse_with_call_parameters() -> None:
    defaults = CallParameters.model_validate({"voice": "Maya", "record": True, "task": "ignored"})
    assert defaults.explicit_fields() == {"voice": "Maya", "record": True}


def test_unknown_top_level_fields_are_ignored_and_reported() -> None:
    raw = {**BASE, "from": "+14155550101", "from_": "+14155550101", "citations": True, "pronunciation": 1}
    request = SendCallRequest.model_validate(raw)
    assert request.from_ == "+14155550101"
    assert unknown_fields(raw) == ["citations", "pronunciation"]


def test_unknown_nested_fields_are_rejected() -> None:
    with pytest.raises(ValidationError):
        SendCallRequest.model_validate({**BASE, "voicemail": {"message": "m", "beep": True}})


def test_parameter_names_use_bland_names() -> None:
    assert "from" in PARAMETER_NAMES
    assert "from_" not in PARAMETER_NAMES
    assert "task" not in PARAMETER_NAMES
    assert "phone_number" not in PARAMETER_NAMES


RULE_CASES: list[tuple[dict[str, Any], str, str]] = [
    ({}, "RUNNER_CONFLICT", "task"),
    ({"agent_id": ID_A, "persona_id": ID_B}, "RUNNER_CONFLICT", "agent_id"),
    ({"agent_id": ID_A, "pathway_id": ID_B}, "RUNNER_CONFLICT", "agent_id"),
    ({"agent_id": ID_A, "task": "Hi"}, "RUNNER_CONFLICT", "task"),
    ({"agent_id": ID_A, "persona_id": ID_B, "task": "Hi"}, "RUNNER_CONFLICT", "agent_id"),
    ({"task": "Hi", "retry": {"voicemail_action": "ignore"}}, "RETRY_NOT_PROVIDED", "retry.wait"),
    ({"retry": {}}, "RETRY_NOT_PROVIDED", "retry.wait"),
]


@pytest.mark.parametrize(("body", "code", "field"), RULE_CASES)
def test_check_rules_raises_the_first_failure(body: dict[str, Any], code: str, field: str) -> None:
    request = SendCallRequest.model_validate({**PHONE, **body})
    with pytest.raises(CallRequestError) as caught:
        request.check_rules()
    assert (caught.value.code, caught.value.field) == (code, field)
    assert code in repr(caught.value)


def test_retry_message_is_the_catalogued_text() -> None:
    request = SendCallRequest.model_validate({**BASE, "retry": {}})
    with pytest.raises(CallRequestError, match=r"^Retry attempt not provided for retry call\.$"):
        request.check_rules()


@pytest.mark.parametrize(
    ("body", "runner"),
    [
        ({"task": "Hi"}, "task"),
        ({"pathway_id": ID_A}, "pathway"),
        ({"pathway_id": ID_A, "task": "Hi"}, "pathway"),
        ({"persona_id": ID_A}, "persona"),
        ({"persona_id": ID_A, "pathway_id": ID_B, "pathway_version": 3}, "persona"),
        ({"persona_id": ID_A, "task": "Override"}, "persona"),
        ({"agent_id": ID_A}, "agent"),
        ({"agent_id": ID_A, "agent_version": "branch:x", "retry": {"wait": 60}}, "agent"),
    ],
)
def test_valid_runner_combinations(body: dict[str, Any], runner: str) -> None:
    request = SendCallRequest.model_validate({**PHONE, **body})
    request.check_rules()
    assert request.runner_type == runner


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-20 09:30:00 -08:00", datetime(2026, 11, 20, 9, 30, tzinfo=timezone(timedelta(hours=-8)))),
        (
            "2026-11-20 09:30:00 +05:30",
            datetime(2026, 11, 20, 9, 30, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        ),
        ("2026-11-20 09:30:00", datetime(2026, 11, 20, 9, 30)),  # noqa: DTZ001 - naive by design
        ("2026-11-20T17:30:00Z", datetime(2026, 11, 20, 17, 30, tzinfo=UTC)),
        (
            "2026-11-20T17:30:00.250+01:00",
            datetime(2026, 11, 20, 17, 30, 0, 250_000, tzinfo=timezone(timedelta(hours=1))),
        ),
    ],
)
def test_start_time_formats(text: str, expected: datetime) -> None:
    request = SendCallRequest.model_validate({**BASE, "start_time": text})
    assert request.start_time == expected
    assert (request.start_time.tzinfo is None) == (expected.tzinfo is None)


@pytest.mark.parametrize("text", ["2026-11-20 9:30", "20/11/2026 09:30:00", "2026-11-20 09:30:00 PST", ""])
def test_bad_start_time_is_rejected(text: str) -> None:
    with pytest.raises(ValidationError):
        SendCallRequest.model_validate({**BASE, "start_time": text})


def test_persona_settings_accept_every_parameter_name() -> None:
    settings = dict.fromkeys(PARAMETER_NAMES, None)
    request = SendCallRequest.model_validate({**PHONE, "persona_id": ID_A, "persona_settings": settings})
    assert set(request.persona_settings) == PARAMETER_NAMES
