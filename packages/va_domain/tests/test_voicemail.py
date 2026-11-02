"""AMD mode, classifier switch and verdict mapping (specs/07 §14 Decision P05-T10c)."""

from __future__ import annotations

from typing import Any

import pytest
from resolved_configs import resolved_config

from va_domain.calls import SendCallConfig
from va_domain.voicemail import amd_mode, amd_verdict, classifier_enabled, verdict_of_answered_by


def config(**overrides: Any) -> SendCallConfig:
    return SendCallConfig.model_validate({**resolved_config(), **overrides})


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "premium"),
        ({"ivr_mode": True}, "disabled"),
        ({"answered_by_enabled": False}, "disabled"),
        ({"voicemail": {"action": "ignore", "sensitive": False}}, "disabled"),
        ({"voicemail": {"action": "ignore", "sensitive": True}}, "premium"),
        ({"voicemail": {"action": "leave_message", "sensitive": False}}, "premium"),
    ],
)
def test_amd_mode(overrides: dict[str, Any], expected: str) -> None:
    assert amd_mode(config(**overrides)) == expected


@pytest.mark.parametrize(
    ("overrides", "direction", "expected"),
    [
        ({"voicemail": {"action": "hangup", "sensitive": True}}, "outbound", True),
        ({"voicemail": {"action": "ignore", "sensitive": True}}, "outbound", True),
        ({"voicemail": {"action": "hangup", "sensitive": False}}, "outbound", False),
        ({"voicemail": {"action": "hangup", "sensitive": True}}, "inbound", False),
        ({"voicemail": {"action": "hangup", "sensitive": True}}, "web", False),
        ({"voicemail": {"action": "hangup", "sensitive": True}, "ivr_mode": True}, "outbound", False),
        (
            {"voicemail": {"action": "hangup", "sensitive": True}, "answered_by_enabled": False},
            "outbound",
            False,
        ),
    ],
)
def test_classifier_enabled(overrides: dict[str, Any], direction: str, expected: bool) -> None:
    assert classifier_enabled(config(**overrides), direction) is expected


@pytest.mark.parametrize(
    ("event_type", "result", "expected"),
    [
        ("call.machine.premium.detection.ended", "human_residence", "human"),
        ("call.machine.premium.detection.ended", "human_business", "human"),
        ("call.machine.premium.detection.ended", "machine", "machine"),
        ("call.machine.premium.detection.ended", "fax_detected", "fax"),
        ("call.machine.premium.detection.ended", "silence", "unknown"),
        ("call.machine.premium.detection.ended", None, "unknown"),
        ("call.machine.detection.ended", "human", "human"),
        ("call.machine.detection.ended", "machine", "machine"),
        ("call.machine.detection.ended", "not_sure", "unknown"),
        ("call.machine.detection.ended", None, "unknown"),
        ("call.machine.premium.greeting.ended", "beep_detected", None),
        ("call.answered", None, None),
    ],
)
def test_amd_verdict(event_type: str, result: str | None, expected: str | None) -> None:
    assert amd_verdict(event_type, result) == expected


@pytest.mark.parametrize(
    ("answered_by", "expected"),
    [
        ("human", "human"),
        ("voicemail", "machine"),
        ("fax", "fax"),
        ("unknown", "unknown"),
        ("ivr", "unknown"),
    ],
)
def test_verdict_of_answered_by(answered_by: str, expected: str) -> None:
    assert verdict_of_answered_by(answered_by) == expected
