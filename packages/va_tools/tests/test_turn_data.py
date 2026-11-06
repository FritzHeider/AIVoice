"""Per-turn dynamic data (specs/13 §7 Decisions P08-T03b and P08-FIX-23) over respx, fakeredis and a manual
clock."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import respx
from clock_helpers import ManualClock
from engine_helpers import PATHWAY_ID, KeyedPort, drive, engine_rig, user
from fakeredis import FakeAsyncRedis
from graph_helpers import edge, graph, node, start
from pydantic import JsonValue
from service_helpers import json_log_stream, log_events
from tools_helpers import API, ORG, HeldAnswer, manual_fetcher

from va_domain.calls.tools import DynamicDataSpec
from va_engine.engine import new_pathway_state
from va_engine.navigation import ActiveGraph
from va_engine.ports import HttpCall, HttpOutcome
from va_engine.templating import TemplateScope
from va_engine.turn_data import TurnData, TurnRefresh, commit_refresh
from va_tools.dynamic_data import DynamicDataFetcher, DynamicDataResult
from va_tools.turn_data import (
    MAX_PAUSE_S,
    PAUSE_AFTER,
    PAUSE_S,
    TURN_BUDGET_S,
    TURN_TIMEOUT_MS,
    TurnDynamicData,
    turn_specs,
)

SCOPE = TemplateScope(variables={"tier": "gold"}, request_data={}, builtins={"to": "+14155550123"})
ITEMS = [
    {"name": "plan", "data": "$.plan", "context": "Their plan is {{plan}}."},
    {"name": "balance", "data": "$.balance", "context": "They owe {{balance}}."},
]
ORDER_ITEMS = [{"name": "order", "data": "$.last.id", "context": "Last order: {{order}}."}]
GOLD = ["Their plan is gold.", "They owe 3."]
LAST_ORDER = "Last order: A-7."


def _spec(path: str = "/plan", **fields: Any) -> DynamicDataSpec:
    values: dict[str, Any] = {
        "url": f"{API}{path}?phone={{{{to}}}}",
        "timing": "every_turn",
        "response_data": ITEMS,
    }
    return DynamicDataSpec.model_validate({**values, **fields})


def _orders(**fields: Any) -> DynamicDataSpec:
    return _spec("/orders", response_data=ORDER_ITEMS, **fields)


@dataclass(slots=True)
class _Reported:
    """The fetches a refresh reported, in order."""

    results: list[DynamicDataResult] = field(default_factory=list)

    def dynamic_data(self, result: DynamicDataResult) -> None:
        self.results.append(result)

    def outcomes(self) -> list[tuple[int, str, str]]:
        return [(result.index, result.outcome, result.level) for result in self.results]


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


def _applied(fetched: TurnRefresh) -> dict[str, JsonValue]:
    """Put a refresh in effect as a turn does; its variables."""
    fetched.apply()
    return dict(fetched.variables)


async def _refreshed(port: TurnData) -> dict[str, JsonValue]:
    """One refresh in the test scope, applied; its variables."""
    return _applied(await port.refresh(SCOPE))


def _slow_plan_beside_orders(api: respx.MockRouter) -> HeldAnswer:
    """``/plan`` answers only when the test lets it; ``/orders`` answers at once."""
    slow = HeldAnswer({"plan": "platinum", "balance": 12})
    api.get("/plan").mock(side_effect=slow)
    api.get("/orders").respond(200, json={"last": {"id": "A-8"}})
    return slow


def _plan_and_orders(fetcher: DynamicDataFetcher, reported: _Reported) -> TurnDynamicData:
    """The plan (index 0) and the last order (index 2), both with the lines of an earlier fetch."""
    specs = [(0, _spec()), (2, _orders())]
    return TurnDynamicData(fetcher, specs, initial={0: GOLD, 2: [LAST_ORDER]}, events=reported)


async def test_a_refresh_sets_the_variables_and_replaces_that_fetch_s_lines(api: respx.MockRouter) -> None:
    route = api.get("/plan").respond(200, json={"plan": "platinum", "balance": 12})
    reported = _Reported()
    async with manual_fetcher() as (_clock, fetcher):
        data = TurnDynamicData(fetcher, [(1, _spec())], initial={1: GOLD}, events=reported)
        port: TurnData = data
        assert port.sections() == tuple(GOLD)
        variables = await _refreshed(port)

    assert variables == {"plan": "platinum", "balance": 12}
    assert data.sections() == ("Their plan is platinum.", "They owe 12.")
    request = route.calls.last.request
    assert str(request.url) == f"{API}/plan?phone=%2B14155550123"
    assert request.extensions["timeout"]["read"] == TURN_TIMEOUT_MS / 1000
    assert [result.event_data() for result in reported.results] == [
        {
            "url": f"{API}/plan",
            "method": "GET",
            "status_code": 200,
            "duration_ms": 0,
            "variables_set": ["plan", "balance"],
            "cached": False,
            "outcome": "ok",
            "timing": "every_turn",
            "index": 1,
        }
    ]


async def test_a_refresh_changes_no_line_and_reports_nothing_until_it_is_applied(
    api: respx.MockRouter,
) -> None:
    api.get("/plan").respond(200, json={"plan": "platinum", "balance": 12})
    api.get("/orders").respond(404, json={})
    reported = _Reported()
    async with manual_fetcher() as (_clock, fetcher):
        data = _plan_and_orders(fetcher, reported)
        fetched = await data.refresh(SCOPE)

        assert fetched.variables == {"plan": "platinum", "balance": 12}
        assert [(result.index, result.outcome) for result in fetched.results] == [
            (0, "ok"),
            (2, "http_error"),
        ]
        assert data.sections() == (*GOLD, LAST_ORDER)
        assert reported.results == []

        fetched.apply()

    assert data.sections() == ("Their plan is platinum.", "They owe 12.", LAST_ORDER)
    assert reported.outcomes() == [(0, "ok", "info"), (2, "http_error", "warning")]


async def test_a_refresh_that_is_never_applied_leaves_no_trace_and_the_next_one_counts(
    api: respx.MockRouter,
) -> None:
    api.get("/plan").mock(
        side_effect=[
            httpx.Response(200, json={"plan": "platinum", "balance": 12}),
            httpx.Response(200, json={"plan": "silver", "balance": 0}),
        ]
    )
    reported = _Reported()
    variables: dict[str, JsonValue] = {"plan": "gold"}
    async with manual_fetcher() as (_clock, fetcher):
        data = TurnDynamicData(fetcher, [(0, _spec())], initial={0: GOLD}, events=reported)
        await data.refresh(SCOPE)  # a turn cut after its refresh finished drops the result

        assert (data.sections(), reported.results, variables) == (tuple(GOLD), [], {"plan": "gold"})

        changed = commit_refresh(variables, await data.refresh(SCOPE))

    assert changed is True
    assert variables == {"plan": "silver", "balance": 0}
    assert data.sections() == ("Their plan is silver.", "They owe 0.")
    assert reported.outcomes() == [(0, "ok", "info")]


async def test_a_fetch_over_the_budget_keeps_the_stale_lines_and_sets_nothing(api: respx.MockRouter) -> None:
    slow = HeldAnswer({"plan": "platinum", "balance": 12})
    api.get("/plan").mock(side_effect=slow)
    reported = _Reported()
    async with manual_fetcher() as (clock, fetcher):
        data = TurnDynamicData(fetcher, [(0, _spec())], initial={0: GOLD}, events=reported)
        running = asyncio.create_task(data.refresh(SCOPE))
        async with asyncio.timeout(5):
            await slow.arrived.wait()
        await clock.wait_for_sleep(TURN_BUDGET_S)
        assert not running.done()
        await clock.run_for(TURN_BUDGET_S)
        variables = _applied(await running)

    assert variables == {}
    assert data.sections() == tuple(GOLD)
    assert slow.cancelled
    assert reported.outcomes() == [(0, "timeout", "warning")]
    assert reported.results[0].duration_ms == 800


async def test_a_fast_fetch_counts_when_another_runs_out_of_time(api: respx.MockRouter) -> None:
    slow = _slow_plan_beside_orders(api)
    reported = _Reported()
    async with manual_fetcher() as (clock, fetcher):
        data = _plan_and_orders(fetcher, reported)
        running = asyncio.create_task(data.refresh(SCOPE))
        async with asyncio.timeout(5):
            await slow.arrived.wait()
        await clock.wait_for_sleep(TURN_BUDGET_S)
        await clock.run_for(TURN_BUDGET_S)
        variables = _applied(await running)

    assert variables == {"order": "A-8"}
    assert data.sections() == (*GOLD, "Last order: A-8.")
    assert reported.outcomes() == [(0, "timeout", "warning"), (2, "ok", "info")]


async def _gold_refreshed(reported: _Reported) -> tuple[dict[str, JsonValue], tuple[str, ...]]:
    """One applied refresh of the plan entry that had the gold lines: its variables and the lines after."""
    async with manual_fetcher() as (_clock, fetcher):
        data = TurnDynamicData(fetcher, [(0, _spec())], initial={0: GOLD}, events=reported)
        variables = await _refreshed(data)
    return variables, data.sections()


async def test_a_failed_refresh_keeps_the_earlier_lines(api: respx.MockRouter) -> None:
    api.get("/plan").respond(500, json={"plan": "platinum", "balance": 12})
    reported = _Reported()

    assert await _gold_refreshed(reported) == ({}, tuple(GOLD))
    assert reported.outcomes() == [(0, "http_error", "warning")]
    assert reported.results[0].message() == "Dynamic data failed: http_error (500)"


@pytest.mark.parametrize(
    "content",
    [b"<html>Bad gateway</html>", b"", b'"maintenance"', b"null"],
    ids=["html", "empty", "json-string", "json-null"],
)
async def test_a_2xx_answer_that_is_no_json_document_keeps_the_earlier_lines(
    api: respx.MockRouter, content: bytes
) -> None:
    """A proxy's 200 error page says nothing about the data: it must not erase what a 500 keeps."""
    api.get("/plan").respond(200, content=content)
    reported = _Reported()

    assert await _gold_refreshed(reported) == ({}, tuple(GOLD))
    assert reported.outcomes() == [(0, "ok", "info")]
    assert reported.results[0].event_data()["variables_set"] == []


