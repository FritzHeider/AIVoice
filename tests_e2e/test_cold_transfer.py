"""``cold_transfer`` (specs/30 §5): a human asks for a person, the task runner's ``transfer_call`` tool
speaks the final line and cold-transfers to ``transfer_phone_number``; the AI stream stops, the bridged caller
hangs up later, and the transferred time is billed at the transfer rate (Decision P05-T17c, specs/30 §5)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Final

import pytest
from e2e_harness import Harness, random_e164

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(600)]

TASK: Final = "You are Maya from Bella Pasta. If the caller asks for a person, transfer the call."
FIRST_SENTENCE: Final = "Hi, this is Maya from Bella Pasta."
FINAL_LINE: Final = "Connecting you now."
BRIDGED_MS: Final = 3000
# Seed `build` plan rates (specs/24 §2), dollars per minute, and its minimum attempt price.
CONNECTED_RATE: Final = Decimal("0.10")
TRANSFER_RATE: Final = Decimal("0.04")
MIN_ATTEMPT: Final = Decimal("0.015")
PRICE_TOLERANCE: Final = Decimal("0.00001")


def platform_commands(summary: dict[str, Any]) -> list[str]:
    return [command["name"] for command in summary["commands"] if not command["duplicate"]]


def transferred(summary: dict[str, Any]) -> bool:
    return bool(summary["transfers"]) and "streaming_stop" in platform_commands(summary)


def check_price(call: dict[str, Any]) -> None:
    """Price = connected min x connected rate + transfer min x transfer rate (+ any attempt top-up)."""
    price = Decimal(str(call["price"]))
    metered = (
        Decimal(str(call["call_length"])) * CONNECTED_RATE
        + Decimal(str(call["transfer_duration"])) * TRANSFER_RATE
    )
    assert metered - PRICE_TOLERANCE <= price <= metered + MIN_ATTEMPT + PRICE_TOLERANCE, (
        price,
        metered,
        call,
    )


async def test_cold_transfer(harness: Harness) -> None:
    org = await harness.create_org()
    scenario = await harness.load_scenario("cold_transfer")
    target = random_e164()
    call_id = await harness.send_task_call(
        org, scenario, TASK, first_sentence=FIRST_SENTENCE, transfer_phone_number=target
    )
    sim = await harness.sim_call(scenario.number)
    ccid = sim["call_control_id"]
    await harness.wait_sim(ccid, transferred, what="the transfer and streaming_stop", timeout_s=60)
    await harness.wait_until_sim_ms(await harness.sim_clock_ms() + BRIDGED_MS)
    await harness.remote_hangup(ccid)
    finished = await harness.finished(org, call_id, ccid=ccid)
    call = finished.call
    summary = await harness.sim_summary(ccid)

    assert not finished.failures(scenario), (call, finished.events, finished.timeline)
    assert summary["transfers"][0]["to"] == target, summary
    commands = platform_commands(summary)
    transfer_at = commands.index("transfer")
    assert "hangup" not in commands[:transfer_at], commands
    assert commands.index("streaming_stop", transfer_at) > transfer_at, commands
    assert FINAL_LINE in await harness.tts_texts()
    transfers = [event["data"] for event in finished.events if event["category"] == "transfer"]
    assert transfers[0] == {"type": "cold", "target": target}, transfers
    assert {} in transfers[1:], transfers
    assert call["transferred_to"] == target, call
    assert call["transfer_duration"] > 0, call
    assert call["call_ended_by"] == "SYSTEM", call
    check_price(call)
