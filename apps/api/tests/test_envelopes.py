"""Envelope families (specs/31-error-catalog.md §1) and the route classes that stamp ``x-envelope``."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.types import Scope

from va_api.envelopes import (
    FAMILIES,
    Envelope,
    EnvelopedRoute,
    LegacyRoute,
    classify_path,
    envelope_router,
    legacy_router,
    route_envelope,
)
from va_api.main import create_app

SPEC = Path(__file__).resolve().parents[3] / "specs" / "31-error-catalog.md"


def spec_families() -> list[tuple[str, Envelope]]:
    rows: list[tuple[str, Envelope]] = []
    for line in SPEC.read_text(encoding="utf-8").splitlines():
        for label, envelope in (("**Legacy**", Envelope.LEGACY), ("**Envelope**", Envelope.ENVELOPE)):
            if line.startswith(f"| {label}"):
                family_cell = line.split("|")[1]
                rows.extend((pattern, envelope) for pattern in re.findall(r"`(/[^`]+)`", family_cell))
    return rows


def sample_paths(pattern: str) -> list[str]:
    if pattern.endswith("/*"):
        base = pattern[:-2]
        return [base, f"{base}/x", f"{base}/x/y"]
    if pattern.endswith("*"):
        base = pattern[:-1]
        return [base, f"{base}/x"]
    return [pattern]


def test_families_mirror_spec_table() -> None:
    assert list(FAMILIES) == spec_families()
    assert len(FAMILIES) == 31


@pytest.mark.parametrize(("pattern", "envelope"), spec_families())
def test_classify_every_spec_family(pattern: str, envelope: Envelope) -> None:
    for path in sample_paths(pattern):
        assert classify_path(path) is envelope, path


@pytest.mark.parametrize(
    ("path", "envelope"),
    [
        ("/v1/orgs/1", Envelope.LEGACY),
        ("/v1/orgs", Envelope.LEGACY),
        ("/v1/org", Envelope.ENVELOPE),
        ("/v1/org/members", Envelope.ENVELOPE),
        ("/v1/me", Envelope.ENVELOPE),
        ("/v1/memory/x", Envelope.ENVELOPE),
        ("/v1/meetings", Envelope.LEGACY),
        ("/v1/pathways/abc", Envelope.LEGACY),
        ("/numbersx", Envelope.LEGACY),
        ("/internalx", Envelope.LEGACY),
        ("/", Envelope.LEGACY),
    ],
)
def test_longest_match_wins(path: str, envelope: Envelope) -> None:
    assert classify_path(path) is envelope


async def endpoint() -> dict[str, str]:
    return {}


def test_route_class_sets_x_envelope() -> None:
    legacy = legacy_router()
    legacy.add_api_route("/v1/calls", endpoint, openapi_extra={"x-scope": "calls:write"})
    enveloped = envelope_router(prefix="/v1/knowledge")
    enveloped.add_api_route("/x", endpoint)
    first, second = legacy.routes[0], enveloped.routes[0]
    assert isinstance(first, LegacyRoute)
    assert first.openapi_extra == {"x-scope": "calls:write", "x-envelope": "legacy"}
    assert isinstance(second, EnvelopedRoute)
    assert second.openapi_extra == {"x-envelope": "envelope"}
    assert second.path_format == "/v1/knowledge/x"


def test_matching_declared_envelope_accepted() -> None:
    router = legacy_router()
    router.add_api_route("/v1/calls", endpoint, openapi_extra={"x-envelope": "legacy"})
    route = router.routes[0]
    assert isinstance(route, APIRoute)
    assert route.openapi_extra == {"x-envelope": "legacy"}


def test_conflicting_envelope_raises() -> None:
    router = envelope_router()
    with pytest.raises(ValueError, match="x-envelope='legacy' on a envelope router"):
        router.add_api_route("/v1/knowledge", endpoint, openapi_extra={"x-envelope": "legacy"})


def test_route_envelope_prefers_matched_route() -> None:
    router = envelope_router()
    router.add_api_route("/v1/calls/odd", endpoint)
    scope: Scope = {"type": "http", "path": "/v1/calls/odd", "route": router.routes[0]}
    assert route_envelope(scope) is Envelope.ENVELOPE
    assert route_envelope({"type": "http", "path": "/v1/calls/odd"}) is Envelope.LEGACY


def test_route_envelope_plain_route_falls_back_to_path() -> None:
    plain = APIRoute("/v1/knowledge/plain", endpoint)
    scope: Scope = {"type": "http", "path": "/v1/knowledge/plain", "route": plain}
    assert route_envelope(scope) is Envelope.ENVELOPE


def api_routes(app: FastAPI) -> list[APIRoute]:
    return [route for router in app.state.routers for route in router.routes if isinstance(route, APIRoute)]


def test_app_routes_match_family() -> None:
    app = create_app(init_observability=False)
    public = [r for r in api_routes(app) if r.path_format.startswith(("/v1", "/v2", "/numbers", "/internal"))]
    assert public
    for route in public:
        assert (route.openapi_extra or {}).get("x-envelope") == classify_path(route.path_format).value
