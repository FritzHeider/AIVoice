"""Send Call ``webhook`` checks (specs/05-calls-api.md §3.7; specs/19 §8 Decision P05-T05a).

The dispatch checks the *resolved* webhook (request, runner, or org ``default_webhook``) at request
time: ``https`` only, unless ``VA_ENV`` is ``local``/``test`` or the org enables
``allow_insecure_webhooks``; private targets only where ``WEBHOOK_ALLOW_PRIVATE`` holds (never in
staging/production). Any refusal is ``WEBHOOK_URL_REJECTED`` on field ``webhook``. Org webhook endpoints
(specs/19 §10 Decision P06-T04b) apply the same rules through :func:`check_webhook_url` on field ``url``.
"""

from __future__ import annotations

from typing import Final

from va_api.settings import ApiSettings
from va_common.errors import AppError
from va_common.net import (
    HostResolver,
    UnsafeDestinationError,
    UnsafeReason,
    check_destination_url,
    system_resolver,
)
from va_common.settings import Environment
from va_domain.org import OrgSettings

__all__ = [
    "INSECURE_ENVIRONMENTS",
    "MESSAGES",
    "check_call_webhook",
    "check_webhook_url",
    "url_policy",
    "webhook_url_messages",
]

INSECURE_ENVIRONMENTS: Final = frozenset({Environment.LOCAL, Environment.TEST})


def webhook_url_messages(field: str) -> dict[UnsafeReason, str]:
    """The refusal messages of a webhook URL sent in ``field``."""
    return {
        "scheme": f"{field} must use https.",
        "host": f"{field} must be an absolute http(s) URL with a host.",
        "resolution": f"The {field} host could not be resolved.",
        "address": f"The {field} URL resolves to a blocked address.",
    }


MESSAGES: Final[dict[UnsafeReason, str]] = webhook_url_messages("webhook")


def url_policy(*, org: OrgSettings, settings: ApiSettings) -> tuple[bool, bool]:
    """``(allow_http, allow_private)`` for the org's webhook URLs."""
    allow_http = settings.va_env in INSECURE_ENVIRONMENTS or org.allow_insecure_webhooks
    return allow_http, settings.webhook_allow_private


async def check_webhook_url(
    url: str,
    *,
    org: OrgSettings,
    settings: ApiSettings,
    field: str,
    resolver: HostResolver = system_resolver,
) -> None:
    """Return when ``url`` is acceptable; else raise ``WEBHOOK_URL_REJECTED`` on ``field``."""
    allow_http, allow_private = url_policy(org=org, settings=settings)
    try:
        await check_destination_url(
            url, allow_http=allow_http, allow_private=allow_private, resolver=resolver
        )
    except UnsafeDestinationError as exc:
        raise AppError("WEBHOOK_URL_REJECTED", webhook_url_messages(field)[exc.reason], field=field) from exc


async def check_call_webhook(
    url: str | None,
    *,
    org: OrgSettings,
    settings: ApiSettings,
    resolver: HostResolver = system_resolver,
) -> None:
    """Return when ``url`` is absent or acceptable; else raise ``WEBHOOK_URL_REJECTED``."""
    if url is None:
        return
    await check_webhook_url(url, org=org, settings=settings, field="webhook", resolver=resolver)
