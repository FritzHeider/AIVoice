"""Cross-tenant route suite (specs/04 §14 decision "Cross-tenant route suite (P02-T15)").

Routes are enumerated from the app's routers, so every route added by a later phase is probed without editing
this module; a route opts out only with ``x-public: <reason>``. The negative test mounts a router that ignores
the caller's org and asserts the suite catches it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, Final

import pytest
from cross_tenant import (
    CrossTenantProbe,
    CrossTenantRegistry,
    FactoryContext,
    RouteRequest,
    app_routes,
    make_tenant,
    path_params,
    public_marker_problems,
    tenant_routes,
)
from cross_tenant_factories import REGISTRY
from fastapi import APIRouter, Depends, Request
from fastapi.routing import APIRoute
from identity_db import IdentityDb
from pydantic import BaseModel
from session_helpers import SessionContainer, session_app, session_client, write_headers
from sqlalchemy import select

from va_api.auth import require_scope
from va_api.auth.session_auth import database_of
from va_api.envelopes import envelope_router
from va_api.main import create_app
from va_common.errors import AppError
from va_db.models import ApiKey

PATH_PARAM_OPERATIONS: Final = frozenset(
    {
        "org_members_update",
        "org_members_remove",
        "org_invitations_revoke",
        "orgs_get",
        "orgs_suspend",
        "orgs_reactivate",
        "api_keys_update",
        "api_keys_revoke",
        "api_keys_roll",
    }
)
PUBLIC_ROUTES: Final = frozenset(
    {
        "auth_signup",
        "auth_verify_email",
        "auth_resend_verification",
        "auth_login",
        "auth_logout",
        "auth_logout_all",
        "auth_session_get",
        "auth_agent_request_get",
        "auth_agent_request_approve",
        "auth_agent_request_deny",
        "auth_invitation_details",
        "auth_invitation_accept",
        "auth_mfa_setup",
        "auth_mfa_enable",
        "auth_mfa_verify",
        "auth_mfa_disable",
        "auth_password_forgot",
        "auth_password_reset",
        "auth_password_change",
        "auth_sso_start",
        "auth_sso_callback",
        "auth_switch_org",
        "health_get",
        "/healthz",
        "/readyz",
        "/metrics",
        "agent_onboarding_keys_create",
        "agent_onboarding_keys_get",
    }
)

# A router that forgets tenancy: it reads keys through the system session, ignoring the caller's org.
leaky_router = envelope_router(prefix="/v1/cross-tenant-leak", tags=["API Keys"])
_LEAKY: Final = [Depends(require_scope("api_keys:write"))]


@leaky_router.get("", operation_id="leak_list", dependencies=_LEAKY)
async def leak_list(request: Request) -> dict[str, list[str]]:
    async with database_of(request).system_session() as session:
        prefixes = (await session.scalars(select(ApiKey.prefix))).all()
    return {"prefixes": list(prefixes)}


@leaky_router.get("/{key_id}", operation_id="leak_get", dependencies=_LEAKY)
async def leak_get(key_id: uuid.UUID, request: Request) -> dict[str, str]:
    async with database_of(request).system_session() as session:
        key = await session.get(ApiKey, key_id)
    if key is None:
        raise AppError("NOT_FOUND", "API key not found.")
    return {"id": str(key.id), "org_id": str(key.org_id), "prefix": key.prefix}


@asynccontextmanager
async def probe_for(
    identity: IdentityDb, container: SessionContainer, extra_routers: tuple[APIRouter, ...] = ()
) -> AsyncIterator[tuple[CrossTenantProbe, list[APIRoute]]]:
    """Orgs A and B with keys and signed-in owners, a probe of B against A, and the app's routes."""
    async with AsyncExitStack() as stack:
        app = session_app(container, extra_routers=extra_routers)
        client = await stack.enter_async_context(session_client(app))
        tenants = []
        for _ in range(2):
            session = await stack.enter_async_context(
                session_client(session_app(container, extra_routers=extra_routers))
            )
            tenants.append(await make_tenant(identity, container, session))
        a, b = tenants
        yield (
            CrossTenantProbe(client=client, container=container, identity=identity, a=a, b=b),
            app_routes(app),
        )


async def test_every_tenant_route_is_isolated(identity: IdentityDb, container: SessionContainer) -> None:
    async with probe_for(identity, container) as (probe, routes):
        checked = {route.operation_id for route in tenant_routes(routes)}
        assert checked >= PATH_PARAM_OPERATIONS
        own = await probe.b.session.patch("/v1/org/settings", json={}, headers=write_headers(probe.b.session))
        assert own.status_code == 200, own.text
        assert await probe.problems(routes) == []
        assert len(probe.markers) >= 4 + len(REGISTRY.params())


async def test_an_unscoped_route_is_caught(identity: IdentityDb, container: SessionContainer) -> None:
    async with probe_for(identity, container, (leaky_router,)) as (probe, _):
        routes = [route for route in leaky_router.routes if isinstance(route, APIRoute)]
        problems = await probe.problems(routes)
    assert [problem[: problem.index(": ") + 5] for problem in problems] == [
        "list-leak api_key GET /v1/cross-tenant-leak: 200",
        "list-leak session GET /v1/cross-tenant-leak: 200",
        "foreign-id api_key GET /v1/cross-tenant-leak/{key_id}: 200",
        "foreign-id session GET /v1/cross-tenant-leak/{key_id}: 200",
    ]
    assert str(probe.a.org_id) in problems[2]


