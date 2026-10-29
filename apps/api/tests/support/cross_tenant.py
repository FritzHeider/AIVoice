"""Cross-tenant route suite support (specs/04-auth-tenancy-rbac.md §14 decision, P02-T15).

Every non-public route of the app is probed automatically: a route opts out only with
``openapi_extra["x-public"] = "<reason>"``. For each tenant route a second org ("B") calls it as an API key
and as a dashboard session:

* **foreign id** (routes with path parameters): fresh resources of org A fill the path; B must get
  ``NOT_FOUND`` or ``FORBIDDEN`` without any A data in the body, and A's own key must then succeed with the
  same ids (the owner control, proving the ids were real);
* **foreign org header** (every tenant route): ``X-VA-Org-Id: <A's org id>`` must be refused with
  ``FORBIDDEN`` "Organization is not a sub-account of the caller.";
* **list leak** (GET routes without path parameters): B's response must not contain any A marker (org,
  owner, key prefix, and the ids of a world of A resources built before the checks).

Path parameters are filled by resource factories registered per parameter name (and optionally a path prefix,
the longest matching prefix wins); request bodies come from the body model's first JSON-schema example unless
a request is registered for the operation. A route nothing can be derived for is reported as a problem, so a
later router that forgets its registration fails the suite.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import httpx
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from identity_db import IdentityDb
from pydantic import TypeAdapter
from session_helpers import Member, SessionContainer, login, make_member, store_api_key, write_headers

from va_api.auth.csrf import CSRF_INVALID, ORIGIN_NOT_ALLOWED
from va_api.auth.sub_accounts import NOT_A_CHILD_MESSAGE

__all__ = [
    "PRINCIPALS",
    "PUBLIC_KEY",
    "REGISTRY",
    "CrossTenantProbe",
    "CrossTenantRegistry",
    "FactoryContext",
    "Principal",
    "ResourceFactory",
    "RouteRequest",
    "Tenant",
    "app_routes",
    "is_public",
    "make_tenant",
    "path_params",
    "public_marker_problems",
    "reset_rate_limits",
    "tenant_routes",
]

PUBLIC_KEY: Final = "x-public"
ORG_HEADER: Final = "X-VA-Org-Id"
READ_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
REFUSED: Final = frozenset({(403, "FORBIDDEN"), (404, "NOT_FOUND")})
CSRF_MESSAGES: Final = frozenset({CSRF_INVALID, ORIGIN_NOT_ALLOWED})
EXCERPT: Final = 200
_PARAM = re.compile(r"{(\w+)(?::[^}]*)?}")

Principal = Literal["api_key", "session"]
PRINCIPALS: Final[tuple[Principal, ...]] = ("api_key", "session")


@dataclass(frozen=True, slots=True)
class Tenant:
    """An org with its owner, a ``*`` API key (and its prefix), and a client signed in as the owner."""

    org_id: uuid.UUID
    owner: Member
    api_key: str
    key_prefix: str
    session: httpx.AsyncClient


@dataclass(frozen=True, slots=True)
class FactoryContext:
    """What a resource factory gets: an unauthenticated client of the app (send ``tenant.api_key`` yourself),
    the container and database, the tenant to create the resource in, and the ids of the path parameters to
    the left of the one being created."""

    client: httpx.AsyncClient
    container: SessionContainer
    identity: IdentityDb
    tenant: Tenant
    params: Mapping[str, str]


ResourceFactory = Callable[[FactoryContext], Awaitable[str]]


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """The JSON body (``None`` for none) and query parameters sent to a route."""

    json: Mapping[str, Any] | None = None
    params: Mapping[str, str] = field(default_factory=dict)


def path_params(path: str) -> list[str]:
    """The path parameter names of ``path``, left to right."""
    return _PARAM.findall(path)


def _fill(path: str, ids: Mapping[str, str]) -> str:
    return _PARAM.sub(lambda match: ids[match.group(1)], path)


def _method(route: APIRoute) -> str:
    return sorted((route.methods or {"GET"}) - {"HEAD"})[0]


def _query_params(dependant: Dependant) -> list[Any]:
    """The query parameters of ``dependant`` and of every dependency below it."""
    return [*dependant.query_params, *(p for sub in dependant.dependencies for p in _query_params(sub))]


def _name(route: APIRoute) -> str:
    return route.operation_id or route.name


class CrossTenantRegistry:
    """Resource factories, registered requests, and owner-control statuses of the suite."""

    def __init__(self) -> None:
        self._factories: dict[tuple[str, str], ResourceFactory] = {}
        self._requests: dict[str, RouteRequest] = {}
        self._owner_status: dict[str, int] = {}

    def register_resource_factory(
        self, path_param: str, factory: ResourceFactory, *, path_prefix: str = "/"
    ) -> None:
        """Create resources for ``{path_param}`` in paths starting with ``path_prefix``."""
        key = (path_param, path_prefix)
        if key in self._factories:
            msg = f"a factory for {{{path_param}}} under {path_prefix!r} is already registered"
            raise ValueError(msg)
        self._factories[key] = factory

    def factory_for(self, path: str, param: str) -> ResourceFactory | None:
        """The factory of ``param`` with the longest prefix of ``path`` (``None`` when none matches)."""
        matches = [
            (prefix, factory)
            for (name, prefix), factory in self._factories.items()
            if name == param and path.startswith(prefix)
        ]
        if not matches:
            return None
        return max(matches, key=lambda match: len(match[0]))[1]

    def params(self) -> frozenset[str]:
        """Every path parameter name with a registered factory."""
        return frozenset(name for name, _ in self._factories)

    def register_request(
        self,
        operation_id: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> None:
        """Send ``json`` and ``params`` to ``operation_id`` instead of the derived request."""
        if operation_id in self._requests:
            msg = f"a request for {operation_id} is already registered"
            raise ValueError(msg)
        self._requests[operation_id] = RouteRequest(json=json, params=dict(params or {}))

    def register_owner_status(self, operation_id: str, status: int) -> None:
        """The status the owner control of ``operation_id`` must return (instead of any 2xx)."""
        self._owner_status[operation_id] = status

    def owner_status_ok(self, route: APIRoute, status: int) -> bool:
        expected = self._owner_status.get(_name(route))
        return 200 <= status < 300 if expected is None else status == expected

    def request_for(self, route: APIRoute) -> RouteRequest | str:
        """The request sent to ``route``, or a problem when it cannot be derived."""
        name = _name(route)
        registered = self._requests.get(name)
        if registered is not None:
            return registered
        required = [param.alias for param in _query_params(route.dependant) if param.field_info.is_required()]
        if required:
            return f"required query parameter {', '.join(required)} without a registered request for {name}"
        body = route.body_field
        if body is None:
            return RouteRequest()
        annotation = body.field_info.annotation or dict[str, Any]
        examples = TypeAdapter(annotation).json_schema().get("examples")
        if examples:
            return RouteRequest(json=examples[0])
        if not body.field_info.is_required():
            return RouteRequest()
        return f"no request body for {name}: add a model example or register_request"

    def resources_for(self, route: APIRoute) -> list[tuple[str, ResourceFactory]] | str:
        """The factory of each path parameter of ``route`` in order, or a problem naming the missing one."""
        chain: list[tuple[str, ResourceFactory]] = []
        for param in path_params(route.path):
            factory = self.factory_for(route.path, param)
            if factory is None:
                return f"no resource factory for {{{param}}} ({_method(route)} {route.path})"
            chain.append((param, factory))
        return chain


REGISTRY: Final = CrossTenantRegistry()


def app_routes(app: FastAPI) -> list[APIRoute]:
    """Every API route of every router the app was built with (``app.state.routers``)."""
    return [route for router in app.state.routers for route in router.routes if isinstance(route, APIRoute)]


def is_public(route: APIRoute) -> bool:
    return PUBLIC_KEY in (route.openapi_extra or {})


def public_marker_problems(routes: Iterable[APIRoute]) -> list[str]:
    """Blank ``x-public`` reasons and ``x-scope: public`` routes that do not opt out with ``x-public``."""
    problems: list[str] = []
    for route in routes:
        extra = route.openapi_extra or {}
        name = f"{_method(route)} {route.path}"
        reason = extra.get(PUBLIC_KEY)
        if PUBLIC_KEY in extra and (not isinstance(reason, str) or not reason.strip()):
            problems.append(f"{name}: {PUBLIC_KEY} needs a reason")
        if extra.get("x-scope") == "public" and PUBLIC_KEY not in extra:
            problems.append(f"{name}: x-scope public without {PUBLIC_KEY}")
    return problems


def tenant_routes(routes: Iterable[APIRoute]) -> list[APIRoute]:
    """Every route that does not opt out with ``x-public``."""
    return [route for route in routes if not is_public(route)]


async def reset_rate_limits(container: SessionContainer) -> None:
    """Delete every ``rl:*`` bucket, so the suite's many calls per principal are never ``RATE_LIMITED``."""
    keys = [key async for key in container.redis.scan_iter(match="rl:*")]
    if keys:
        await container.redis.delete(*keys)


