"""FakeStripe products and prices (specs/30 §4.1 Decision P03-T07b rule 2): ``POST /v1/products``,
``GET /v1/products/{id}``, ``POST /v1/prices``, ``GET /v1/prices/{id}``, ``GET /v1/prices``.

Price parameters are parsed into a :class:`PriceDraft` first and stored by :func:`store_price` only after a
whole request validated, so checkout ``line_items[n][price_data]`` (inline prices, ``active: false``) and
``POST /v1/prices`` share the rules and a rejected request leaves nothing behind.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from va_sim.stripe.billing_objects import price_json, product_json
from va_sim.stripe.charging import new_id
from va_sim.stripe.errors import invalid_request, missing_param, resource_missing
from va_sim.stripe.listing import LIST_PARAMS, paginate
from va_sim.stripe.params import (
    Params,
    boolean,
    check_known,
    currency,
    integer,
    mapping,
    new_metadata,
    string,
    string_list,
)
from va_sim.stripe.periods import INTERVALS, MAX_INTERVAL_COUNT

if TYPE_CHECKING:
    from va_sim.state import SimState
    from va_sim.stripe.http import StripeCall
    from va_sim.stripe.store import StripeStore

__all__ = [
    "PRICE_DATA_PARAMS",
    "PriceDraft",
    "create_price",
    "create_product",
    "list_prices",
    "parse_price",
    "product_name",
    "require_price",
    "retrieve_price",
    "retrieve_product",
    "store_price",
]

_PRODUCT_ID_LENGTH: Final = 14
_PRICE_ID_LENGTH: Final = 24
_PRODUCT_PARAMS: Final = frozenset({"id", "name", "description", "metadata", "active"})
_PRODUCT_DATA_PARAMS: Final = frozenset({"name", "description", "metadata"})
_RECURRING_PARAMS: Final = frozenset({"interval", "interval_count", "usage_type"})
PRICE_DATA_PARAMS: Final = frozenset({"currency", "unit_amount", "product", "product_data", "recurring"})
_PRICE_PARAMS: Final = PRICE_DATA_PARAMS | {
    "nickname",
    "lookup_key",
    "transfer_lookup_key",
    "metadata",
    "active",
}
_PRICE_LIST_PARAMS: Final = LIST_PARAMS | {"product", "active", "type", "lookup_keys"}
_PRICE_TYPES: Final = frozenset({"one_time", "recurring"})


def _join(prefix: str, name: str) -> str:
    return f"{prefix}[{name}]" if prefix else name


@dataclass(frozen=True, slots=True)
class PriceDraft:
    """A validated price not yet stored: ``product`` names an existing product, else ``product_data`` makes
    one."""

    currency: str
    unit_amount: int
    product: str | None
    product_data: Mapping[str, Any] | None
    recurring: Mapping[str, Any] | None


def require_price(store: StripeStore, price_id: str, *, param: str = "price") -> dict[str, Any]:
    price = store.prices.get(price_id)
    if price is None:
        raise resource_missing("price", price_id, param=param)
    return price


def product_name(store: StripeStore, price: Mapping[str, Any]) -> str:
    product = store.products.get(price["product"])
    return str(product["name"] if product is not None else price["product"])


def _recurring(values: Params, prefix: str) -> dict[str, Any] | None:
    name = _join(prefix, "recurring")
    hash_ = mapping(values, "recurring", prefix=prefix)
    if hash_ is None:
        return None
    check_known(hash_, _RECURRING_PARAMS, prefix=name)
    interval = string(hash_, "interval", required=True, prefix=name)
    if interval not in INTERVALS:
        msg = f"Invalid {name}[interval]: must be one of day, week, month, or year"
        raise invalid_request(msg, param=f"{name}[interval]")
    usage = string(hash_, "usage_type", prefix=name)
    if usage not in {None, "licensed"}:
        msg = f"Invalid {name}[usage_type]: the simulator supports only licensed prices"
        raise invalid_request(msg, param=f"{name}[usage_type]")
    count = integer(hash_, "interval_count", prefix=name)
    count = 1 if count is None else count
    if not 1 <= count <= MAX_INTERVAL_COUNT[interval]:
        msg = f"Invalid {name}[interval_count]: the maximum billing period is one year"
        raise invalid_request(msg, param=f"{name}[interval_count]")
    return {"interval": interval, "interval_count": count}


def _product_data(values: Params, prefix: str) -> dict[str, Any] | None:
    name = _join(prefix, "product_data")
    hash_ = mapping(values, "product_data", prefix=prefix)
    if hash_ is None:
        return None
    check_known(hash_, _PRODUCT_DATA_PARAMS, prefix=name)
    return {
        "name": string(hash_, "name", required=True, prefix=name),
        "description": string(hash_, "description", prefix=name),
        "metadata": new_metadata(hash_, prefix=name),
    }


def parse_price(store: StripeStore, values: Params, *, prefix: str = "") -> PriceDraft:
    """Validate the price fields of ``values`` (``prefix`` names them in errors)."""
    price_currency = currency(values, prefix=prefix)
    unit_amount = integer(values, "unit_amount", prefix=prefix)
    if unit_amount is None:
        raise missing_param(_join(prefix, "unit_amount"))
    if unit_amount < 0:
        msg = f"Invalid {_join(prefix, 'unit_amount')}: must be at least 0"
        raise invalid_request(msg, param=_join(prefix, "unit_amount"))
    product = string(values, "product", prefix=prefix)
    product_data = _product_data(values, prefix)
    if (product is None) == (product_data is None):
        msg = "You must specify exactly one of product or product_data."
        raise invalid_request(msg, param=_join(prefix, "product"))
    if product is not None and product not in store.products:
        raise resource_missing("product", product, param=_join(prefix, "product"))
    assert price_currency is not None  # noqa: S101 - required
    return PriceDraft(price_currency, unit_amount, product, product_data, _recurring(values, prefix))


def _new_product(
    state: SimState, fields: Mapping[str, Any], *, product_id: str | None = None
) -> dict[str, Any]:
    product_id = product_id or new_id(state, "prod_", _PRODUCT_ID_LENGTH)
    product = product_json(product_id, int(state.clock.now().timestamp()), fields)
    state.stripe.products[product_id] = product
    return product


def store_price(state: SimState, draft: PriceDraft, **fields: Any) -> dict[str, Any]:
    """Store ``draft`` (creating its product from ``product_data``); ``fields`` add nickname, lookup_key,
    metadata, active."""
    product = draft.product
    if product is None:
        assert draft.product_data is not None  # noqa: S101 - parse_price guarantees one of them
        product = _new_product(state, draft.product_data)["id"]
    price_id = new_id(state, "price_", _PRICE_ID_LENGTH)
    price = price_json(
        price_id,
        int(state.clock.now().timestamp()),
        {
            **fields,
            "product": product,
            "currency": draft.currency,
            "unit_amount": draft.unit_amount,
            "recurring": draft.recurring,
        },
    )
    state.stripe.prices[price_id] = price
    return price


async def create_product(call: StripeCall) -> dict[str, Any]:
    params = call.params
    check_known(params, _PRODUCT_PARAMS)
    name = string(params, "name", required=True)
    product_id = string(params, "id")
    if product_id is not None and product_id in call.store.products:
        msg = "Product already exists."
        raise invalid_request(msg, code="resource_already_exists", param="id")
    active = boolean(params, "active")
    fields = {
        "name": name,
        "description": string(params, "description"),
        "metadata": new_metadata(params),
        "active": True if active is None else active,
    }
    return copy.deepcopy(_new_product(call.state, fields, product_id=product_id))


async def retrieve_product(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, frozenset())
    product_id = call.path_params["product_id"]
    product = call.store.products.get(product_id)
    if product is None:
        raise resource_missing("product", product_id)
    return copy.deepcopy(product)


def _claim_lookup_key(call: StripeCall, lookup_key: str | None) -> None:
    if lookup_key is None:
        return
    holder = next((p for p in call.store.prices.values() if p["lookup_key"] == lookup_key), None)
    if holder is None:
        return
    if not boolean(call.params, "transfer_lookup_key"):
        msg = f"A price (`{holder['id']}`) already uses that lookup key."
        raise invalid_request(msg, param="lookup_key")
    holder["lookup_key"] = None


async def create_price(call: StripeCall) -> dict[str, Any]:
    params = call.params
    check_known(params, _PRICE_PARAMS)
    draft = parse_price(call.store, params)
    lookup_key = string(params, "lookup_key")
    active = boolean(params, "active")
    nickname = string(params, "nickname")
    price_metadata = new_metadata(params)
    _claim_lookup_key(call, lookup_key)
    price = store_price(
        call.state,
        draft,
        nickname=nickname,
        lookup_key=lookup_key,
        metadata=price_metadata,
        active=True if active is None else active,
    )
    return copy.deepcopy(price)


async def retrieve_price(call: StripeCall) -> dict[str, Any]:
    check_known(call.params, frozenset())
    return copy.deepcopy(require_price(call.store, call.path_params["price_id"], param="id"))


async def list_prices(call: StripeCall) -> dict[str, Any]:
    params = call.params
    check_known(params, _PRICE_LIST_PARAMS)
    product = string(params, "product")
    active = boolean(params, "active")
    kind = string(params, "type")
    if kind is not None and kind not in _PRICE_TYPES:
        msg = f"Invalid type: {kind} (one_time or recurring)"
        raise invalid_request(msg, param="type")
    lookup_keys = string_list(params, "lookup_keys")

    def wanted(price: Mapping[str, Any]) -> bool:
        return (
            (product is None or price["product"] == product)
            and (active is None or price["active"] == active)
            and (kind is None or price["type"] == kind)
            and (lookup_keys is None or price["lookup_key"] in lookup_keys)
        )

    prices = (price for price in reversed(call.store.prices.values()) if wanted(price))
    return paginate(prices, params, "/v1/prices")