async def test_an_answer_cut_at_the_size_cap_keeps_the_earlier_lines_and_sets_nothing() -> None:
    class Cut:
        async def execute(self, call: HttpCall) -> HttpOutcome:
            return HttpOutcome(
                kind="ok",
                status=200,
                body={"plan": "plat"},
                truncated=True,
                extracted={name: "plat" for name, _ in call.extract},
            )

    clock = ManualClock()
    fetcher = DynamicDataFetcher(Cut(), redis=None, org_id=ORG, clock=clock)
    data = TurnDynamicData(fetcher, [(0, _spec())], initial={0: GOLD})

    variables = await _refreshed(data)

    assert variables == {}
    assert data.sections() == tuple(GOLD)


async def test_a_cached_answer_is_used_without_a_request(api: respx.MockRouter) -> None:
    route = api.get("/plan").respond(200, json={"plan": "platinum", "balance": 12})
    reported = _Reported()
    redis = FakeAsyncRedis()
    async with manual_fetcher(redis=redis) as (_clock, fetcher):
        data = TurnDynamicData(fetcher, [(0, _spec(cache=True))], events=reported)
        first = await _refreshed(data)
        second = await _refreshed(data)
        assert route.call_count == 1
        await redis.delete(*await redis.keys("dyn:*"))
        third = await _refreshed(data)
    await redis.aclose()

    assert first == second == third == {"plan": "platinum", "balance": 12}
    assert route.call_count == 2
    assert [result.cached for result in reported.results] == [False, True, False]
    assert reported.results[1].message() == "Dynamic data: 2 variables set (cached)"
    assert data.sections() == ("Their plan is platinum.", "They owe 12.")


