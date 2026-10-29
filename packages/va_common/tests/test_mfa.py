"""TOTP matching, QR provisioning, and recovery codes (specs/04-auth-tenancy-rbac.md §4.5)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, unquote, urlsplit

import pyotp
import pytest

from va_common.auth import (
    RECOVERY_CODE_COUNT,
    TOTP_INTERVAL_S,
    Argon2Params,
    PasswordHasher,
    hash_recovery_codes,
    match_recovery_code,
    match_totp,
    new_recovery_codes,
    new_totp_secret,
    normalise_recovery_code,
    otpauth_uri,
    qr_svg,
    totp_code,
    totp_counter,
)

CHEAP = Argon2Params(time_cost=1, memory_cost=8, parallelism=1, hash_len=16, salt_len=8)
# A step boundary: 2026-10-29T12:00:00Z is divisible by 30 s.
NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=UTC)
ACCOUNT = "ada@voiceagents.test"
RECOVERY_RE = re.compile(r"^[a-z2-7]{4}-[a-z2-7]{4}-[a-z2-7]{4}$")


@pytest.fixture(scope="module")
def secret() -> str:
    return new_totp_secret()


@pytest.fixture(scope="module")
def hasher() -> PasswordHasher:
    return PasswordHasher(CHEAP)


def test_new_totp_secret_is_32_base32_chars_and_unique() -> None:
    secrets_ = {new_totp_secret() for _ in range(20)}
    assert len(secrets_) == 20
    for value in secrets_:
        assert re.fullmatch(r"[A-Z2-7]{32}", value)


def test_otpauth_uri(secret: str) -> None:
    uri = otpauth_uri(secret, ACCOUNT)
    parts = urlsplit(uri)
    assert (parts.scheme, parts.netloc) == ("otpauth", "totp")
    assert unquote(parts.path) == f"/VoiceAgents:{ACCOUNT}"
    query = parse_qs(parts.query)
    assert query["secret"] == [secret]
    assert query["issuer"] == ["VoiceAgents"]
    parsed = pyotp.parse_uri(uri)
    assert isinstance(parsed, pyotp.TOTP)
    assert (parsed.secret, parsed.digits, parsed.interval) == (secret, 6, 30)


def test_qr_svg_is_inline_svg(secret: str) -> None:
    uri = otpauth_uri(secret, ACCOUNT)
    svg = qr_svg(uri)
    assert svg.startswith("<svg")
    assert "<?xml" not in svg
    assert "path" in svg
    assert qr_svg(uri) == svg
    assert qr_svg(otpauth_uri(new_totp_secret(), ACCOUNT)) != svg


def test_totp_counter_requires_aware_datetime() -> None:
    assert totp_counter(NOW) == int(NOW.timestamp()) // TOTP_INTERVAL_S
    with pytest.raises(ValueError, match="timezone-aware"):
        totp_counter(datetime(2026, 10, 29, 12, 0, 0))  # noqa: DTZ001 - the naive input under test


def test_totp_code_matches_pyotp(secret: str) -> None:
    assert totp_code(secret, totp_counter(NOW)) == pyotp.TOTP(secret).at(NOW)


@pytest.mark.parametrize(("offset", "accepted"), [(-2, False), (-1, True), (0, True), (1, True), (2, False)])
def test_match_totp_window(secret: str, offset: int, accepted: bool) -> None:
    counter = totp_counter(NOW) + offset
    assert match_totp(secret, totp_code(secret, counter), NOW) == (counter if accepted else None)


def test_match_totp_step_boundaries(secret: str) -> None:
    start = totp_counter(NOW)
    last_second_before = NOW - timedelta(seconds=1)
    assert totp_counter(last_second_before) == start - 1
    # At the last second of step T-1, T+1 is out of the window and T-2 is in it.
    assert match_totp(secret, totp_code(secret, start + 1), last_second_before) is None
    assert match_totp(secret, totp_code(secret, start - 2), last_second_before) == start - 2
    assert match_totp(secret, totp_code(secret, start - 2), NOW) is None


def test_match_totp_ignores_whitespace(secret: str) -> None:
    code = totp_code(secret, totp_counter(NOW))
    assert match_totp(secret, f" {code[:3]} {code[3:]} ", NOW) == totp_counter(NOW)


@pytest.mark.parametrize(
    "code",
    [
        "",
        "12345",
        "1234567",
        "12a456",
        "\uff11\uff12\uff13\uff14\uff15\uff16",  # full-width digits
        "-12345",
        "12.456",
    ],
)
def test_match_totp_rejects_malformed(secret: str, code: str) -> None:
    assert match_totp(secret, code, NOW) is None


def test_match_totp_at_the_epoch(secret: str) -> None:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    assert match_totp(secret, totp_code(secret, 0), epoch) == 0
    assert match_totp(secret, totp_code(secret, 1), epoch) == 1


def test_new_recovery_codes_format_and_distinct() -> None:
    codes = new_recovery_codes()
    assert len(codes) == RECOVERY_CODE_COUNT == 10
    assert len(set(codes)) == 10
    for code in codes:
        assert RECOVERY_RE.fullmatch(code)
    assert set(new_recovery_codes()).isdisjoint(codes)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abcd-efgh-ijkl", "abcd-efgh-ijkl"),
        ("ABCD-EFGH-IJKL", "abcd-efgh-ijkl"),
        ("abcdefghijkl", "abcd-efgh-ijkl"),
        ("  ab cd-ef gh - ij kl\n", "abcd-efgh-ijkl"),
        ("a2b3-c4d5-e6f7", "a2b3-c4d5-e6f7"),
        ("abcd-efgh-ijk", None),
        ("abcd-efgh-ijklm", None),
        ("abcd-efgh-ijk0", None),
        ("abcd-efgh-ijk1", None),
        ("abcd-efgh-ijk8", None),
        ("", None),
        ("\uff41bcd-efgh-ijkl", None),
    ],
)
def test_normalise_recovery_code(raw: str, expected: str | None) -> None:
    assert normalise_recovery_code(raw) == expected


async def test_hash_and_match_recovery_codes(hasher: PasswordHasher) -> None:
    codes = new_recovery_codes()[:3]
    hashes = await hash_recovery_codes(hasher, codes)
    assert len(hashes) == 3
    for stored, code in zip(hashes, codes, strict=True):
        assert stored.startswith("$argon2id$")
        assert code not in stored
        assert code.replace("-", "") not in stored
    assert await match_recovery_code(hasher, hashes, codes[1]) == hashes[1]
    assert await match_recovery_code(hasher, hashes, codes[2].upper().replace("-", " ")) == hashes[2]
    assert await match_recovery_code(hasher, hashes, new_recovery_codes()[0]) is None
    assert await match_recovery_code(hasher, hashes, "not-a-code") is None
    assert await match_recovery_code(hasher, [], codes[0]) is None
