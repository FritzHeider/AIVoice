"""Server error message types and close frames map to the specs/08 §4 actions (Decision P03-T11 rule 6)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from va_elevenlabs import SttAuthError, SttProtocolError, SttQuotaError, SttTermsError
from va_elevenlabs.stt_errors import ACTIONS, MINIMAL, classify_close, fatal_error
from va_elevenlabs.stt_wire import IGNORED_TYPES

SPEC = Path(__file__).parents[3] / "specs" / "08-stt-elevenlabs.md"
DATA_TYPES = {
    "session_started",
    "partial_transcript",
    "committed_transcript",
    "committed_transcript_with_timestamps",
    "committed_transcript_entities",
}
EXPECTED_BY_ACTION = {
    "fatal": {"auth_error", "quota_exceeded", "unaccepted_terms"},
    "reconnect": {"error", "session_time_limit_exceeded"},
    "backoff": {"rate_limited", "resource_exhausted", "transcriber_error"},
    "minimal": {"invalid_request"},
    "warn": {"warning"},
    "throttled": {"commit_throttled"},
    "grow": {"queue_overflow"},
    "shrink": {"chunk_size_exceeded"},
    "log": {"input_error"},
}


def spec_message_types() -> set[str]:
    text = SPEC.read_text(encoding="utf-8")
    section = text[text.index("Server → client handling:") : text.index("## 5.")]
    return set(re.findall(r"^\| `([a-z_]+)` \|", section, flags=re.MULTILINE)) - {"message_type"}


def test_every_spec_row_is_handled() -> None:
    rows = spec_message_types()
    assert len(rows) == 21
    assert rows == DATA_TYPES | IGNORED_TYPES | set(ACTIONS)


def test_actions_follow_rule_6() -> None:
    by_action: dict[str, set[str]] = {}
    for message_type, action in ACTIONS.items():
        by_action.setdefault(action, set()).add(message_type)
    assert by_action == EXPECTED_BY_ACTION


@pytest.mark.parametrize(
    ("message_type", "error_class"),
    [
        ("auth_error", SttAuthError),
        ("quota_exceeded", SttQuotaError),
        ("unaccepted_terms", SttTermsError),
        ("invalid_request", SttProtocolError),
    ],
)
def test_fatal_error_classes(message_type: str, error_class: type[Exception]) -> None:
    error = fatal_error(message_type, "why")
    assert type(error) is error_class
    assert (error.code, error.message, str(error)) == (message_type, "why", f"{message_type}: why")


def test_close_classification() -> None:
    assert isinstance(classify_close(1008, "Invalid API key"), SttAuthError)
    assert isinstance(classify_close(1008, "Missing API key"), SttAuthError)
    assert classify_close(1008, "invalid keyterms: too many") == MINIMAL
    assert classify_close(1008, "Simulated rate_limited") is None
    assert classify_close(1011, "Invalid API key") is None
    assert classify_close(None, "") is None
    assert classify_close(1000, "") is None
