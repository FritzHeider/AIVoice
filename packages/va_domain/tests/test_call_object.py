"""The Call Object contract against the specs/19 §4 table, and the stored → Bland mapping helpers."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, get_args

import pytest

from va_domain.call_object import (
    BLAND_STATUS_FILTER,
    CALL_ENDED_BY,
    LIST_EXCLUDED_FIELDS,
    BlandCallStatus,
    CallLatency,
    CallListItem,
    CallObject,
    Transcript,
    bland_answered_by,
    bland_status,
    concatenated_transcript,
    dollars,
    latency_summary,
    minutes,
)

SPEC: Final = Path(__file__).resolve().parents[3] / "specs" / "19-events-and-webhooks.md"
STORED_STATUSES: Final = (
    "scheduled",
    "queued",
    "dialing",
    "ringing",
    "in_progress",
    "transferring",
    "transferred",
    "completed",
    "failed",
    "canceled",
    "no_answer",
    "busy",
)
NULLABLE_BY_DECISION: Final = frozenset({"call_ended_by", "end_reason"})
"""Not ``| null`` in the table; `null` while the call is active (specs/19 §4 Decision P05-T16a)."""


def spec_fields() -> dict[str, tuple[str, bool]]:
    """Field name → (JSON type, nullable) parsed from the §4 table."""
    text = SPEC.read_text(encoding="utf-8")
    section = text.split("## 4. Call Object", 1)[1].split("\n## 5.", 1)[0]
    fields: dict[str, tuple[str, bool]] = {}
    for line in section.splitlines():
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip())[1:-1]]
        if len(cells) < 2 or not cells[0].startswith("`") or cells[0] == "Field":
            continue
        names = re.findall(r"`([a-z_]+)`", cells[0])
        type_cell = cells[1].replace("\\|", "|")
        nullable = "null" in type_cell
        for name in names:
            fields[name] = (_json_type(type_cell), nullable)
    return fields


def _json_type(cell: str) -> str:
    for word, json_type in (
        ("boolean", "boolean"),
        ("integer", "integer"),
        ("number", "number"),
        ("object", "object"),
        ("array", "array"),
    ):
        if cell.startswith(word):
            return json_type
    return "string"  # string, ISO 8601, and quoted enumerations


def _schema_types(prop: dict[str, Any], defs: dict[str, Any]) -> tuple[set[str], bool]:
    options = prop.get("anyOf", [prop])
    types: set[str] = set()
    nullable = False
    for option in options:
        target = defs[option["$ref"].rsplit("/", 1)[1]] if "$ref" in option else option
        kind = target.get("type")
        if kind == "null":
            nullable = True
        elif kind is not None:
            types.add(kind)
    return types, nullable


def test_the_spec_table_is_parsed() -> None:
    fields = spec_fields()
    assert len(fields) == 54
    assert fields["from"] == ("string", False)
    assert fields["agent_version"] == ("string", True)
    assert fields["pathway_version"] == ("integer", True)
    assert fields["transcripts"] == ("array", False)


def test_the_call_object_has_every_spec_field_with_its_type() -> None:
    schema = CallObject.model_json_schema(by_alias=True)
    properties = schema["properties"]
    expected = spec_fields()
    assert set(properties) == set(expected)
    assert set(schema["required"]) == set(expected)
    for name, (json_type, nullable) in expected.items():
        types, schema_nullable = _schema_types(properties[name], schema["$defs"])
        wanted = {"number", "integer"} if json_type == "number" else {json_type}
        assert types and types <= wanted, name
        assert schema_nullable == (nullable or name in NULLABLE_BY_DECISION), name


def test_list_items_are_the_call_object_minus_the_heavy_fields() -> None:
    full = set(CallObject.model_json_schema(by_alias=True)["properties"])
    listed = set(CallListItem.model_json_schema(by_alias=True)["properties"])
    assert {
        "transcripts",
        "concatenated_transcript",
        "pathway_logs",
        "variables",
        "citations",
        "evals",
        "analysis",
    } == LIST_EXCLUDED_FIELDS
    assert listed == full - LIST_EXCLUDED_FIELDS


@pytest.mark.parametrize("model", [CallObject, CallListItem])
def test_the_schema_example_validates_and_round_trips(model: type[CallListItem]) -> None:
    example = model.model_json_schema()["examples"][0]
    parsed = model.model_validate(example)
    dumped = parsed.model_dump(mode="json", by_alias=True)
    assert dumped["from"] == example["from"]
    assert set(dumped) == set(example)


def test_from_is_accepted_by_field_name_too() -> None:
    example = dict(CallListItem.model_json_schema()["examples"][0])
    sender = example.pop("from")
    item = CallListItem.model_validate({**example, "from_": sender})
    assert item.from_ == sender


@pytest.mark.parametrize(
    ("stored", "bland"),
    [
        ("scheduled", "scheduled"),
        ("queued", "queued"),
        ("dialing", "in-progress"),
        ("ringing", "in-progress"),
        ("in_progress", "in-progress"),
        ("transferring", "in-progress"),
        ("transferred", "completed"),
        ("completed", "completed"),
        ("failed", "failed"),
        ("canceled", "canceled"),
        ("no_answer", "no-answer"),
        ("busy", "busy"),
    ],
)
def test_bland_status(stored: str, bland: str) -> None:
    assert bland_status(stored) == bland


def test_the_status_filter_is_the_inverse_of_the_mapping() -> None:
    assert set(BLAND_STATUS_FILTER) == set(get_args(BlandCallStatus.__value__))
    covered = [status for statuses in BLAND_STATUS_FILTER.values() for status in statuses]
    assert sorted(covered) == sorted(STORED_STATUSES)
    for bland, statuses in BLAND_STATUS_FILTER.items():
        assert all(bland_status(status) == bland for status in statuses)
    assert BLAND_STATUS_FILTER["completed"] == ("transferred", "completed")


def test_every_ended_by_maps_to_bland_uppercase() -> None:
    assert dict(CALL_ENDED_BY) == {
        "agent": "ASSISTANT",
        "user": "USER",
        "system": "SYSTEM",
        "api": "API",
        "guard_rail": "GUARD_RAIL",
        "timeout": "TIMEOUT",
        "error": "ERROR",
    }


@pytest.mark.parametrize(
    ("stored", "bland"),
    [
        (None, None),
        ("human", "human"),
        ("voicemail", "voicemail"),
        ("ivr", "ivr"),
        ("unknown", "unknown"),
        ("no_answer", "no-answer"),
        ("fax", "fax"),
    ],
)
def test_bland_answered_by(stored: str | None, bland: str | None) -> None:
    assert bland_answered_by(stored) == bland


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(Decimal("141.000"), 2.35), (Decimal("0"), 0.0), (Decimal("1.234"), 0.020567), (59.9999999, 1.0)],
)
def test_minutes_round_to_six_decimals(seconds: Decimal | float, expected: float) -> None:
    assert minutes(seconds) == expected


@pytest.mark.parametrize(
    ("micros", "expected"), [(282_000, 0.282), (0, 0.0), (1, 0.000001), (1_234_567, 1.234567)]
)
def test_dollars(micros: int, expected: float) -> None:
    assert dollars(micros) == expected


def _turn(position: int, role: str, text: str) -> Transcript:
    example = CallObject.model_json_schema()["examples"][0]["transcripts"][0]
    return Transcript.model_validate({**example, "id": position, "user": role, "text": text})


def test_concatenated_transcript_uses_the_bland_format() -> None:
    turns = [_turn(1, "assistant", "Hi there."), _turn(2, "user", "Hello."), _turn(3, "agent-action", "x")]
    assert concatenated_transcript(turns) == "assistant: Hi there.\n user: Hello.\n agent-action: x\n"
    assert concatenated_transcript([]) == ""


def test_latency_summary_of_no_turns() -> None:
    assert latency_summary([]) == CallLatency(
        p50_e2e_ms=None, p95_e2e_ms=None, avg_llm_ttft_ms=None, avg_tts_ttfa_ms=None, turns=0
    )


def test_latency_summary_uses_nearest_rank_and_rounded_means() -> None:
    latencies: list[dict[str, Any]] = [
        {"e2e_ms": 900, "llm_ttft_ms": 300, "tts_ttfa_ms": 151},
        {"e2e_ms": 700, "llm_ttft_ms": 301},
        {"e2e_ms": 800.4, "tts_ttfa_ms": 150},
        {},
        {"e2e_ms": True, "llm_ttft_ms": "fast"},
    ]
    assert latency_summary(latencies) == CallLatency(
        p50_e2e_ms=800, p95_e2e_ms=900, avg_llm_ttft_ms=300, avg_tts_ttfa_ms=150, turns=3
    )


def test_latency_summary_of_an_even_count() -> None:
    summary = latency_summary([{"e2e_ms": value} for value in (400, 100, 300, 200)])
    assert (summary.p50_e2e_ms, summary.p95_e2e_ms, summary.turns) == (200, 400, 4)


def test_latency_summary_of_one_turn() -> None:
    summary = latency_summary([{"e2e_ms": 512}])
    assert (summary.p50_e2e_ms, summary.p95_e2e_ms) == (512, 512)
