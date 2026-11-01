"""Telnyx number commands (specs/06-telephony-telnyx.md §2.1 number rows, §2.2).

``create_number_order`` is not idempotent (retried only before bytes were sent); the activity layer
reconciles a lost answer by ``customer_reference`` (the org id) and the ``va``/``org:{org_id}`` tags of
:func:`number_tags`.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Final

from va_telnyx.number_models import (
    AvailableNumber,
    NumberOrder,
    NumberSearch,
    PhoneNumber,
    PhoneNumberMessaging,
)
from va_telnyx.transport import Operation, TelnyxTransport, parse_data, parse_items, segment

__all__ = ["NumberCommands", "number_tags"]

_SEARCH: Final = Operation("search_numbers", "GET", "number_search", idempotent=True)
_ORDER: Final = Operation("create_number_order", "POST", "number_order", idempotent=False)
_GET_ORDER: Final = Operation("get_number_order", "GET", None, idempotent=True)
_LOOKUP: Final = Operation("get_phone_number_by_e164", "GET", None, idempotent=True)
_UPDATE: Final = Operation("update_phone_number", "PATCH", None, idempotent=True)
_UPDATE_MESSAGING: Final = Operation("update_phone_number_messaging", "PATCH", None, idempotent=True)
_DELETE: Final = Operation("delete_phone_number", "DELETE", None, idempotent=True)


def number_tags(org_id: uuid.UUID) -> list[str]:
    """The tags of a number the platform owns for ``org_id`` (specs/06 §2.1 ``update_phone_number``)."""
    return ["va", f"org:{org_id}"]


class NumberCommands:
    """Number search, orders, and owned phone numbers."""

    _transport: TelnyxTransport

    async def search_numbers(self, search: NumberSearch) -> list[AvailableNumber]:
        envelope = await self._transport.request(_SEARCH, "/available_phone_numbers", params=search.params())
        return parse_items(_SEARCH, envelope, AvailableNumber)

    async def create_number_order(
        self,
        phone_numbers: Sequence[str],
        *,
        connection_id: str,
        customer_reference: str,
        messaging_profile_id: str | None = None,
    ) -> NumberOrder:
        if not phone_numbers:
            msg = "create_number_order needs at least one phone number"
            raise ValueError(msg)
        body: dict[str, Any] = {
            "phone_numbers": [{"phone_number": e164} for e164 in phone_numbers],
            "connection_id": connection_id,
            "customer_reference": customer_reference,
        }
        if messaging_profile_id is not None:
            body["messaging_profile_id"] = messaging_profile_id
        envelope = await self._transport.request(_ORDER, "/number_orders", json=body)
        return parse_data(_ORDER, envelope, NumberOrder)

    async def get_number_order(self, order_id: str) -> NumberOrder:
        envelope = await self._transport.request(_GET_ORDER, f"/number_orders/{segment(order_id)}")
        return parse_data(_GET_ORDER, envelope, NumberOrder)

    async def get_phone_number_by_e164(self, e164: str) -> PhoneNumber | None:
        """The owned number ``e164``, or ``None`` when the account does not own it."""
        envelope = await self._transport.request(
            _LOOKUP, "/phone_numbers", params={"filter[phone_number]": e164}
        )
        items = parse_items(_LOOKUP, envelope, PhoneNumber)
        return items[0] if items else None

    async def update_phone_number(
        self, number_id: str, *, connection_id: str | None = None, tags: Sequence[str] | None = None
    ) -> PhoneNumber:
        """PATCH ``connection_id`` and/or ``tags`` (``ValueError`` when neither is given)."""
        body: dict[str, Any] = {}
        if connection_id is not None:
            body["connection_id"] = connection_id
        if tags is not None:
            body["tags"] = list(tags)
        if not body:
            msg = "update_phone_number needs connection_id or tags"
            raise ValueError(msg)
        envelope = await self._transport.request(_UPDATE, f"/phone_numbers/{segment(number_id)}", json=body)
        return parse_data(_UPDATE, envelope, PhoneNumber)

    async def update_phone_number_messaging(
        self, number_id: str, messaging_profile_id: str | None
    ) -> PhoneNumberMessaging:
        """Assign the number to a messaging profile (``None`` detaches it)."""
        envelope = await self._transport.request(
            _UPDATE_MESSAGING,
            f"/phone_numbers/{segment(number_id)}/messaging",
            json={"messaging_profile_id": messaging_profile_id},
        )
        return parse_data(_UPDATE_MESSAGING, envelope, PhoneNumberMessaging)

    async def delete_phone_number(self, number_id: str) -> PhoneNumber:
        envelope = await self._transport.request(_DELETE, f"/phone_numbers/{segment(number_id)}")
        return parse_data(_DELETE, envelope, PhoneNumber)
