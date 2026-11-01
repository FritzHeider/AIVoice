"""List answers with ``limit`` and ``starting_after`` (specs/30 §4.1 Decision P03-T07a rules 6, 10)."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from typing import Any

from va_sim.stripe.errors import invalid_request
from va_sim.stripe.objects import list_json
from va_sim.stripe.params import Params, limit, string

__all__ = ["LIST_PARAMS", "paginate"]

LIST_PARAMS = frozenset({"limit", "starting_after", "ending_before"})


def paginate(newest_first: Iterable[Mapping[str, Any]], params: Params, url: str) -> dict[str, Any]:
    """A list object of up to ``limit`` items after ``starting_after`` (items are newest first)."""
    size = limit(params)
    after = string(params, "starting_after")
    if string(params, "ending_before") is not None:
        msg = "ending_before is not supported by the simulator; page with starting_after"
        raise invalid_request(
            msg,
            param="ending_before",
        )
    items = list(newest_first)
    if after is not None:
        ids = [item["id"] for item in items]
        if after not in ids:
            msg = f"No such object: '{after}'"
            raise invalid_request(msg, code="resource_missing", param="starting_after")
        items = items[ids.index(after) + 1 :]
    page = [copy.deepcopy(dict(item)) for item in items[:size]]
    return list_json(page, url, has_more=len(items) > size)
