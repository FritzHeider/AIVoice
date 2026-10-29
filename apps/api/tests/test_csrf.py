"""CSRF double submit and Origin/Referer checks (specs/04-auth-tenancy-rbac.md §4.4)."""

from __future__ import annotations

import pytest

from va_api.auth.csrf import CSRF_INVALID, ORIGIN_NOT_ALLOWED, check_csrf, check_origin, origin_of
from va_common.auth import generate_token
from va_common.errors import AppError

DASHBOARD = "https://app.voiceagents.test"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://App.VoiceAgents.test", "https://app.voiceagents.test"),
        ("https://app.voiceagents.test:443/calls?x=1#y", "https://app.voiceagents.test"),
        ("http://localhost:80/", "http://localhost"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("HTTPS://app.voiceagents.test:8443", "https://app.voiceagents.test:8443"),
        ("http://[::1]:3000/x", "http://[::1]:3000"),
        ("null", None),
        ("", None),
        ("ftp://app.voiceagents.test", None),
        ("https://", None),
        ("http://localhost:99999", None),
        ("not a url", None),
    ],
)
def test_origin_of(url: str, expected: str | None) -> None:
    assert origin_of(url) == expected


def test_check_csrf() -> None:
    token = generate_token()
    check_csrf(token, token)
    for header, cookie in ((None, token), (token, None), ("", ""), (generate_token(), token)):
        with pytest.raises(AppError) as info:
            check_csrf(header, cookie)
        assert (info.value.code, info.value.message, info.value.http_status) == (
            "FORBIDDEN",
            CSRF_INVALID,
            403,
        )


def test_check_origin_accepts_dashboard() -> None:
    check_origin(DASHBOARD, None, DASHBOARD, required=True)
    check_origin("https://app.voiceagents.test:443", "https://evil.test/", DASHBOARD + "/", required=True)
    check_origin(None, DASHBOARD + "/settings/keys", DASHBOARD, required=True)
    check_origin(None, None, DASHBOARD, required=False)


@pytest.mark.parametrize(
    ("origin", "referer", "required"),
    [
        ("https://evil.test", None, True),
        ("https://evil.test", None, False),
        (None, "https://evil.test/page", False),
        ("null", DASHBOARD, True),
        ("http://app.voiceagents.test", None, True),
        (None, None, True),
    ],
)
def test_check_origin_rejects(origin: str | None, referer: str | None, *, required: bool) -> None:
    with pytest.raises(AppError) as info:
        check_origin(origin, referer, DASHBOARD, required=required)
    assert (info.value.code, info.value.message) == ("FORBIDDEN", ORIGIN_NOT_ALLOWED)
