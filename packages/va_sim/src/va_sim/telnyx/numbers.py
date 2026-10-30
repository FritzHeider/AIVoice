"""FakeTelnyx numbers REST (specs/06-telephony-telnyx.md §2.1; specs/30-testing-and-evals.md §3.2).

Number search over the seeded inventory, number orders (``pending`` → ``success``/``failure`` one second later
on the sim clock, then ``number_order.complete``), and owned phone numbers (list/get/patch/messaging
patch/delete).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from va_sim.clock import iso_z
from va_sim.telnyx.errors import (
    NUMBER_NOT_AVAILABLE,
    TelnyxIssue,
    invalid,
    invalid_parameter,
    not_found,
    telnyx_error,
)
from va_sim.telnyx.http import authenticated, data, int_param, page, parse_body
from va_sim.telnyx.inventory import NUMBER_TYPES, NumberFilters, search, to_json
from va_sim.telnyx.models import NumberOrderCreate, PhoneNumberMessagingUpdate, PhoneNumberUpdate
from va_sim.telnyx.store import Account, Record

if TYPE_CHECKING:
    from va_sim.state import SimState

__all__ = ["MAX_SEARCH_LIMIT", "ORDER_COMPLETION_S", "number_routes"]

ORDER_COMPLETION_S: Final = 1.0
MAX_SEARCH_LIMIT: Final = 50
_DEFAULT_SEARCH_LIMIT: Final = 10
_FEATURES: Final = frozenset({"voice", "sms", "mms"})


def _search_filters(request: Request) -> NumberFilters | JSONResponse:
    query = request.query_params
    limit = int_param(request, "filter[limit]", default=_DEFAULT_SEARCH_LIMIT, maximum=MAX_SEARCH_LIMIT)
    if isinstance(limit, JSONResponse):
        return limit
    number_type = query.get("filter[phone_number_type]")
    if number_type is not None and number_type not in NUMBER_TYPES:
        allowed = ", ".join(sorted(NUMBER_TYPES))
        return invalid_parameter(
            f"filter[phone_number_type] must be one of {allowed}.", "filter[phone_number_type]"
        )
    features = tuple(query.getlist("filter[features][]"))
    unknown = sorted(set(features) - _FEATURES)
    if unknown:
        return invalid_parameter(f"Unknown feature {unknown[0]!r}.", "filter[features][]")
    return NumberFilters(
        country_code=query.get("filter[country_code]"),
        national_destination_code=query.get("filter[national_destination_code]"),
        locality=query.get("filter[locality]"),
        administrative_area=query.get("filter[administrative_area]"),
        contains=query.get("filter[phone_number][contains]"),
        features=features,
        phone_number_type=number_type,
        limit=limit,
    )


def number_routes(state: SimState) -> list[BaseRoute]:
    """Available numbers, number orders, and phone numbers."""
    store = state.telnyx

    async def available(request: Request, _account: Account) -> Response:
        filters = _search_filters(request)
        if isinstance(filters, JSONResponse):
            return filters
        found = search(store.inventory, filters, store.taken)
        return JSONResponse(
            {
                "data": [to_json(number) for number in found],
                "meta": {"total_results": len(found), "best_effort_results": 0},
            }
        )

    return [
        Route("/v2/available_phone_numbers", authenticated(store, available), methods=["GET"]),
        *_order_routes(state),
        *_phone_number_routes(state),
    ]


def _order_routes(state: SimState) -> list[BaseRoute]:
    store = state.telnyx

    def now() -> str:
        return iso_z(state.clock.now())

    def check_order(account: Account, body: NumberOrderCreate) -> JSONResponse | None:
        if body.connection_id is not None and body.connection_id not in account.apps:
            return invalid(f"Connection {body.connection_id!r} does not exist.", "/connection_id")
        if (
            body.messaging_profile_id is not None
            and body.messaging_profile_id not in account.messaging_profiles
        ):
            return invalid(
                f"Messaging profile {body.messaging_profile_id!r} does not exist.", "/messaging_profile_id"
            )
        seen: set[str] = set()
        for index, ordered in enumerate(body.phone_numbers):
            pointer = f"/phone_numbers/{index}/phone_number"
            if ordered.phone_number in seen:
                return invalid(f"{ordered.phone_number} is listed more than once.", pointer)
            if store.available(ordered.phone_number) is None:
                return telnyx_error(
                    422,
                    TelnyxIssue(
                        NUMBER_NOT_AVAILABLE,
                        f"{ordered.phone_number} is not available for purchase.",
                        pointer,
                    ),
                )
            seen.add(ordered.phone_number)
        return None

    async def create_order(request: Request, account: Account) -> Response:
        body = await parse_body(request, NumberOrderCreate)
        if isinstance(body, JSONResponse):
            return body
        problem = check_order(account, body)
        if problem is not None:
            return problem
        created = now()
        order: Record = {
            "id": state.ids.uuid(),
            "record_type": "number_order",
            "phone_numbers": [
                {
                    "id": state.ids.uuid(),
                    "record_type": "number_order_phone_number",
                    "phone_number": ordered.phone_number,
                    "status": "pending",
                    "regulatory_requirements": [],
                }
                for ordered in body.phone_numbers
            ],
            "phone_numbers_count": len(body.phone_numbers),
            "connection_id": body.connection_id,
            "messaging_profile_id": body.messaging_profile_id,
            "billing_group_id": None,
            "customer_reference": body.customer_reference,
            "status": "pending",
            "requirements_met": True,
            "sub_number_orders_ids": [],
            "created_at": created,
            "updated_at": created,
        }
        store.taken.update(entry["phone_number"] for entry in order["phone_numbers"])
        account.orders[order["id"]] = order
        state.tasks.spawn(complete_order(account, order), name=f"telnyx-number-order-{order['id']}")
        return data(order)

    def owned_number(order: Record, phone_number: str, at: str) -> Record:
        entry = store.inventory_entry(phone_number)
        return {
            "id": state.ids.digits(),
            "record_type": "phone_number",
            "phone_number": phone_number,
            "status": "active",
            "phone_number_type": entry.phone_number_type if entry is not None else "local",
            "connection_id": order["connection_id"],
            "messaging_profile_id": order["messaging_profile_id"],
            "customer_reference": order["customer_reference"],
            "external_pin": None,
            "tags": [],
            "billing_group_id": None,
            "emergency_enabled": False,
            "call_forwarding_enabled": True,
            "cnam_listing_enabled": False,
            "caller_id_name_enabled": False,
            "call_recording_enabled": False,
            "t38_fax_gateway_enabled": False,
            "purchased_at": at,
            "created_at": at,
            "updated_at": at,
        }

    async def complete_order(account: Account, order: Record) -> None:
        await state.clock.sleep(ORDER_COMPLETION_S)
        finished = now()
        succeeded = not store.config.fail_number_orders
        for entry in order["phone_numbers"]:
            entry["status"] = "success" if succeeded else "failure"
            if succeeded:
                number = owned_number(order, entry["phone_number"], finished)
                account.numbers[number["id"]] = number
            else:
                store.taken.discard(entry["phone_number"])
        order["status"] = "success" if succeeded else "failure"
        order["updated_at"] = finished
        app = account.apps.get(order["connection_id"] or "")
        url = app["webhook_event_url"] if app is not None else state.webhook_defaults.voice_url
        await state.webhooks.emit(url, "number_order.complete", order)

    async def get_order(request: Request, account: Account) -> Response:
        order_id = request.path_params["order_id"]
        order = account.orders.get(order_id)
        return data(order) if order is not None else not_found("number order", order_id)

    return [
        Route("/v2/number_orders", authenticated(store, create_order), methods=["POST"]),
        Route("/v2/number_orders/{order_id}", authenticated(store, get_order), methods=["GET"]),
    ]


def _phone_number_routes(state: SimState) -> list[BaseRoute]:
    store = state.telnyx

    def now() -> str:
        return iso_z(state.clock.now())

    async def list_numbers(request: Request, account: Account) -> Response:
        records = list(account.numbers.values())
        wanted = request.query_params.get("filter[phone_number]")
        if wanted is not None:
            records = [record for record in records if record["phone_number"] == wanted]
        tag = request.query_params.get("filter[tag]")
        if tag is not None:
            records = [record for record in records if tag in record["tags"]]
        return page(records, request)

    def lookup(request: Request, account: Account) -> Record | JSONResponse:
        number_id = request.path_params["number_id"]
        record = account.numbers.get(number_id)
        return record if record is not None else not_found("phone number", number_id)

    async def get_number(request: Request, account: Account) -> Response:
        record = lookup(request, account)
        return record if isinstance(record, JSONResponse) else data(record)

    async def update_number(request: Request, account: Account) -> Response:
        record = lookup(request, account)
        if isinstance(record, JSONResponse):
            return record
        body = await parse_body(request, PhoneNumberUpdate)
        if isinstance(body, JSONResponse):
            return body
        if body.connection_id is not None and body.connection_id not in account.apps:
            return invalid(f"Connection {body.connection_id!r} does not exist.", "/connection_id")
        changes = body.model_dump(exclude_unset=True)
        if "tags" in changes:
            changes["tags"] = changes["tags"] or []
        record.update(changes, updated_at=now())
        return data(record)

    async def update_messaging(request: Request, account: Account) -> Response:
        record = lookup(request, account)
        if isinstance(record, JSONResponse):
            return record
        body = await parse_body(request, PhoneNumberMessagingUpdate)
        if isinstance(body, JSONResponse):
            return body
        profile_id = body.messaging_profile_id
        if profile_id is not None and profile_id not in account.messaging_profiles:
            return invalid(f"Messaging profile {profile_id!r} does not exist.", "/messaging_profile_id")
        record.update(messaging_profile_id=profile_id, updated_at=now())
        return data(
            {
                "record_type": "messaging_phone_number",
                "id": record["id"],
                "phone_number": record["phone_number"],
                "messaging_profile_id": profile_id,
                "updated_at": record["updated_at"],
            }
        )

    async def delete_number(request: Request, account: Account) -> Response:
        record = lookup(request, account)
        if isinstance(record, JSONResponse):
            return record
        del account.numbers[record["id"]]
        store.taken.discard(record["phone_number"])
        return data({**record, "status": "deleted", "updated_at": now()})

    return [
        Route("/v2/phone_numbers", authenticated(store, list_numbers), methods=["GET"]),
        Route("/v2/phone_numbers/{number_id}", authenticated(store, get_number), methods=["GET"]),
        Route("/v2/phone_numbers/{number_id}", authenticated(store, update_number), methods=["PATCH"]),
        Route("/v2/phone_numbers/{number_id}", authenticated(store, delete_number), methods=["DELETE"]),
        Route(
            "/v2/phone_numbers/{number_id}/messaging",
            authenticated(store, update_messaging),
            methods=["PATCH"],
        ),
    ]
