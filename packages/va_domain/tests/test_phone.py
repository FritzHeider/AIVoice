"""Destination parsing to E.164 (specs/05 §3.2)."""

from __future__ import annotations

import pytest

from va_domain.phone import InvalidPhoneNumberError, normalize_e164


@pytest.mark.parametrize(
    ("value", "region", "expected"),
    [
        ("+14155550100", "US", "+14155550100"),
        ("(415) 555-0100", "US", "+14155550100"),
        ("415.555.0100", "us", "+14155550100"),
        ("1 415 555 0100", "US", "+14155550100"),
        ("  +1 415-555-0100 ", "GB", "+14155550100"),
        ("+44 20 7946 0958", "US", "+442079460958"),
        ("020 7946 0958", "GB", "+442079460958"),
        ("030 123456", "DE", "+4930123456"),
    ],
)
def test_valid_numbers_normalize_to_e164(value: str, region: str, expected: str) -> None:
    assert normalize_e164(value, region) == expected


@pytest.mark.parametrize(
    ("value", "region"),
    [
        ("", "US"),
        ("   ", "US"),
        ("1-800-FLOWERS", "US"),
        ("call me", "US"),
        ("12345", "US"),
        ("+1 555 0100", "US"),
        ("020 7946 0958", "US"),
        ("+999 1234 5678", "US"),
        ("+1415555010000000", "US"),
    ],
)
def test_invalid_numbers_are_rejected(value: str, region: str) -> None:
    with pytest.raises(InvalidPhoneNumberError):
        normalize_e164(value, region)


def test_default_region_is_us_and_error_is_a_value_error() -> None:
    assert normalize_e164("4155550100") == "+14155550100"
    with pytest.raises(ValueError, match=r"cannot be parsed|not a valid"):
        normalize_e164("+0")