async def test_one_unreadable_answer_does_not_pin_a_cached_entry(api: respx.MockRouter) -> None:
    """The stale rule keeps the lines; the cache must not then keep the endpoint from being asked again."""
    route = api.get("/plan").mock(
        side_effect=[
            httpx.Response(200, text="<html>proxy error</html>"),
            httpx.Response(200, json={"plan": "platinum", "balance": 12}),
        ]
    )
    reported = _Reported()
    redis = FakeAsyncRedis()
    async with manual_fetcher(redis=redis) as (_clock, fetcher):
        data = TurnDynamicData(fetcher, [(0, _spec(cache=True))], initial={0: GOLD}, events=reported)
        first = await _refreshed(data)
        stale = data.sections()
        second = await _refreshed(data)
        third = await _refreshed(data)
    await redis.aclose()

    assert (first, stale) == ({}, tuple(GOLD))
    assert second == third == {"plan": "platinum", "balance": 12}
    assert data.sections() == ("Their plan is platinum.", "They owe 12.")
    assert route.call_count == 2
    assert [(result.cached, result.readable) for result in reported.results] == [
        (False, False),
        (False, True),
        (True, True),
    ]


async def test_a_variable_the_answer_no_longer_has_loses_its_line(api: respx.MockRouter) -> None:
    api.get("/plan").respond(200, json={"plan": "silver", "balance": None})
    async with manual_fetcher() as (_clock, fetcher):
        data = TurnDynamicData(fetcher, [(0, _spec())], initial={0: GOLD})
        variables = await _refreshed(data)

    assert variables == {"plan": "silver"}
    assert data.sections() == ("Their plan is silver.",)


