"""Request signing of tools (specs/13-tools-and-dynamic-data.md §5 step 4).

A tool request carries the specs/19 §8 signature headers (``X-VA-Signature: t=<unix>,v1=<hmac>[,v1=…]`` over
``"{t}.{body}"`` with every active org webhook secret, plus the Bland-style header over the body alone), so a
tool endpoint verifies it exactly like a webhook endpoint.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from va_common.time import Clock
from va_events.webhook_signing import signature_headers
from va_tools.http import Signer

__all__ = ["tool_signer"]


def tool_signer(secrets: Sequence[str], clock: Clock) -> Signer:
    """A :data:`~va_tools.http.Signer` over ``secrets`` (current first) stamped with ``clock``'s time."""
    if not secrets:
        msg = "at least one signing secret is required"
        raise ValueError(msg)
    kept = tuple(secrets)

    def sign(body: bytes) -> Mapping[str, str]:
        return signature_headers(kept, body, int(clock.now().timestamp()))

    return sign
