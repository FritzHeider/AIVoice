"""Voicemail retry rules (specs/05 §3 ``retry``; specs/07 §14 Decision P05-T10f)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from resolved_configs import resolved_config

from va_domain.call_retry import (
    NO_ANSWER_END_REASONS,
    RETRY_ATTEMPT_KEY,
    VOICEMAIL_END_REASONS,
    child_config,
    retry_attempt,
    retry_wanted,
)
from va_domain.calls.config import RetryConfig, SendCallConfig

AT = datetime(2026, 11, 2, 15, 30, tzinfo=UTC)


@pytest.mark.parametrize("end_reason", sorted(VOICEMAIL_END_REASONS))
@pytest.mark.parametrize("on_no_answer", [False, True])
def test_voicemail_ends_always_ask_for_a_retry(end_reason: str, on_no_answer: bool) -> None:
    assert retry_wanted(RetryConfig(wait=60, on_no_answer=on_no_answer), end_reason) is True


@pytest.mark.parametrize("end_reason", sorted(NO_ANSWER_END_REASONS))
def test_no_answer_ends_ask_for_a_retry_only_with_on_no_answer(end_reason: str) -> None:
    assert retry_wanted(RetryConfig(wait=60, on_no_answer=True), end_reason) is True
    assert retry_wanted(RetryConfig(wait=60), end_reason) is False


@pytest.mark.parametrize("end_reason", ["user_hangup", "agent_end_call", "carrier_error", None])
def test_other_ends_never_ask_for_a_retry(end_reason: str | None) -> None:
    assert retry_wanted(RetryConfig(wait=60, on_no_answer=True), end_reason) is False


@pytest.mark.parametrize("end_reason", ["voicemail_left", "no_answer"])
def test_a_call_without_retry_never_asks(end_reason: str) -> None:
    assert retry_wanted(None, end_reason) is False


@pytest.mark.parametrize(
    ("stored", "attempt"),
    [
        ({}, 0),
        ({RETRY_ATTEMPT_KEY: 0}, 0),
        ({RETRY_ATTEMPT_KEY: 2}, 2),
        ({RETRY_ATTEMPT_KEY: "2"}, 0),
        ({RETRY_ATTEMPT_KEY: True}, 0),
        ({RETRY_ATTEMPT_KEY: -1}, 0),
        ({RETRY_ATTEMPT_KEY: None}, 0),
    ],
)
def test_the_attempt_number_reads_only_non_negative_integers(stored: dict[str, Any], attempt: int) -> None:
    assert retry_attempt(stored) == attempt


def _stored() -> dict[str, Any]:
    config: dict[str, Any] = resolved_config("01_task_basic.json")
    config["voicemail"] = {
        **config["voicemail"],
        "action": "leave_message_and_sms",
        "message": "Parent message.",
        "sensitive": True,
        "sms": {"from": "+14155550190", "message": "Call us back."},
    }
    config["retry"] = {"wait": 600, "voicemail_action": "leave_message", "max_attempts": 2}
    config["pricing"] = {"per_minute": 90_000}
    return config


def test_the_child_config_takes_the_retry_voicemail_settings() -> None:
    stored = _stored()
    original = copy.deepcopy(stored)
    retry = RetryConfig(wait=600, voicemail_action="hangup", voicemail_message="Retry message.")

    child = child_config(stored, retry, 1, AT)

    assert stored == original
    assert child["voicemail"] == {
        **stored["voicemail"],
        "action": "hangup",
        "message": "Retry message.",
    }
    assert (child["start_time"], child[RETRY_ATTEMPT_KEY]) == ("2026-11-02T15:30:00Z", 1)
    assert (child["retry"], child["pricing"]) == (stored["retry"], stored["pricing"])
    untouched = {key for key in stored if key not in {"voicemail", "start_time"}}
    assert {key: child[key] for key in untouched} == {key: stored[key] for key in untouched}


def test_a_retry_without_a_message_keeps_the_parent_message() -> None:
    child = child_config(_stored(), RetryConfig(wait=60, voicemail_action="leave_message"), 2, AT)
    voicemail: Any = child["voicemail"]

    assert (voicemail["action"], voicemail["message"]) == (
        "leave_message",
        "Parent message.",
    )
    assert child[RETRY_ATTEMPT_KEY] == 2


def test_the_start_time_is_written_in_utc() -> None:
    local = AT.astimezone(timezone(timedelta(hours=-5)))

    assert child_config(_stored(), RetryConfig(wait=60), 1, local)["start_time"] == "2026-11-02T15:30:00Z"


def test_a_config_without_voicemail_gets_one() -> None:
    stored = {key: value for key, value in _stored().items() if key != "voicemail"}

    child = child_config(stored, RetryConfig(wait=60, voicemail_message="Hi."), 1, AT)

    assert child["voicemail"] == {"action": "hangup", "message": "Hi."}


def test_the_child_config_is_a_valid_call_configuration() -> None:
    child = child_config(_stored(), RetryConfig(wait=60, voicemail_action="ignore"), 1, AT)
    fields = SendCallConfig.model_fields

    config = SendCallConfig.model_validate({key: value for key, value in child.items() if key in fields})

    assert (config.voicemail.action, config.start_time) == ("ignore", AT)
    assert config.retry == RetryConfig(wait=600, voicemail_action="leave_message", max_attempts=2)
