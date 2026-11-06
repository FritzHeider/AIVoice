"""The per-turn data port helpers (specs/13 §7 Decision P08-T03b)."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import JsonValue
from service_helpers import json_log_stream, log_events
from turn_data_fakes import FakeTurnData

from va_engine.templating import TemplateScope
from va_engine.turn_data import TurnData, commit_refresh, merge_refreshed, refresh_turn

SCOPE = TemplateScope(variables={"plan": "gold"})
GOLD = "Their plan is gold."
PLATINUM = "Their plan is platinum."


async def test_without_a_port_nothing_is_refreshed() -> None:
    variables: dict[str, JsonValue] = {"plan": "gold"}

    refresh = await refresh_turn(None, SCOPE)

    assert refresh is None
    assert commit_refresh(variables, refresh) is False
    assert variables == {"plan": "gold"}


async def test_a_refresh_changes_nothing_until_it_is_committed() -> None:
    fake = FakeTurnData(lines=(GOLD,)).then({"plan": "platinum"}, PLATINUM)
    port: TurnData = fake
    variables: dict[str, JsonValue] = {"plan": "gold"}

    refresh = await refresh_turn(port, SCOPE)

    assert refresh is not None
    assert refresh.variables == {"plan": "platinum"}
    assert (port.sections(), fake.applied, variables) == ((GOLD,), [], {"plan": "gold"})

    assert commit_refresh(variables, refresh) is True

    assert (port.sections(), fake.applied, variables) == (
        (PLATINUM,),
        [{"plan": "platinum"}],
        {"plan": "platinum"},
    )


async def test_a_commit_that_changes_no_value_still_applies_the_lines() -> None:
    fake = FakeTurnData(lines=(GOLD,)).then({"plan": "gold"}, "Their plan is still gold.")
    variables: dict[str, JsonValue] = {"plan": "gold"}

    assert commit_refresh(variables, await refresh_turn(fake, SCOPE)) is False

    assert fake.sections() == ("Their plan is still gold.",)
    assert fake.applied == [{"plan": "gold"}]


async def test_a_refresh_whose_apply_raises_merges_nothing_and_is_logged() -> None:
    class Broken:
        variables: dict[str, JsonValue] = {"plan": "platinum"}  # noqa: RUF012 - read-only test double

        def apply(self) -> None:
            raise RuntimeError("boom")

    stream = json_log_stream()
    variables: dict[str, JsonValue] = {"plan": "gold"}

    assert commit_refresh(variables, Broken()) is False

    assert variables == {"plan": "gold"}
    failures = [line for line in log_events(stream) if line["event"] == "engine.turn_data_failed"]
    assert [line["error_type"] for line in failures] == ["RuntimeError"]
    assert "boom" not in stream.getvalue()


async def test_a_port_that_raises_sets_nothing_and_is_logged() -> None:
    stream = json_log_stream()
    port = FakeTurnData(lines=(GOLD,)).failing(RuntimeError("boom"))

    assert await refresh_turn(port, SCOPE) is None
    assert port.sections() == (GOLD,)
    assert port.applied == []
    failures = [line for line in log_events(stream) if line["event"] == "engine.turn_data_failed"]
    assert [line["error_type"] for line in failures] == ["RuntimeError"]
    assert "boom" not in stream.getvalue()


async def test_a_cancelled_refresh_stays_cancelled() -> None:
    port = FakeTurnData(hold=asyncio.Event())
    running = asyncio.create_task(refresh_turn(port, SCOPE))
    await port.started.wait()

    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running
    assert port.cancelled == 1


@pytest.mark.parametrize(
    ("refreshed", "changed", "merged"),
    [
        ({}, False, {"plan": "gold", "seats": 2}),
        ({"plan": "gold"}, False, {"plan": "gold", "seats": 2}),
        ({"plan": "platinum"}, True, {"plan": "platinum", "seats": 2}),
        ({"plan": "gold", "tier": {"level": 3}}, True, {"plan": "gold", "seats": 2, "tier": {"level": 3}}),
        ({"seats": None}, True, {"plan": "gold", "seats": None}),
    ],
    ids=["nothing", "equal", "changed", "new-name", "null"],
)
def test_merge_refreshed_sets_the_values_and_tells_whether_one_changed(
    refreshed: dict[str, JsonValue], changed: bool, merged: dict[str, JsonValue]
) -> None:
    variables: dict[str, JsonValue] = {"plan": "gold", "seats": 2}

    assert merge_refreshed(variables, refreshed) is changed
    assert variables == merged