async def make_tenant(
    identity: IdentityDb, container: SessionContainer, session: httpx.AsyncClient
) -> Tenant:
    """A new org with an owner signed in on ``session`` and a ``*`` API key."""
    owner = await make_member(identity, container.password_hasher)
    api_key, row = await store_api_key(container, owner.org.id)
    await login(session, owner.user.email)
    return Tenant(org_id=owner.org.id, owner=owner, api_key=api_key, key_prefix=row.prefix, session=session)


def _error(response: httpx.Response) -> tuple[str | None, str | None]:
    try:
        item = response.json()["errors"][0]
    except (ValueError, KeyError, IndexError, TypeError):
        return None, None
    return item.get("error"), item.get("message")


@dataclass
class CrossTenantProbe:
    """Runs the foreign-id, foreign-org-header, and list-leak checks of org ``b`` against org ``a``."""

    client: httpx.AsyncClient
    container: SessionContainer
    identity: IdentityDb
    a: Tenant
    b: Tenant
    registry: CrossTenantRegistry = REGISTRY
    markers: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        owner = self.a.owner.user
        self.markers |= {str(self.a.org_id), str(owner.id), owner.email, self.a.key_prefix}

    async def problems(self, routes: Iterable[APIRoute]) -> list[str]:
        """One message per failed check; empty when ``b`` can neither reach nor see anything of ``a``."""
        checked = tenant_routes(routes)
        await self._build_world(checked)
        problems: list[str] = []
        for route in checked:
            problems += await self._check(route)
        return problems

    async def _build_world(self, routes: list[APIRoute]) -> None:
        """One chain of A resources per distinct factory chain; their ids become markers."""
        built: set[tuple[tuple[str, ResourceFactory], ...]] = set()
        for route in routes:
            chain = self.registry.resources_for(route)
            if isinstance(chain, str) or not chain or tuple(chain) in built:
                continue
            built.add(tuple(chain))
            await self._create(chain)

    async def _create(self, chain: list[tuple[str, ResourceFactory]]) -> dict[str, str]:
        ids: dict[str, str] = {}
        for param, factory in chain:
            await reset_rate_limits(self.container)
            context = FactoryContext(
                client=self.client,
                container=self.container,
                identity=self.identity,
                tenant=self.a,
                params=ids,
            )
            ids[param] = await factory(context)
            self.markers.add(ids[param])
        return ids

    async def _check(self, route: APIRoute) -> list[str]:
        request = self.registry.request_for(route)
        if isinstance(request, str):
            return [request]
        chain = self.registry.resources_for(route)
        if isinstance(chain, str):
            return [chain]
        if chain:
            return await self._check_foreign_ids(route, request, await self._create(chain))
        problems = await self._check_header(route, route.path, request)
        if _method(route) == "GET":
            for principal in PRINCIPALS:
                response = await self._call(
                    self.b, principal=principal, route=route, path=route.path, request=request
                )
                if self._leaks(response, ()):
                    problems.append(self._problem("list-leak", principal, route, response))
        return problems

    async def _check_foreign_ids(
        self, route: APIRoute, request: RouteRequest, ids: Mapping[str, str]
    ) -> list[str]:
        path = _fill(route.path, ids)
        problems: list[str] = []
        for principal in PRINCIPALS:
            response = await self._call(self.b, principal=principal, route=route, path=path, request=request)
            code, message = _error(response)
            refused = (response.status_code, code) in REFUSED and message not in CSRF_MESSAGES
            if not refused or self._leaks(response, ids.values()):
                problems.append(self._problem("foreign-id", principal, route, response))
        problems += await self._check_header(route, path, request)
        control = await self._call(self.a, principal="api_key", route=route, path=path, request=request)
        if not self.registry.owner_status_ok(route, control.status_code):
            problems.append(self._problem("owner-control", "api_key", route, control))
        return problems

    async def _check_header(self, route: APIRoute, path: str, request: RouteRequest) -> list[str]:
        problems: list[str] = []
        for principal in PRINCIPALS:
            response = await self._call(
                self.b,
                principal=principal,
                route=route,
                path=path,
                request=request,
                headers={ORG_HEADER: str(self.a.org_id)},
            )
            if (response.status_code, *_error(response)) != (403, "FORBIDDEN", NOT_A_CHILD_MESSAGE):
                problems.append(self._problem("foreign-org-header", principal, route, response))
        return problems

    def _leaks(self, response: httpx.Response, requested: Iterable[str]) -> bool:
        """Whether the body holds an A marker other than the ids the request itself named."""
        return any(marker in response.text for marker in self.markers - set(requested))

    async def _call(  # noqa: PLR0913 - one request of one principal
        self,
        tenant: Tenant,
        *,
        principal: Principal,
        route: APIRoute,
        path: str,
        request: RouteRequest,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        await reset_rate_limits(self.container)
        method = _method(route)
        if principal == "api_key":
            client, sent = self.client, {"Authorization": tenant.api_key}
        else:
            client = tenant.session
            sent = {} if method in READ_METHODS else write_headers(client)
        return await client.request(
            method, path, json=request.json, params=dict(request.params), headers={**sent, **(headers or {})}
        )

    @staticmethod
    def _problem(check: str, principal: Principal, route: APIRoute, response: httpx.Response) -> str:
        excerpt = response.text[:EXCERPT]
        return f"{check} {principal} {_method(route)} {route.path}: {response.status_code} {excerpt}"
