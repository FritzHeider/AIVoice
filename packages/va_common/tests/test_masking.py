from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from va_common.masking import REDACTED, is_secret_key, mask_email, mask_phone, mask_phones_in_text


@pytest.mark.parametrize(
    ("number", "masked"),
    [
        ("+14155550123", "+1415***0123"),
        ("+442071838750", "+4420***8750"),
        ("+123456789012345", "+1234***2345"),
        ("+12345678", "+12***78"),
        ("+1234567", "+12***67"),
        ("+1234567890", "+12***90"),
    ],
)
def test_mask_phone_e164(number: str, masked: str) -> None:
    assert mask_phone(number) == masked


@pytest.mark.parametrize(
    "value",
    ["4155550123", "+0123456789", "+1-415-555-0123", "+123456", "+1234567890123456", "", "call me"],
)
def test_mask_phone_leaves_non_e164_unchanged(value: str) -> None:
    assert mask_phone(value) == value


def test_mask_phones_in_text_masks_every_number() -> None:
    text = "from +14155550123 to +442071838750, done"
    assert mask_phones_in_text(text) == "from +1415***0123 to +4420***8750, done"


def test_mask_phones_in_text_inside_url() -> None:
    url = "https://x.example/cb?to=+14155550123&from=%2B1"
    assert mask_phones_in_text(url) == "https://x.example/cb?to=+1415***0123&from=%2B1"


@pytest.mark.parametrize(
    "text",
    ["+12345678901234567", "1+14155550123", "++14155550123", "order 14155550123", "no numbers here"],
)
def test_mask_phones_in_text_ignores_non_numbers(text: str) -> None:
    assert mask_phones_in_text(text) == text


@given(st.from_regex(r"\+[1-9]\d{10,14}", fullmatch=True))
def test_mask_phone_hides_middle_digits(number: str) -> None:
    masked = mask_phone(number)
    assert masked.startswith(number[:5])
    assert masked.endswith(number[-4:])
    assert masked == f"{number[:5]}***{number[-4:]}"
    assert len(masked) == len("+1415***0123")  # the middle digits are gone whatever the length


def test_redacted_marker() -> None:
    assert REDACTED == "[REDACTED]"


@pytest.mark.parametrize(
    "name",
    [
        "api_key",
        "xi-api-key",
        "XI-API-KEY",
        "client_secret",
        "access_token",
        "Authorization",
        "authorization",
        "password",
        "db.passwd",
        "sentry_dsn",
        "api_key_pepper",
        "Set-Cookie",
        "aws credentials",
        "secret",
        "token",
        "apiKey",
        "accessToken",
        "clientSecret",
        "secretKey",
        "webhookSecret",
        "apikey",
        "APIKey",
        "XApiKey",
        "refreshtoken",
        "dbPassword",
        "dbpassword",
    ],
)
def test_is_secret_key_true(name: str) -> None:
    assert is_secret_key(name)


@pytest.mark.parametrize(
    "name",
    [
        "api_key_prefix",
        "key_id",
        "pepper_version",
        "prompt_tokens",
        "completion_tokens",
        "turn_index",
        "monkey",
        "keyboard",
        "secret_count",
        "token_type",
        "",
        "__",
        "promptTokens",
        "apiKeyPrefix",
        "keyId",
        "turkey",
        "hotkey",
        "tokenizer",
        "Monkey",
    ],
)
def test_is_secret_key_false(name: str) -> None:
    assert not is_secret_key(name)


@pytest.mark.parametrize(
    ("address", "masked"),
    [
        ("jane.doe@example.com", "j***@example.com"),
        ("a@b.io", "a***@b.io"),
        ("odd@local@example.com", "o***@example.com"),
        ("no-at-sign", "***"),
        ("@example.com", "***"),
        ("", "***"),
    ],
)
def test_mask_email(address: str, masked: str) -> None:
    assert mask_email(address) == masked
