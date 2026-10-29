"""TOTP multi-factor authentication and recovery codes (specs/04-auth-tenancy-rbac.md §4.5).

TOTP follows RFC 6238 through ``pyotp`` (6 digits, 30 s steps, ±1 step window); matching returns the accepted
time-step counter so the caller can refuse a replay (``mfa:last:<user_id>``, specs/03-data-model.md §11).
Recovery codes are 12 lowercase base32 characters shown as ``xxxx-xxxx-xxxx`` and stored as argon2id hashes of
that canonical form; hashing and matching run sequentially in one worker thread, because each argon2id call
takes about 64 MiB and parallel threads would multiply it.
"""

from __future__ import annotations

import asyncio
import hmac
import re
import secrets
from collections.abc import Sequence
from datetime import datetime
from typing import Final

import pyotp
import segno

from va_common.auth.passwords import PasswordHasher

__all__ = [
    "RECOVERY_CODE_ALPHABET",
    "RECOVERY_CODE_COUNT",
    "TOTP_DIGITS",
    "TOTP_INTERVAL_S",
    "TOTP_ISSUER",
    "TOTP_SECRET_LENGTH",
    "TOTP_VALID_WINDOW",
    "hash_recovery_codes",
    "match_recovery_code",
    "match_totp",
    "new_recovery_codes",
    "new_totp_secret",
    "normalise_recovery_code",
    "otpauth_uri",
    "qr_svg",
    "totp_code",
    "totp_counter",
]

TOTP_DIGITS: Final = 6
TOTP_INTERVAL_S: Final = 30
TOTP_VALID_WINDOW: Final = 1
TOTP_ISSUER: Final = "VoiceAgents"
# 32 base32 characters = 160 bits, the RFC 4226 recommended HMAC-SHA1 key length.
TOTP_SECRET_LENGTH: Final = 32
RECOVERY_CODE_COUNT: Final = 10
RECOVERY_CODE_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz234567"
RECOVERY_CODE_GROUP: Final = 4
RECOVERY_CODE_LENGTH: Final = 12
QR_ERROR_LEVEL: Final = "m"
QR_SCALE: Final = 4

_TOTP_CODE_RE: Final = re.compile(r"[0-9]{6}", re.ASCII)
_RECOVERY_CODE_RE: Final = re.compile(rf"[a-z2-7]{{{RECOVERY_CODE_LENGTH}}}", re.ASCII)
_WHITESPACE_RE: Final = re.compile(r"\s+")


def _totp(secret: str) -> pyotp.TOTP:
    return pyotp.TOTP(secret, digits=TOTP_DIGITS, interval=TOTP_INTERVAL_S)


def new_totp_secret() -> str:
    """A fresh random base32 TOTP secret of :data:`TOTP_SECRET_LENGTH` characters."""
    return pyotp.random_base32(TOTP_SECRET_LENGTH)


def otpauth_uri(secret: str, account: str) -> str:
    """The ``otpauth://totp/VoiceAgents:<account>?secret=...&issuer=VoiceAgents`` provisioning URI."""
    return _totp(secret).provisioning_uri(name=account, issuer_name=TOTP_ISSUER)


def qr_svg(data: str) -> str:
    """An inline SVG QR code of ``data`` (error level M, scale 4, no XML declaration)."""
    return segno.make(data, error=QR_ERROR_LEVEL).svg_inline(scale=QR_SCALE)


def totp_counter(now: datetime) -> int:
    """The RFC 6238 time-step counter of ``now``; ``ValueError`` for a naive datetime."""
    if now.tzinfo is None or now.utcoffset() is None:
        msg = "totp_counter needs a timezone-aware datetime"
        raise ValueError(msg)
    return int(now.timestamp()) // TOTP_INTERVAL_S


def totp_code(secret: str, counter: int) -> str:
    """The 6-digit code of ``secret`` for time step ``counter``."""
    return _totp(secret).generate_otp(counter)


def match_totp(secret: str, code: str, now: datetime) -> int | None:
    """The time-step counter whose code equals ``code`` within ±1 step of ``now``, else ``None``.

    Whitespace in ``code`` is ignored; anything but exactly six ASCII digits never matches. Every step in the
    window is compared in constant time, without returning early, so timing does not reveal which one matched.
    """
    candidate = _WHITESPACE_RE.sub("", code)
    if _TOTP_CODE_RE.fullmatch(candidate) is None:
        return None
    current = totp_counter(now)
    matched: int | None = None
    for counter in range(current - TOTP_VALID_WINDOW, current + TOTP_VALID_WINDOW + 1):
        if counter >= 0 and hmac.compare_digest(totp_code(secret, counter), candidate):
            matched = counter
    return matched


def _hyphenate(compact: str) -> str:
    groups = (compact[i : i + RECOVERY_CODE_GROUP] for i in range(0, len(compact), RECOVERY_CODE_GROUP))
    return "-".join(groups)


def new_recovery_codes() -> list[str]:
    """:data:`RECOVERY_CODE_COUNT` distinct random codes in ``xxxx-xxxx-xxxx`` form (60 bits each)."""
    codes: list[str] = []
    while len(codes) < RECOVERY_CODE_COUNT:
        compact = "".join(secrets.choice(RECOVERY_CODE_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
        code = _hyphenate(compact)
        if code not in codes:
            codes.append(code)
    return codes


def normalise_recovery_code(raw: str) -> str | None:
    """``raw`` in canonical ``xxxx-xxxx-xxxx`` form (case, whitespace, and hyphens ignored), else ``None``."""
    compact = _WHITESPACE_RE.sub("", raw).replace("-", "").casefold()
    if _RECOVERY_CODE_RE.fullmatch(compact) is None:
        return None
    return _hyphenate(compact)


async def hash_recovery_codes(hasher: PasswordHasher, codes: Sequence[str]) -> list[str]:
    """argon2id hashes of ``codes`` (canonical form), computed one after another in a worker thread."""
    canonical = list(codes)

    def run() -> list[str]:
        return [hasher.hash(code) for code in canonical]

    return await asyncio.to_thread(run)


async def match_recovery_code(hasher: PasswordHasher, hashes: Sequence[str], raw: str) -> str | None:
    """The stored hash in ``hashes`` that ``raw`` matches after normalising, else ``None``.

    A malformed ``raw`` returns ``None`` without hashing (the format is public, so this reveals nothing).
    """
    code = normalise_recovery_code(raw)
    if code is None:
        return None
    stored = list(hashes)

    def run() -> str | None:
        return next((candidate for candidate in stored if hasher.verify(candidate, code)), None)

    return await asyncio.to_thread(run)
