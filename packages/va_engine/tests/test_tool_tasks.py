"""The executor tool-round helpers shared by task mode and pathways (specs/13 §5; Decisions P08-T02b1,
P08-T02c1)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from clock_helpers import ManualClock
from service_helpers import json_log_stream, log_events
from task_helpers import RecordingExecutor

from va_engine.system_tools import SystemToolFacts
from va_engine.task_tools import EXECUTOR_CONCURRENCY, FILLER_DELAY_S, TOOL_FAILED, PlannedCall
from va_engine.templating import TemplateScope
from va_engine.tool_executor import ToolAnswer
from va_engine.tool_tasks import (
    ToolLines,
    call_arguments,
    execute_gated,
    merge_variables,
    runnable_facts,
    start_tools,
    stop_tasks,
)
from va_llm.models import ToolCall
from va_voice.phrase_cache import TOOL_FILLERS


def planned(call_id: str, name: str = "lookup", arguments: str = "{}") -> PlannedCall:
    return PlannedCall(ToolCall(id=call_id, name=name, arguments_json=arguments), "execute")


def events_named(stream: Any, name: str) -> list[dict[str, Any]]:
    return [event for event in log_events(stream) if event["event"] == name]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ('{"city": "Paris"}', {"city": "Paris"}),
        ("", {}),
        ('{"city": "Par', {}),
        ('["Paris"]', {}),
        ('"Paris"', {}),
    ],
)
def test_call_arguments_are_a_mapping_or_empty(arguments: str, expected: dict[str, object]) -> None:
    assert call_arguments(ToolCall(id="c1", name="lookup", arguments_json=arguments)) == expected


def test_runnable_facts_keep_only_the_system_tools_the_executor_runs() -> None:
    facts = SystemToolFacts(
        runner="task",
        sms_capable=True,
        allow_schedule_callback=True,
        has_knowledge_bases=True,
        transfer_phone_number="+14155550199",
    )

    without = runnable_facts(facts, None)
    texting = runnable_facts(facts, RecordingExecutor(names=frozenset({"send_sms"})))

    assert (without.sms_capable, without.allow_schedule_callback, without.has_knowledge_bases) == (
        False,
        False,
        False,
    )
    assert (texting.sms_capable, texting.allow_schedule_callback, texting.has_knowledge_bases) == (
        True,
        False,
        False,
    )
    assert without.transfer_phone_number == "+14155550199"


async def test_a_raising_tool_answers_tool_failed() -> None:
    executor = RecordingExecutor(failing=frozenset({"lookup"}))
    stream = json_log_stream()

    answer = await execute_gated(executor, planned("c1").call, TemplateScope(), asyncio.Semaphore(1))

    assert answer == ToolAnswer(TOOL_FAILED)
    [event] = events_named(stream, "tool_failed")
    assert (event["tool"], event["error_type"]) == ("lookup", "RuntimeError")


async def test_no_executor_or_no_calls_start_no_tasks() -> None:
    assert start_tools(None, [planned("c1")], TemplateScope()) == {}
    assert start_tools(RecordingExecutor(), [], TemplateScope()) == {}


async def test_started_tools_run_gated_on_a_copy_of_the_variables() -> None:
    executor = RecordingExecutor(hold=True)
    variables: dict[str, object] = {"city": "Paris"}
    calls = [planned(f"c{index}") for index in range(EXECUTOR_CONCURRENCY + 1)]

    tasks = start_tools(executor, calls, TemplateScope(variables=variables))
    async with executor.changed:
        await executor.changed.wait_for(lambda: executor.active == EXECUTOR_CONCURRENCY)
    variables["city"] = "Rome"
    executor.release.set()
    results = {call_id: await task for call_id, task in tasks.items()}

    assert list(results) == [item.call.id for item in calls]
    assert executor.max_active == EXECUTOR_CONCURRENCY
    assert [scope.variables for scope in executor.scopes] == [{"city": "Paris"}] * len(calls)


def test_variables_merge_in_call_order() -> None:
    variables: dict[str, Any] = {"kept": 1}
    calls = [planned("c1"), planned("c2"), planned("c3")]
    results = {
        "c2": ToolAnswer("{}", {"hours": "10-6", "city": "Rome"}),
        "c1": ToolAnswer("{}", {"hours": "9-5"}),
    }

    assert merge_variables(variables, calls, results) is True
    assert variables == {"kept": 1, "hours": "10-6", "city": "Rome"}
    assert merge_variables(variables, calls, {"c1": ToolAnswer("{}")}) is False


async def test_stop_tasks_cancels_only_the_unfinished() -> None:
    gate = asyncio.Event()
    waiting = asyncio.create_task(gate.wait())
    finished = asyncio.create_task(asyncio.sleep(0, "done"))
    await finished

    await stop_tasks([waiting, finished])

    assert waiting.cancelled()
    assert finished.result() == "done"


async def held_round(
    executor: RecordingExecutor, *calls: PlannedCall
) -> tuple[dict[str, asyncio.Task[ToolAnswer]], TemplateScope]:
    """Start ``calls`` on a holding executor and wait until every one of them runs."""
    scope = TemplateScope(variables={"shop": "Bella Pasta"})
    tasks = start_tools(executor, calls, scope)
    async with executor.changed:
        await executor.changed.wait_for(lambda: executor.active == len(calls))
    return tasks, scope


async def finish(executor: RecordingExecutor, tasks: dict[str, asyncio.Task[ToolAnswer]]) -> None:
    executor.release.set()
    await asyncio.gather(*tasks.values())


async def test_the_line_is_the_first_speech_rendered_with_the_call_arguments() -> None:
    executor = RecordingExecutor(
        hold=True, speeches={"track": "Tracking {{input.order}} at {{shop}}.", "lookup": "Looking."}
    )
    silent, tracked = planned("c1", "quiet"), planned("c2", "track", '{"order": "A-7"}')
    tasks, scope = await held_round(executor, silent, tracked, planned("c3"))
    lines = ToolLines(executor, clock=ManualClock(), enabled=True)

    assert await lines.line([silent, tracked, planned("c3")], tasks, scope) == "Tracking A-7 at Bella Pasta."
    await finish(executor, tasks)


async def test_a_speech_with_unreadable_arguments_renders_without_them() -> None:
    executor = RecordingExecutor(hold=True, speeches={"lookup": "Checking{{input.city}}."})
    broken = planned("c1", "lookup", '{"city": "Par')
    tasks, scope = await held_round(executor, broken)
    lines = ToolLines(executor, clock=ManualClock(), enabled=True)

    assert await lines.line([broken], tasks, scope) == "Checking."
    await finish(executor, tasks)


@pytest.mark.parametrize("template", ["{{input.city", "   "])
async def test_an_invalid_or_blank_speech_falls_back_to_the_filler(template: str) -> None:
    executor = RecordingExecutor(hold=True, speeches={"lookup": template})
    tasks, scope = await held_round(executor, planned("c1"))
    clock = ManualClock()
    lines = ToolLines(executor, clock=clock, enabled=True)
    stream = json_log_stream()

    waiting = asyncio.create_task(lines.line([planned("c1")], tasks, scope))
    await clock.wait_for_sleep(FILLER_DELAY_S)
    await clock.run_for(FILLER_DELAY_S)

    assert await waiting == TOOL_FILLERS[0]
    assert len(events_named(stream, "tool_speech_invalid")) == int(template.startswith("{{"))
    await finish(executor, tasks)


async def test_disabled_lines_and_rounds_without_tasks_say_nothing() -> None:
    executor = RecordingExecutor(hold=True, speeches={"lookup": "Looking."})
    tasks, scope = await held_round(executor, planned("c1"))

    assert (
        await ToolLines(executor, clock=ManualClock(), enabled=False).line([planned("c1")], tasks, scope)
        is None
    )
    assert await ToolLines(executor, clock=ManualClock(), enabled=True).line([], {}, scope) is None
    await finish(executor, tasks)


async def test_fillers_rotate_while_a_tool_outlasts_the_delay() -> None:
    executor = RecordingExecutor(hold=True)
    clock = ManualClock()
    lines = ToolLines(executor, clock=clock, enabled=True)
    spoken: list[str | None] = []

    for index in range(len(TOOL_FILLERS) + 1):
        tasks, scope = await held_round(executor, planned(f"c{index}"))
        waiting = asyncio.create_task(lines.line([planned(f"c{index}")], tasks, scope))
        await clock.wait_for_sleep(FILLER_DELAY_S)
        await clock.run_for(FILLER_DELAY_S)
        spoken.append(await waiting)
        await finish(executor, tasks)
        executor.release.clear()

    assert spoken == [*TOOL_FILLERS, TOOL_FILLERS[0]]


async def test_tools_finishing_within_the_delay_get_no_filler() -> None:
    executor = RecordingExecutor(hold=True)
    clock = ManualClock()
    lines = ToolLines(executor, clock=clock, enabled=True)
    tasks, scope = await held_round(executor, planned("c1"), planned("c2"))

    waiting = asyncio.create_task(lines.line([planned("c1"), planned("c2")], tasks, scope))
    await clock.wait_for_sleep(FILLER_DELAY_S)
    await finish(executor, tasks)

    assert await waiting is None
    assert clock.sleepers == 0
    assert await lines.line([planned("c1")], tasks, scope) is None


async def test_lines_without_an_executor_have_no_speech() -> None:
    done = asyncio.create_task(asyncio.sleep(0, ToolAnswer("{}")))
    await done

    assert (
        await ToolLines(None, clock=ManualClock(), enabled=True).line(
            [planned("c1")], {"c1": done}, TemplateScope()
        )
        is None
    )
