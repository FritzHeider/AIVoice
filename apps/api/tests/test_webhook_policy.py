"""Send Call ``webhook`` policy (specs/05-calls-api.md §3.7; specs/19 §8 Decision P05-T05a)."""

from __future__ import annotations

import secrets

import pytest
from api_helpers import make_settings

from va_api.services.webhook_policy import MESSAGES, check_call_webhook
from va_api.settings import ApiSettings
from va_common.errors import AppError
from va_common.net import HostResolver
from va_domain.org import OrgSettings

ANSWERS = {
    "hooks.example.com": ["8.8.8.8"],
    "intranet.example.com": ["10.0.0.7"],
    "localhost": ["127.0.0.1"],
}


async def resolver(host: str) -> list[str]:
    if host not in ANSWERS:
        raise OSError(host)
    return ANSWERS[host]


def settings_for(env: str, **overrides: object) -> ApiSettings:
    if env in {"staging", "production"}:
        overrides = {
            "kms_provider": "aws",
            "aws_kms_key_id": f"alias/{secrets.token_hex(4)}",
            "smtp_url": "smtps://smtp.example.com",
            **overrides,
        }
    return make_settings(va_env=env, **overrides)


async def rejected(
    url: str, settings: ApiSettings, org: OrgSettings | None = None, resolve: HostResolver = resolver
) -> AppError:
    with pytest.raises(AppError) as caught:
        await check_call_webhook(url, org=org or OrgSettings(), settings=settings, resolver=resolve)
    error = caught.value
    assert (error.code, error.http_status, error.field) == ("WEBHOOK_URL_REJECTED", 400, "webhook")
    return error


async def test_absent_webhook_is_not_checked() -> None:
    async def never(host: str) -> list[str]:
        raise AssertionError(host)

    await check_call_webhook(None, org=OrgSettings(), settings=settings_for("production"), resolver=never)


@pytest.mark.parametrize("env", ["local", "test", "staging", "production"])
async def test_public_https_accepted_everywhere(env: str) -> None:
    await check_call_webhook(
        "https://hooks.example.com/calls", org=OrgSettings(), settings=settings_for(env), resolver=resolver
    )


@pytest.mark.parametrize("env", ["local", "test"])
async def test_http_allowed_in_local_and_test(env: str) -> None:
    await check_call_webhook(
        "http://hooks.example.com/calls", org=OrgSettings(), settings=settings_for(env), resolver=resolver
    )


@pytest.mark.parametrize("env", ["staging", "production"])
async def test_http_rejected_outside_dev_unless_org_allows(env: str) -> None:
    error = await rejected("http://hooks.example.com/calls", settings_for(env))
    assert error.message == MESSAGES["scheme"] == "webhook must use https."
    await check_call_webhook(
        "http://hooks.example.com/calls",
        org=OrgSettings(allow_insecure_webhooks=True),
        settings=settings_for(env),
        resolver=resolver,
    )


async def test_private_target_allowed_where_webhook_allow_private_holds() -> None:
    settings = settings_for("test")
    assert settings.webhook_allow_private is True
    await check_call_webhook(
        "http://localhost:9100/hook", org=OrgSettings(), settings=settings, resolver=resolver
    )


@pytest.mark.parametrize(("env", "allow"), [("production", True), ("staging", True), ("test", False)])
async def test_private_target_rejected_without_webhook_allow_private(env: str, *, allow: bool) -> None:
    settings = settings_for(env, webhook_allow_private=allow)
    assert settings.webhook_allow_private is False
    error = await rejected("https://intranet.example.com/hook", settings)
    assert error.message == "The webhook URL resolves to a blocked address."


async def test_metadata_address_rejected_even_in_test() -> None:
    error = await rejected("http://169.254.169.254/latest/meta-data", settings_for("test"))
    assert error.message == MESSAGES["address"]


async def test_unresolvable_host() -> None:
    error = await rejected("https://gone.example.com/hook", settings_for("production"))
    assert error.message == "The webhook host could not be resolved."


@pytest.mark.parametrize("url", ["https:///hook", "https://[::1"])
async def test_missing_host(url: str) -> None:
    error = await rejected(url, settings_for("production"))
    assert error.message == "webhook must be an absolute http(s) URL with a host."


async def test_error_chains_the_guard_reason() -> None:
    error = await rejected("ftp://hooks.example.com", settings_for("test"))
    assert error.message == MESSAGES["scheme"]
    assert error.__cause__ is not None
    assert getattr(error.__cause__, "reason", None) == "scheme"