async def test_a_later_entry_wins_a_name_and_both_keep_their_lines(api: respx.MockRouter) -> None:
    api.get("/plan").respond(200, json={"plan": "silver"})
    api.get("/upgrade").respond(200, json={"plan": "platinum"})
    async with manual_fetcher() as (_clock, fetcher):
        data = _shared_plan(fetcher)
        variables = await _refreshed(data)

    assert variables == {"plan": "platinum"}
    assert data.sections() == ("Their plan is silver.", "Their plan is platinum.")


def _shared_plan(fetcher: DynamicDataFetcher) -> TurnDynamicData:
    """Two entries that both set ``plan``: ``/plan`` (index 0) and ``/upgrade`` (index 1)."""
    return TurnDynamicData(fetcher, [(0, _spec()), (1, _spec("/upgrade"))])


@pytest.mark.parametrize(
    "answer",
    [httpx.Response(500), httpx.Response(200, text="<html>Bad gateway</html>"), httpx.Response(200)],
    ids=["failed", "html", "empty"],
)
async def test_a_later_entry_that_does_not_count_keeps_the_name_it_shares(
    api: respx.MockRouter, answer: httpx.Response
) -> None:
    """Its failure changes nothing: the variable must not flip to the earlier entry's value and back."""
    api.get("/plan").respond(200, json={"plan": "silver", "balance": 3})
    api.get("/upgrade").mock(
        side_effect=[
            httpx.Response(200, json={"plan": "platinum"}),
            answer,
            httpx.Response(200, json={"plan": "diamond"}),
        ]
    )
    async with manual_fetcher() as (_clock, fetcher):
        data = _shared_plan(fetcher)
        first = await _refreshed(data)
        second = await _refreshed(data)
        lines = data.sections()
        third = await _refreshed(data)

    assert first == {"plan": "platinum", "balance": 3}
    assert second == {"balance": 3}
    assert lines == ("Their plan is silver.", "They owe 3.", "Their plan is platinum.")
    assert third == {"plan": "diamond", "balance": 3}


async def test_a_later_entry_that_times_out_keeps_the_name_it_shares(api: respx.MockRouter) -> None:
    api.get("/plan").respond(200, json={"plan": "silver"})
    upgrade = HeldAnswer({"plan": "platinum"})
    upgrade.gate.set()
    api.get("/upgrade").mock(side_effect=upgrade)
    async with manual_fetcher() as (clock, fetcher):
        data = _shared_plan(fetcher)
        first = await _refreshed(data)
        upgrade.gate.clear()
        upgrade.arrived.clear()
        running = asyncio.create_task(data.refresh(SCOPE))
        async with asyncio.timeout(5):
            await upgrade.arrived.wait()
        await clock.wait_for_sleep(TURN_BUDGET_S)
        await clock.run_for(TURN_BUDGET_S)
        second = _applied(await running)

    assert (first, second) == ({"plan": "platinum"}, {})
    assert data.sections() == ("Their plan is silver.", "Their plan is platinum.")
    assert upgrade.cancelled


async def test_an_entry_holds_only_the_names_its_last_applied_counted_fetch_set(
    api: respx.MockRouter,
) -> None:
    api.get("/plan").respond(200, json={"plan": "silver"})
    api.get("/upgrade").mock(
        side_effect=[
            httpx.Response(500),
            httpx.Response(200, json={"plan": "platinum"}),
            httpx.Response(500),
            httpx.Response(200, json={"plan": "platinum"}),
            httpx.Response(500),
            httpx.Response(200, json={"plan": None}),
            httpx.Response(500),
        ]
    )
    async with manual_fetcher() as (_clock, fetcher):
        data = _shared_plan(fetcher)
        never_counted = await _refreshed(data)
        await data.refresh(SCOPE)
        never_applied = await _refreshed(data)
        counted = await _refreshed(data)
        held = await _refreshed(data)
        released = await _refreshed(data)
        after_release = await _refreshed(data)

    assert (never_counted, never_applied) == ({"plan": "silver"}, {"plan": "silver"})
    assert (counted, held) == ({"plan": "platinum"}, {})
    assert (released, after_release) == ({"plan": "silver"}, {"plan": "silver"})


async def test_an_earlier_entry_that_does_not_count_takes_nothing_from_a_later_one(
    api: respx.MockRouter,
) -> None:
    api.get("/plan").mock(side_effect=[httpx.Response(200, json={"plan": "silver"}), httpx.Response(500)])
    api.get("/upgrade").respond(200, json={"plan": "platinum"})
    async with manual_fetcher() as (_clock, fetcher):
        data = _shared_plan(fetcher)
        first = await _refreshed(data)
        second = await _refreshed(data)

    assert first == second == {"plan": "platinum"}
    assert data.sections() == ("Their plan is silver.", "Their plan is platinum.")


