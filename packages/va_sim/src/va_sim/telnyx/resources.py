"""FakeTelnyx provisioning REST (specs/06-telephony-telnyx.md §2.1; specs/30-testing-and-evals.md §3.2).

``GET /v2/balance`` plus call control applications, outbound voice profiles, and messaging profiles, all
served by one generic :class:`Collection` (create, list with filters and pagination, get, PATCH with nested
merge). A PATCH merges the body into the stored request fields and re-validates the result with the create
model, so unknown fields and invalid values fail exactly as they do on create.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.clock import iso_z
from va_sim.ids import SimIds
from va_sim.telnyx.errors import invalid, not_found
from va_sim.telnyx.http import authenticated, data, page, parse_body, parse_object, validate
from va_sim.telnyx.models import CallControlAppCreate, MessagingProfileCreate, OutboundVoiceProfileCreate
from va_sim.telnyx.store import Account, Record

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = [
    "APPS",
    "MESSAGING_PROFILES",
    "VOICE_PROFILES",
    "Collection",
    "ListFilter",
    "collection_routes",
    "deep_merge",
    "resource_routes",
]


@dataclass(frozen=True, slots=True)
class ListFilter:
    """A list query filter: exact match, or case-insensitive substring when ``contains``."""

    parameter: str
    field: str
    contains: bool

    def keep(self, record: Record, wanted: str) -> bool:
        value = str(record.get(self.field) or "")
        return wanted.casefold() in value.casefold() if self.contains else value == wanted


def _no_check(_account: Account, _body: Any) -> JSONResponse | None:
    return None


def _as_is(_account: Account, record: Record) -> Record:
    return record


@dataclass(frozen=True, slots=True)
class Collection[M: BaseModel]:
    """One Telnyx resource family stored per account."""

    path: str
    kind: str
    record_type: str
    model: type[M]
    records: Callable[[Account], dict[str, Record]]
    new_id: Callable[[SimIds], str]
    created_status: int = 201
    filters: tuple[ListFilter, ...] = ()
    fixed: Mapping[str, object] = field(default_factory=dict)
    check: Callable[[Account, M], JSONResponse | None] = _no_check
    render: Callable[[Account, Record], Record] = _as_is


def deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    """``base`` updated with ``patch``; nested objects merge key-wise instead of being replaced."""
    merged = dict(base)
    for key, value in patch.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def collection_routes[M: BaseModel](collection: Collection[M], state: SimState) -> list[BaseRoute]:
    """POST/GET ``/v2/{path}`` and GET/PATCH ``/v2/{path}/{id}`` for ``collection``."""
    store = state.telnyx

    async def create(request: Request, account: Account) -> Response:
        body = await parse_body(request, collection.model)
        if isinstance(body, JSONResponse):
            return body
        problem = collection.check(account, body)
        if problem is not None:
            return problem
        now = iso_z(state.clock.now())
        record: Record = {
            "id": collection.new_id(state.ids),
            "record_type": collection.record_type,
            **collection.fixed,
            **body.model_dump(mode="json"),
            "created_at": now,
            "updated_at": now,
        }
        collection.records(account)[record["id"]] = record
        return data(collection.render(account, record), collection.created_status)

    async def listing(request: Request, account: Account) -> Response:
        records = list(collection.records(account).values())
        for list_filter in collection.filters:
            wanted = request.query_params.get(list_filter.parameter)
            if wanted is not None:
                records = [record for record in records if list_filter.keep(record, wanted)]
        return page([collection.render(account, record) for record in records], request)

    def lookup(request: Request, account: Account) -> Record | JSONResponse:
        resource_id = request.path_params["resource_id"]
        record = collection.records(account).get(resource_id)
        return record if record is not None else not_found(collection.kind, resource_id)

    async def detail(request: Request, account: Account) -> Response:
        record = lookup(request, account)
        if isinstance(record, JSONResponse):
            return record
        return data(collection.render(account, record))

    async def update(request: Request, account: Account) -> Response:
        record = lookup(request, account)
        if isinstance(record, JSONResponse):
            return record
        patch = await parse_object(request)
        if isinstance(patch, JSONResponse):
            return patch
        stored = {name: record[name] for name in collection.model.model_fields}
        body = validate(collection.model, deep_merge(stored, patch))
        if isinstance(body, JSONResponse):
            return body
        problem = collection.check(account, body)
        if problem is not None:
            return problem
        record.update(body.model_dump(mode="json"), updated_at=iso_z(state.clock.now()))
        return data(collection.render(account, record))

    base = f"/v2/{collection.path}"
    return [
        Route(base, authenticated(store, create), methods=["POST"]),
        Route(base, authenticated(store, listing), methods=["GET"]),
        Route(f"{base}/{{resource_id}}", authenticated(store, detail), methods=["GET"]),
        Route(f"{base}/{{resource_id}}", authenticated(store, update), methods=["PATCH"]),
    ]


def _check_app(account: Account, body: CallControlAppCreate) -> JSONResponse | None:
    profile_id = body.outbound.outbound_voice_profile_id
    if profile_id is not None and profile_id not in account.voice_profiles:
        return invalid(
            f"Outbound voice profile {profile_id!r} does not exist.", "/outbound/outbound_voice_profile_id"
        )
    return None


def _render_voice_profile(account: Account, record: Record) -> Record:
    connections = sum(
        1 for app in account.apps.values() if app["outbound"]["outbound_voice_profile_id"] == record["id"]
    )
    return {**record, "connections_count": connections}


def _numeric_id(ids: SimIds) -> str:
    return ids.digits()


def _uuid(ids: SimIds) -> str:
    return ids.uuid()


APPS: Collection[CallControlAppCreate] = Collection(
    path="call_control_applications",
    kind="call control application",
    record_type="call_control_application",
    model=CallControlAppCreate,
    records=lambda account: account.apps,
    new_id=_numeric_id,
    filters=(ListFilter("filter[application_name][contains]", "application_name", contains=True),),
    check=_check_app,
)

VOICE_PROFILES: Collection[OutboundVoiceProfileCreate] = Collection(
    path="outbound_voice_profiles",
    kind="outbound voice profile",
    record_type="outbound_voice_profile",
    model=OutboundVoiceProfileCreate,
    records=lambda account: account.voice_profiles,
    new_id=_numeric_id,
    filters=(ListFilter("filter[name][contains]", "name", contains=True),),
    fixed={
        "usage_payment_method": "rate-deck",
        "daily_spend_limit": "0.00",
        "daily_spend_limit_enabled": False,
        "max_destination_rate": None,
        "billing_group_id": None,
    },
    render=_render_voice_profile,
)

MESSAGING_PROFILES: Collection[MessagingProfileCreate] = Collection(
    path="messaging_profiles",
    kind="messaging profile",
    record_type="messaging_profile",
    model=MessagingProfileCreate,
    records=lambda account: account.messaging_profiles,
    new_id=_uuid,
    created_status=200,
    filters=(ListFilter("filter[name]", "name", contains=False),),
    fixed={"number_pool_settings": None, "url_shortener_settings": None, "alpha_sender": None},
)


def resource_routes(state: SimState) -> list[BaseRoute]:
    """``GET /v2/balance`` and the three provisioning collections."""

    async def balance(_request: Request, _account: Account) -> Response:
        return data(
            {
                "record_type": "balance",
                "balance": "100.00",
                "credit_limit": "0.00",
                "available_credit": "100.00",
                "pending": "0.00",
                "currency": "USD",
            }
        )

    return [
        Route("/v2/balance", authenticated(state.telnyx, balance), methods=["GET"]),
        *collection_routes(APPS, state),
        *collection_routes(VOICE_PROFILES, state),
        *collection_routes(MESSAGING_PROFILES, state),
    ]
