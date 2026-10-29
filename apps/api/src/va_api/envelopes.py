"""Response-envelope families (specs/31-error-catalog.md §1 and §4).

Every operation carries ``x-envelope`` (``legacy`` or ``envelope``) in its OpenAPI extension, set by the route
class of the router it is declared on: build routers only with :func:`legacy_router` or
:func:`envelope_router`. Requests that match no route (404, unknown paths) are classified from the §1 path
patterns.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from typing import Any, ClassVar, Final

from fastapi import APIRouter
from fastapi.routing import APIRoute
from starlette.types import Scope

__all__ = [
    "ENVELOPE_KEY",
    "FAMILIES",
    "Envelope",
    "EnvelopeRoute",
    "EnvelopedRoute",
    "LegacyRoute",
    "classify_path",
    "envelope_router",
    "legacy_router",
    "route_envelope",
]

ENVELOPE_KEY: Final = "x-envelope"


class Envelope(StrEnum):
    LEGACY = "legacy"
    ENVELOPE = "envelope"


# specs/31 §1 envelope matrix, verbatim. A trailing `*` is a raw prefix match; otherwise an exact match.
FAMILIES: Final[tuple[tuple[str, Envelope], ...]] = (
    ("/v1/calls*", Envelope.LEGACY),
    ("/v1/inbound*", Envelope.LEGACY),
    ("/numbers/*", Envelope.LEGACY),
    ("/v1/pathway*", Envelope.LEGACY),
    ("/v1/tools*", Envelope.LEGACY),
    ("/v1/voices*", Envelope.LEGACY),
    ("/v1/accounts*", Envelope.LEGACY),
    ("/v1/orgs*", Envelope.LEGACY),
    ("/v1/prompts*", Envelope.LEGACY),
    ("/v2/batches*", Envelope.LEGACY),
    ("/v1/knowledge*", Envelope.ENVELOPE),
    ("/v1/sms*", Envelope.ENVELOPE),
    ("/v1/widget*", Envelope.ENVELOPE),
    ("/v1/memory*", Envelope.ENVELOPE),
    ("/v1/personas*", Envelope.ENVELOPE),
    ("/v1/agents*", Envelope.ENVELOPE),
    ("/v1/citation_schemas*", Envelope.ENVELOPE),
    ("/v1/evals*", Envelope.ENVELOPE),
    ("/v1/webhooks*", Envelope.ENVELOPE),
    ("/v1/dispositions*", Envelope.ENVELOPE),
    ("/v1/usage*", Envelope.ENVELOPE),
    ("/v1/billing*", Envelope.ENVELOPE),
    ("/v1/dnc*", Envelope.ENVELOPE),
    ("/v1/contacts*", Envelope.ENVELOPE),
    ("/v1/number_pools*", Envelope.ENVELOPE),
    ("/v1/experiments*", Envelope.ENVELOPE),
    ("/v1/me", Envelope.ENVELOPE),
    ("/v1/org*", Envelope.ENVELOPE),
    ("/v1/api_keys*", Envelope.ENVELOPE),
    ("/v1/audit_logs*", Envelope.ENVELOPE),
    ("/v1/agent-onboarding*", Envelope.ENVELOPE),
    ("/internal/*", Envelope.ENVELOPE),
)


def _matches(pattern: str, path: str) -> bool:
    if not pattern.endswith("*"):
        return path == pattern
    prefix = pattern[:-1]
    # `/numbers/*` and `/internal/*` also cover the bare collection path.
    return path.startswith(prefix) or (prefix.endswith("/") and path == prefix[:-1])


def classify_path(path: str) -> Envelope:
    """Envelope family of ``path``: the longest matching §1 pattern wins; unknown paths are legacy (Bland)."""
    best: tuple[int, Envelope] | None = None
    for pattern, envelope in FAMILIES:
        if _matches(pattern, path) and (best is None or len(pattern) > best[0]):
            best = (len(pattern), envelope)
    return Envelope.LEGACY if best is None else best[1]


class EnvelopeRoute(APIRoute):
    """An ``APIRoute`` that stamps its family into ``openapi_extra["x-envelope"]``."""

    envelope: ClassVar[Envelope]

    def __init__(self, path: str, endpoint: Callable[..., Any], **kwargs: Any) -> None:
        extra: dict[str, Any] = dict(kwargs.get("openapi_extra") or {})
        declared = extra.get(ENVELOPE_KEY)
        if declared is not None and declared != self.envelope.value:
            msg = f"route {path} declares {ENVELOPE_KEY}={declared!r} on a {self.envelope.value} router"
            raise ValueError(msg)
        extra[ENVELOPE_KEY] = self.envelope.value
        kwargs["openapi_extra"] = extra
        super().__init__(path, endpoint, **kwargs)


class LegacyRoute(EnvelopeRoute):
    envelope: ClassVar[Envelope] = Envelope.LEGACY


class EnvelopedRoute(EnvelopeRoute):
    envelope: ClassVar[Envelope] = Envelope.ENVELOPE


def legacy_router(**kwargs: Any) -> APIRouter:
    """Router for a legacy (Bland-shaped) family; pass ``prefix=`` here, never to ``include_router``."""
    return APIRouter(route_class=LegacyRoute, **kwargs)


def envelope_router(**kwargs: Any) -> APIRouter:
    """Router for an envelope family; pass ``prefix=`` here, never to ``include_router``."""
    return APIRouter(route_class=EnvelopedRoute, **kwargs)


def route_envelope(scope: Scope) -> Envelope:
    """Envelope of the matched route (``scope["route"]``), else of the request path."""
    route = scope.get("route")
    if isinstance(route, APIRoute):
        declared = (route.openapi_extra or {}).get(ENVELOPE_KEY)
        if declared is not None:
            return Envelope(declared)
    return classify_path(str(scope.get("path", "")))