async def test_one_event_per_fetch_in_dynamic_data_order(api: respx.MockRouter) -> None:
    api.get("/plan").respond(200, json={"plan": "silver"})
    api.get("/orders").respond(404, json={})
    reported = _Reported()
    async with manual_fetcher() as (_clock, fetcher):
        data = TurnDynamicData(fetcher, [(3, _orders()), (1, _spec())], events=reported)
        await _refreshed(data)
        await _refreshed(data)

    assert reported.outcomes() == [(1, "ok", "info"), (3, "http_error", "warning")] * 2
    assert {result.timing for result in reported.results} == {"every_turn"}
    assert data.sections() == ("Their plan is silver.",)


async def test_only_every_turn_entries_are_refreshed(api: respx.MockRouter) -> None:
    pre_call = _spec("/contact", timing="pre_call")
    entries = [pre_call, _spec(), _orders(timing="pre_call"), _orders()]
    specs = turn_specs(entries)
    assert [(index, spec.url) for index, spec in specs] == [
        (1, f"{API}/plan?phone={{{{to}}}}"),
        (3, f"{API}/orders?phone={{{{to}}}}"),
    ]
    assert turn_specs([pre_call]) == []

    contact = api.get("/contact").respond(200, json={})
    api.get("/plan").respond(200, json={"plan": "silver"})
    api.get("/orders").respond(200, json={"last": {"id": "A-8"}})
    async with manual_fetcher() as (_clock, fetcher):
        data = TurnDynamicData(fetcher, specs, initial={0: ["A pre-call line."], 3: ["Last order: A-7."]})
        assert data.sections() == ("Last order: A-7.",)
        variables = await _refreshed(data)

    assert variables == {"plan": "silver", "order": "A-8"}
    assert data.sections() == ("Their plan is silver.", "Last order: A-8.")
    assert contact.call_count == 0


async def test_a_cancelled_refresh_changes_nothing_and_reports_nothing(api: respx.MockRouter) -> None:
    slow = _slow_plan_beside_orders(api)
    reported = _Reported()
    async with manual_fetcher() as (_clock, fetcher):
        data = _plan_and_orders(fetcher, reported)
        running = asyncio.create_task(data.refresh(SCOPE))
        async with asyncio.timeout(5):
            await slow.arrived.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

    assert slow.cancelled
    assert data.sections() == (*GOLD, LAST_ORDER)
    assert reported.results == []


async def test_a_reporter_that_raises_costs_the_turn_nothing(api: respx.MockRouter) -> None:
    class Broken:
        def dynamic_data(self, result: DynamicDataResult) -> None:
            raise RuntimeError(result.outcome)

    stream = json_log_stream()
    api.get("/plan").respond(200, json={"plan": "silver"})
    async with manual_fetcher() as (_clock, fetcher):
        data = TurnDynamicData(fetcher, [(0, _spec())], events=Broken())
        variables: dict[str, JsonValue] = {}
        changed = commit_refresh(variables, await data.refresh(SCOPE))

    assert changed is True
    assert variables == {"plan": "silver"}
    assert data.sections() == ("Their plan is silver.",)
    failures = [line for line in log_events(stream) if line["event"] == "tools.turn_data_event_failed"]
    assert [line["error_type"] for line in failures] == ["RuntimeError"]


# An endpoint that does not answer is paused (Decision P08-FIX-23).

PLATINUM: dict[str, JsonValue] = {"plan": "platinum", "balance": 12}
PAUSED_MESSAGE = "Dynamic data paused: endpoint not answering"
EDGE_S = 0.5
"""How long before the end of a pause the tests look again (binary-exact, so the sums are too)."""


def _refused() -> httpx.ConnectError:
    return httpx.ConnectError("refused")


def _gold_plan(fetcher: DynamicDataFetcher, reported: _Reported | None = None) -> TurnDynamicData:
    """The plan entry (index 0) with the gold lines of an earlier fetch."""
    return TurnDynamicData(fetcher, [(0, _spec())], initial={0: GOLD}, events=reported)


async def _turns(data: TurnDynamicData, count: int) -> list[dict[str, JsonValue]]:
    """``count`` applied refreshes; the variables of each."""
    return [await _refreshed(data) for _ in range(count)]


