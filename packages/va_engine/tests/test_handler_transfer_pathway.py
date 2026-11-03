"""Transfer Pathway node handler (specs/11 §4 "Transfer Pathway" row, §3.2.6, Decision P07-T07)."""

from __future__ import annotations

from typing import Any

from graph_helpers import PW_BILLING, node
from handler_helpers import handler_ctx, spoken

from va_engine.handlers import TransferPathwayHandler

HANDLER = TransferPathwayHandler()


def transfer(**config: Any) -> dict[str, Any]:
    return node("hop", node_type="Transfer Pathway", transferPathway={"pathwayId": PW_BILLING, **config})


async def test_switch_graph_with_defaults() -> None:
    result = await HANDLER.on_enter(handler_ctx(transfer()))
    assert await spoken(result.actions) == [("switch", PW_BILLING, "production", None, True, False)]
    assert result.problem is None


async def test_switch_graph_carries_the_config() -> None:
    raw = transfer(version=4, nodeId=" billing_start ", carryVariables=False, returnOnEnd=True)
    result = await HANDLER.on_enter(handler_ctx(raw))
    assert await spoken(result.actions) == [("switch", PW_BILLING, 4, "billing_start", False, True)]


async def test_blank_node_id_means_the_start_node() -> None:
    result = await HANDLER.on_enter(handler_ctx(transfer(nodeId="  ")))
    assert await spoken(result.actions) == [("switch", PW_BILLING, "production", None, True, False)]


async def test_missing_pathway_is_a_problem() -> None:
    blank = await HANDLER.on_enter(handler_ctx(transfer(pathwayId=" ")))
    absent = await HANDLER.on_enter(handler_ctx(node("hop", node_type="Transfer Pathway")))
    for result in (blank, absent):
        assert result.actions == ()
        assert result.problem == "transfer_pathway_missing"


async def test_respond_is_delegated_and_never_waits() -> None:
    ctx = handler_ctx(transfer())
    assert (await HANDLER.respond(ctx)).actions == ()
    assert HANDLER.waits_for_user(ctx.node) is False
