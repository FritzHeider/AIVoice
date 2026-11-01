"""Send Call ``webhook`` checks (specs/05-calls-api.md §3.7; specs/19 §8 Decision P05-T05a).

The dispatch checks the *resolved* webhook (request, runner, or org ``default_webhook``) at request
time: ``https`` only, unless ``VA_ENV`` is ``local``/``test`` or the org enables
``allow_insecure_webhooks``; private targets only where ``WEBHOOK_ALLOW_PRIVATE`` holds (never in
staging/production). Any refusal is ``WEBHOOK_URL_REJECTED`` on field ``webhook``.
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

__all__ = ["INSECURE_ENVIRONMENTS", "MESSAGES", "check_call_webhook"]

INSECURE_ENVIRONMENTS: Final = frozenset({Environment.LOCAL, Environment.TEST})
MESSAGES: Final[dict[UnsafeReason, str]] = {
    "scheme": "webhook must use https.",
    "host": "webhook must be an absolute http(s) URL with a host.",
    "resolution": "The webhook host could not be resolved.",
    "address": "The webhook URL resolves to a blocked address.",
}


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
    try:
        await check_destination_url(
            url,
            allow_http=settings.va_env in INSECURE_ENVIRONMENTS or org.allow_insecure_webhooks,
            allow_private=settings.webhook_allow_private,
            resolver=resolver,
        )
    except UnsafeDestinationError as exc:
        raise AppError("WEBHOOK_URL_REJECTED", MESSAGES[exc.reason], field="webhook") from exc