def test_the_pause_constants_are_the_documented_ones() -> None:
    assert (PAUSE_AFTER, PAUSE_S, MAX_PAUSE_S) == (2, 30.0, 120.0)


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [(httpx.ReadTimeout("slow"), "timeout"), (httpx.ConnectError("refused"), "connection")],
    ids=["timeout", "connection"],
)
async def test_two_unanswered_fetches_pause_the_entry(
    api: respx.MockRouter, failure: Exception, outcome: str
) -> None:
    route = api.get("/plan").mock(side_effect=failure)
    reported = _Reported()
    async with manual_fetcher() as (_clock, fetcher):
        data = _gold_plan(fetcher, reported)
        variables = await _turns(data, 3)

    assert variables == [{}, {}, {}]
    assert route.call_count == 2
    assert reported.outcomes() == [(0, outcome, "warning"), (0, outcome, "warning"), (0, "paused", "warning")]
    paused = reported.results[2]
    assert paused.message() == PAUSED_MESSAGE
    assert paused.event_data() == {
        "url": f"{API}/plan",
        "method": "GET",
        "status_code": None,
        "duration_ms": 0,
        "variables_set": [],
        "cached": False,
        "outcome": "paused",
        "timing": "every_turn",
        "index": 0,
    }
    assert data.sections() == tuple(GOLD)


async def _over_budget(clock: ManualClock, data: TurnDynamicData, slow: HeldAnswer) -> dict[str, JsonValue]:
    """One applied refresh whose held request runs out of the turn's budget; its variables."""
    slow.arrived.clear()
    running = asyncio.create_task(data.refresh(SCOPE))
    async with asyncio.timeout(5):
        await slow.arrived.wait()
    await clock.wait_for_sleep(TURN_BUDGET_S)
    await clock.run_for(TURN_BUDGET_S)
    return _applied(await running)


async def test_a_budget_timeout_counts_as_unanswered(api: respx.MockRouter) -> None:
    """The fetch the refresh cancels at its budget, not only the request the executor times out."""
    slow = HeldAnswer(PLATINUM)
    api.get("/plan").mock(side_effect=slow)
    reported = _Reported()
    async with manual_fetcher() as (clock, fetcher):
        data = _gold_plan(fetcher, reported)
        first = await _over_budget(clock, data, slow)
        second = await _over_budget(clock, data, slow)
        slow.arrived.clear()
        third = await _refreshed(data)

    assert (first, second, third) == ({}, {}, {})
    assert not slow.arrived.is_set()  # respx counts only requests that completed: none did, and no third came
    assert [outcome for _, outcome, _ in reported.outcomes()] == ["timeout", "timeout", "paused"]
    assert data.sections() == tuple(GOLD)


async def test_a_refresh_of_paused_entries_does_not_wait(api: respx.MockRouter) -> None:
    api.get("/plan").mock(side_effect=_refused())
    api.get("/orders").mock(side_effect=_refused())
    reported = _Reported()
    async with manual_fetcher() as (clock, fetcher):
        data = _plan_and_orders(fetcher, reported)
        await _turns(data, 2)

        refresh = data.refresh(SCOPE)
        with pytest.raises(StopIteration) as finished:  # it returns without suspending once
            refresh.send(None)
        fetched: TurnRefresh = finished.value.value

        assert clock.sleepers == 0
        assert _applied(fetched) == {}

    assert reported.outcomes()[4:] == [(0, "paused", "warning"), (2, "paused", "warning")]
    assert data.sections() == (*GOLD, LAST_ORDER)


async def test_only_the_unanswering_entry_is_paused(api: respx.MockRouter) -> None:
    plan = api.get("/plan").mock(side_effect=_refused())
    orders = api.get("/orders").mock(
        side_effect=[httpx.Response(200, json={"last": {"id": f"A-{number}"}}) for number in (8, 9, 10)]
    )
    reported = _Reported()
    async with manual_fetcher() as (_clock, fetcher):
        data = _plan_and_orders(fetcher, reported)
        variables = await _turns(data, 3)

    assert variables == [{"order": "A-8"}, {"order": "A-9"}, {"order": "A-10"}]
    assert (plan.call_count, orders.call_count) == (2, 3)
    assert reported.outcomes()[4:] == [(0, "paused", "warning"), (2, "ok", "info")]
    assert data.sections() == (*GOLD, "Last order: A-10.")


async def test_the_pause_ends_after_30_s_and_an_answered_probe_resumes_the_entry(
    api: respx.MockRouter,
) -> None:
    route = api.get("/plan").mock(
        side_effect=[
            _refused(),
            _refused(),
            httpx.Response(200, json=PLATINUM),
            _refused(),
            httpx.Response(200, json={"plan": "silver", "balance": 0}),
        ]
    )
    reported = _Reported()
    async with manual_fetcher() as (clock, fetcher):
        data = _gold_plan(fetcher, reported)
        await _turns(data, 2)
        await clock.run_for(PAUSE_S - EDGE_S)
        still_paused = await _refreshed(data)
        sent_while_paused = route.call_count
        await clock.run_for(EDGE_S)
        probe = await _refreshed(data)
        lines = data.sections()
        after_one_failure = await _turns(data, 2)  # the streak starts again: one failure pauses nothing

    assert (still_paused, sent_while_paused) == ({}, 2)
    assert probe == PLATINUM
    assert lines == ("Their plan is platinum.", "They owe 12.")
    assert after_one_failure == [{}, {"plan": "silver", "balance": 0}]
    assert route.call_count == 5
    assert [outcome for _, outcome, _ in reported.outcomes()] == [
        "connection",
        "connection",
        "paused",
        "ok",
        "connection",
        "ok",
    ]