async def _endpoint() -> dict[str, str]:
    return {}


class _NoExample(BaseModel):
    name: str


class _WithExample(BaseModel):
    model_config = {"json_schema_extra": {"examples": [{"name": "Front desk"}]}}

    name: str


async def _with_body(body: _NoExample) -> dict[str, str]:
    return {"name": body.name}


async def _with_optional_body(body: _NoExample | None = None) -> dict[str, str]:
    return {}


async def _with_example(body: _WithExample) -> dict[str, str]:
    return {"name": body.name}


async def _with_query(status: str) -> dict[str, str]:
    return {"status": status}


def _route(
    method: str, path: str, endpoint: Any = _endpoint, extra: Mapping[str, object] | None = None
) -> APIRoute:
    router = APIRouter()
    router.add_api_route(path, endpoint, methods=[method], openapi_extra=dict(extra) if extra else None)
    route = router.routes[0]
    assert isinstance(route, APIRoute)
    return route


async def _factory(context: FactoryContext) -> str:
    return str(context.tenant.org_id)


async def _other_factory(context: FactoryContext) -> str:
    return str(context.tenant.owner.user.id)


def test_a_missing_factory_is_reported() -> None:
    registry = CrossTenantRegistry()
    registry.register_resource_factory("agent_id", _factory)
    route = _route("GET", "/v1/agents/{agent_id}/versions/{version_id}")
    assert path_params(route.path) == ["agent_id", "version_id"]
    assert (
        registry.resources_for(route)
        == "no resource factory for {version_id} (GET /v1/agents/{agent_id}/versions/{version_id})"
    )
    registry.register_resource_factory("version_id", _other_factory)
    assert registry.resources_for(route) == [("agent_id", _factory), ("version_id", _other_factory)]


def test_a_body_without_example_is_reported() -> None:
    registry = CrossTenantRegistry()
    route = _route("POST", "/v1/tools", _with_body)
    assert (
        registry.request_for(route)
        == "no request body for _with_body: add a model example or register_request"
    )
    assert registry.request_for(_route("POST", "/v1/tools", _with_optional_body)) == RouteRequest()
    assert registry.request_for(_route("POST", "/v1/tools", _with_example)) == RouteRequest(
        json={"name": "Front desk"}
    )
    assert registry.request_for(_route("DELETE", "/v1/tools")) == RouteRequest()


def test_a_required_query_without_registration_is_reported() -> None:
    registry = CrossTenantRegistry()
    route = _route("GET", "/v1/calls", _with_query)
    route.operation_id = "calls_list"
    assert (
        registry.request_for(route)
        == "required query parameter status without a registered request for calls_list"
    )
    registry.register_request("calls_list", params={"status": "completed"})
    assert registry.request_for(route) == RouteRequest(params={"status": "completed"})


def test_a_registered_request_wins_over_the_example() -> None:
    registry = CrossTenantRegistry()
    route = _route("POST", "/v1/tools", _with_example)
    route.operation_id = "tools_create"
    registry.register_request("tools_create", json={"name": "Billing line"})
    assert registry.request_for(route) == RouteRequest(json={"name": "Billing line"})
    with pytest.raises(ValueError, match="tools_create"):
        registry.register_request("tools_create", json={})


def test_the_longest_prefix_wins_and_duplicates_raise() -> None:
    registry = CrossTenantRegistry()
    registry.register_resource_factory("id", _factory)
    registry.register_resource_factory("id", _other_factory, path_prefix="/v1/tools")
    assert registry.factory_for("/v1/tools/{id}", "id") is _other_factory
    assert registry.factory_for("/v1/agents/{id}", "id") is _factory
    assert registry.factory_for("/v1/agents/{id}", "agent_id") is None
    assert registry.params() == frozenset({"id"})
    with pytest.raises(ValueError, match="already registered"):
        registry.register_resource_factory("id", _factory, path_prefix="/v1/tools")


def test_the_owner_control_status() -> None:
    registry = CrossTenantRegistry()
    route = _route("POST", "/v1/tools/{id}/archive")
    route.operation_id = "tools_archive"
    assert registry.owner_status_ok(route, 200)
    assert registry.owner_status_ok(route, 204)
    assert not registry.owner_status_ok(route, 409)
    registry.register_owner_status("tools_archive", 409)
    assert registry.owner_status_ok(route, 409)
    assert not registry.owner_status_ok(route, 200)


def test_public_markers() -> None:
    routes = app_routes(create_app())
    assert public_marker_problems(routes) == []
    public = {route.operation_id or route.path for route in routes if route not in tenant_routes(routes)}
    assert public == PUBLIC_ROUTES
    assert public_marker_problems(
        [
            _route("GET", "/v1/widget/config", extra={"x-public": "  "}),
            _route("POST", "/v1/widget/session", extra={"x-scope": "public"}),
            _route("POST", "/v1/billing/webhook", extra={"x-public": "stripe-signature"}),
        ]
    ) == [
        "GET /v1/widget/config: x-public needs a reason",
        "POST /v1/widget/session: x-scope public without x-public",
    ]


def test_the_phase_02_factories_are_registered() -> None:
    assert {"user_id", "invitation_id", "key_id", "org_id"} <= REGISTRY.params()