async def test_an_unanswered_probe_pauses_twice_as_long_up_to_two_minutes(api: respx.MockRouter) -> None:
    stream = json_log_stream()
    route = api.get("/plan").mock(side_effect=_refused())
    sent: list[tuple[float, int, int]] = []
    async with manual_fetcher() as (clock, fetcher):
        data = _gold_plan(fetcher)
        await _turns(data, 2)
        for pause_s in (30.0, 60.0, 120.0, 120.0):
            before = route.call_count
            await clock.run_for(pause_s - EDGE_S)
            await _refreshed(data)
            during = route.call_count - before
            await clock.run_for(EDGE_S)
            await _refreshed(data)  # the probe: unanswered, so the entry is paused again at once
            sent.append((pause_s, during, route.call_count - before))
        await _refreshed(data)

    assert sent == [(30.0, 0, 1), (60.0, 0, 1), (120.0, 0, 1), (120.0, 0, 1)]
    assert route.call_count == 6
    pauses = [line for line in log_events(stream) if line["event"] == "tools.turn_data_paused"]
    assert [(line["pause_s"], line["unanswered"]) for line in pauses] == [
        (30.0, 2),
        (60.0, 3),
        (120.0, 4),
        (120.0, 5),
        (120.0, 6),
    ]


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500),
        httpx.Response(200, json={"plan": "silver"}),
        httpx.Response(200, text="<html>Bad gateway</html>"),
    ],
    ids=["http_error", "ok", "unreadable"],
)
async def test_an_answered_fetch_between_two_unanswered_ones_resets_the_streak(
    api: respx.MockRouter, answer: httpx.Response
) -> None:
    route = api.get("/plan").mock(
        side_effect=[_refused(), answer, _refused(), httpx.Response(200, json=PLATINUM)]
    )
    reported = _Reported()
    async with manual_fetcher() as (_clock, fetcher):
        data = _gold_plan(fetcher, reported)
        variables = await _turns(data, 4)

    assert variables[3] == PLATINUM
    assert route.call_count == 4
    assert "paused" not in [outcome for _, outcome, _ in reported.outcomes()]


@pytest.mark.parametrize(
    ("fields", "outcome"),
    [({"url": "http://api.acme.test/plan"}, "blocked"), ({"headers": {"X-Token": "{{to"}}, "render_error")],
    ids=["blocked", "render_error"],
)
async def test_a_request_that_is_never_sent_does_not_pause_the_entry(
    api: respx.MockRouter, fields: dict[str, Any], outcome: str
) -> None:
    route = api.get("/plan").respond(200, json=PLATINUM)
    reported = _Reported()
    async with manual_fetcher() as (_clock, fetcher):
        data = TurnDynamicData(fetcher, [(0, _spec(**fields))], initial={0: GOLD}, events=reported)
        await _turns(data, 4)

    assert route.call_count == 0
    assert reported.outcomes() == [(0, outcome, "warning")] * 4


async def test_unanswered_fetches_of_refreshes_that_are_never_applied_count_nothing(
    api: respx.MockRouter,
) -> None:
    route = api.get("/plan").mock(side_effect=_refused())
    reported = _Reported()
    async with manual_fetcher() as (_clock, fetcher):
        data = _gold_plan(fetcher, reported)
        await data.refresh(SCOPE)  # two turns cut before their commit
        await data.refresh(SCOPE)
        await _turns(data, 2)
        sent_until_the_second_commit = route.call_count
        await _refreshed(data)

    assert sent_until_the_second_commit == 4
    assert route.call_count == 4
    assert [outcome for _, outcome, _ in reported.outcomes()] == ["connection", "connection", "paused"]


async def test_a_paused_result_that_is_never_applied_is_not_reported(api: respx.MockRouter) -> None:
    api.get("/plan").mock(side_effect=_refused())
    reported = _Reported()
    async with manual_fetcher() as (_clock, fetcher):
        data = _gold_plan(fetcher, reported)
        await _turns(data, 2)
        dropped = await data.refresh(SCOPE)

    assert [result.outcome for result in dropped.results] == ["paused"]
    assert len(reported.results) == 2


async def test_a_paused_later_entry_keeps_the_name_it_shares(api: respx.MockRouter) -> None:
    api.get("/plan").respond(200, json={"plan": "silver"})
    upgrade = api.get("/upgrade").mock(
        side_effect=[httpx.Response(200, json={"plan": "platinum"}), _refused(), _refused()]
    )
    async with manual_fetcher() as (_clock, fetcher):
        data = _shared_plan(fetcher)
        variables = await _turns(data, 4)

    assert variables == [{"plan": "platinum"}, {}, {}, {}]
    assert upgrade.call_count == 3
    assert data.sections() == ("Their plan is silver.", "Their plan is platinum.")


async def test_another_call_s_entry_is_not_paused(api: respx.MockRouter) -> None:
    route = api.get("/plan").mock(side_effect=_refused())
    other_call = _Reported()
    async with manual_fetcher() as (_clock, fetcher):
        await _turns(_gold_plan(fetcher), 3)
        sent_by_the_paused_call = route.call_count
        await _refreshed(_gold_plan(fetcher, other_call))

    assert sent_by_the_paused_call == 2
    assert route.call_count == 3
    assert other_call.outcomes() == [(0, "connection", "warning")]


async def test_a_pause_is_logged_once_without_the_url(api: respx.MockRouter) -> None:
    stream = json_log_stream()
    api.get("/plan").mock(side_effect=_refused())
    async with manual_fetcher() as (_clock, fetcher):
        await _turns(_gold_plan(fetcher), 4)

    lines = log_events(stream)
    pauses = [line for line in lines if line["event"] == "tools.turn_data_paused"]
    assert [(line["index"], line["pause_s"], line["unanswered"], line["level"]) for line in pauses] == [
        (0, 30.0, 2, "warning")
    ]
    assert not [value for value in pauses[0].values() if "acme" in str(value)]
    fetches = [line["outcome"] for line in lines if line["event"] == "tools.dynamic_data"]
    assert fetches == ["connection", "connection", "paused", "paused"]


class _Signalling:
    """A :class:`TurnData` over ``inner`` that tells when a refresh has returned."""

    def __init__(self, inner: TurnDynamicData) -> None:
        self._inner = inner
        self.fetched = asyncio.Event()

    def sections(self) -> tuple[str, ...]:
        return self._inner.sections()

    async def refresh(self, scope: TemplateScope) -> TurnRefresh:
        fetched = await self._inner.refresh(scope)
        self.fetched.set()
        return fetched


async def test_a_pathway_turn_cut_after_its_refresh_finished_keeps_lines_events_and_variables_old(
    api: respx.MockRouter,
) -> None:
    """The real port in a real engine: the refresh answers at once, the routing call is still held when
    the turn is superseded. Lines, events and variables stay as they were; the next turn commits all three."""
    api.get("/plan").respond(200, json={"plan": "platinum", "balance": 12})
    pathway = graph(
        [start("ask", prompt="Help with the {{plan}} plan."), node("billing", text="Your plan is {{plan}}.")],
        [edge("e_bill", "ask", "billing", "Asks about billing")],
    )
    reported = _Reported()
    gate = asyncio.Event()
    decisions = KeyedPort(gates={"select_path": gate})
    async with manual_fetcher() as (_clock, fetcher):
        data = TurnDynamicData(fetcher, [(0, _spec())], initial={0: GOLD}, events=reported)
        port = _Signalling(data)
        state = new_pathway_state(ActiveGraph(PATHWAY_ID, 1, pathway))
        state.variables["plan"] = "gold"
        rig = engine_rig(pathway, state=state, port=decisions, turn_data=port)
        rig.source.reply("Hello.").reply("speculative").reply("speculative").reply("You are on platinum.")
        await rig.start()
        cut = asyncio.create_task(rig.say("which plan am I on"))
        async with asyncio.timeout(5):
            await decisions.started.wait()
            await port.fetched.wait()
        assert not cut.done()

        cut.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cut

        assert rig.state.variables == {"plan": "gold"}
        assert data.sections() == tuple(GOLD)
        assert reported.results == []

        gate.set()
        merged = user("which plan am I on now", rig.state.turn_index + 1, supersedes=rig.state.turn_index)
        actions = await drive(rig.engine.handle_user_turn(merged))

    assert actions == [("stream", "You are on platinum.", "t2.1", None)]
    assert rig.state.variables == {"plan": "platinum", "balance": 12}
    assert data.sections() == ("Their plan is platinum.", "They owe 12.")
    assert reported.outcomes() == [(0, "ok", "info")]
