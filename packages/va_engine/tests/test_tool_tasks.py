"""The executor tool-round helpers shared by task mode and pathways (specs/13 §5; Decisions P08-T02b1,
P08-T02c1, P08-FIX-16, P08-FIX-20)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from clock_helpers import ManualClock
from service_helpers import json_log_stream, log_events
from task_helpers import RecordingExecutor

from va_engine.system_tools import SystemToolFacts
from va_engine.task_tools import (
    ENDING,
    EXECUTOR_CONCURRENCY,
    FILLER_DELAY_S,
    INTERRUPTED,
    SKIPPED,
    TOOL_FAILED,
    TRANSFERRING,
    PlannedCall,
    RoundPlan,
    sent_digits,
)
from va_engine.templating import TemplateScope
from va_engine.tool_executor import ToolAnswer
from va_engine.tool_tasks import (
    ToolLines,
    call_arguments,
    cut_contents,
    execute_gated,
    finished_answers,
    merge_variables,
    round_contents,
    runnable_facts,
    settle_round,
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
    assert start_tools(None, [planned("c1")], TemplateScope()) == []
    assert start_tools(RecordingExecutor(), [], TemplateScope()) == []


async def test_started_tools_run_gated_on_a_copy_of_the_variables() -> None:
    executor = RecordingExecutor(hold=True)
    variables: dict[str, object] = {"city": "Paris"}
    calls = [planned(f"c{index}") for index in range(EXECUTOR_CONCURRENCY + 1)]

    tasks = start_tools(executor, calls, TemplateScope(variables=variables))
    async with executor.changed:
        await executor.changed.wait_for(lambda: executor.active == EXECUTOR_CONCURRENCY)
    variables["city"] = "Rome"
    executor.release.set()
    answers = [await task for task in tasks]

    assert len(answers) == len(calls)
    assert [call.id for call in executor.calls] == [item.call.id for item in calls]
    assert executor.max_active == EXECUTOR_CONCURRENCY
    assert [scope.variables for scope in executor.scopes] == [{"city": "Paris"}] * len(calls)


def test_variables_merge_in_call_order() -> None:
    variables: dict[str, Any] = {"kept": 1}
    answers = [
        ToolAnswer("{}", {"hours": "9-5"}),
        ToolAnswer("{}", {"hours": "10-6", "city": "Rome"}),
        ToolAnswer("{}"),
    ]

    assert merge_variables(variables, answers) is True
    assert variables == {"kept": 1, "hours": "10-6", "city": "Rome"}
    assert merge_variables(variables, [ToolAnswer("{}")]) is False
    assert merge_variables(variables, []) is False


async def test_calls_without_ids_each_get_a_task() -> None:
    executor = RecordingExecutor(hold=True, results={"lookup": '{"hours":"9-5"}', "other": '{"city":"Rome"}'})
    calls = [planned("", "lookup"), planned("", "other")]

    tasks = start_tools(executor, calls, TemplateScope())
    await executor.wait_active(2)
    executor.release.set()

    assert [(await task).content for task in tasks] == ['{"hours":"9-5"}', '{"city":"Rome"}']
    assert [call.name for call in executor.calls] == ["lookup", "other"]


async def test_stopping_a_round_cancels_every_call_without_an_id() -> None:
    executor = RecordingExecutor(hold=True)

    tasks = start_tools(executor, [planned("", "lookup"), planned("", "other")], TemplateScope())
    await executor.wait_active(2)
    await stop_tasks(tasks)

    assert [task.cancelled() for task in tasks] == [True, True]
    assert sorted(name for name, _ in executor.cancelled) == ["lookup", "other"]


def test_round_contents_pair_calls_and_answers_by_position() -> None:
    pressed = PlannedCall(
        ToolCall(id="", name="press_button", arguments_json='{"digits":"1"}'),
        "press",
        answer=sent_digits("1"),
        digits="1",
    )
    refused = PlannedCall(ToolCall(id="", name="nope", arguments_json="{}"), "answer", answer='{"error":"x"}')
    first, blank, last = planned("", "lookup"), planned("", "quiet"), planned("", "other")
    plan = RoundPlan((first, pressed, blank, refused, last), invalid=True, used=5)
    answers = [ToolAnswer('{"hours":"9-5"}'), ToolAnswer(""), ToolAnswer('{"city":"Rome"}')]

    assert round_contents(plan, answers) == [
        (first, '{"hours":"9-5"}'),
        (pressed, sent_digits("1")),
        (blank, TOOL_FAILED),
        (refused, '{"error":"x"}'),
        (last, '{"city":"Rome"}'),
    ]


def test_round_contents_refuse_a_wrong_number_of_answers() -> None:
    plan = RoundPlan((planned("c1"), planned("c2")), invalid=False, used=2)

    with pytest.raises(ValueError, match="2 executed calls need as many answers, got 1"):
        round_contents(plan, [ToolAnswer("{}")])


def other(name: str, kind: Any, answer: str) -> PlannedCall:
    """A call the executor does not run, with its planned ``answer``."""
    return PlannedCall(ToolCall(id="", name=name, arguments_json="{}"), kind, answer=answer)


async def half_finished_round() -> tuple[RecordingExecutor, list[asyncio.Task[ToolAnswer]]]:
    """``lookup`` finished (extracting ``hours``), ``track`` still runs."""
    executor = RecordingExecutor(
        holding=frozenset({"track"}),
        results={"lookup": '{"hours":"9-5"}'},
        variables={"lookup": {"hours": "9-5"}},
    )
    tasks = start_tools(executor, [planned("", "lookup"), planned("", "track")], TemplateScope())
    await tasks[0]
    await executor.wait_active(1)
    return executor, tasks


async def test_finished_answers_keep_finished_runs_and_drop_cancelled_ones() -> None:
    executor, tasks = await half_finished_round()
    failing = RecordingExecutor(failing=frozenset({"lookup"}))
    [failed] = start_tools(failing, [planned("c9")], TemplateScope())
    await asyncio.wait([failed])

    before = finished_answers(tasks)
    await stop_tasks(tasks)

    assert before == [ToolAnswer('{"hours":"9-5"}', {"hours": "9-5"}), None]
    assert finished_answers(tasks) == before
    assert finished_answers([failed]) == [ToolAnswer(TOOL_FAILED)]
    assert [name for name, _ in executor.cancelled] == ["track"]


async def test_a_task_that_raised_has_no_finished_answer() -> None:
    async def broken() -> ToolAnswer:
        message = "boom"
        raise RuntimeError(message)

    task = asyncio.create_task(broken())
    await asyncio.wait([task])

    assert finished_answers([task]) == [None]


def test_cut_contents_answer_each_kind() -> None:
    done, cancelled, blank = planned("", "lookup"), planned("", "track"), planned("", "quiet")
    refused = other("nope", "answer", '{"error":"x"}')
    pressed = PlannedCall(
        ToolCall(id="", name="press_button", arguments_json='{"digits":"1"}'),
        "press",
        answer=sent_digits("1"),
        digits="1",
    )
    ending = other("end_call", "end", ENDING)
    transferring = other("transfer_call", "transfer", TRANSFERRING)
    skipped = other("late", "answer", SKIPPED)
    plan = RoundPlan(
        (done, cancelled, refused, blank, pressed, ending, transferring, skipped), invalid=True, used=7
    )
    answers = [ToolAnswer('{"hours":"9-5"}'), None, ToolAnswer("")]

    assert cut_contents(plan, answers) == [
        (done, '{"hours":"9-5"}'),
        (cancelled, INTERRUPTED),
        (refused, '{"error":"x"}'),
        (blank, TOOL_FAILED),
        (pressed, INTERRUPTED),
        (ending, INTERRUPTED),
        (transferring, INTERRUPTED),
        (skipped, SKIPPED),
    ]


def test_cut_contents_of_tools_that_never_started_read_interrupted() -> None:
    refused = other("nope", "answer", '{"error":"x"}')
    plan = RoundPlan((planned("c1"), refused, planned("c2")), invalid=True, used=3)

    assert [content for _, content in cut_contents(plan, [])] == [INTERRUPTED, '{"error":"x"}', INTERRUPTED]


async def test_settle_round_cancels_the_running_tools_and_reports_late_answers() -> None:
    executor, tasks = await half_finished_round()
    first, second = planned("", "lookup"), planned("", "track")
    plan = RoundPlan((first, second), invalid=False, used=2)

    late, rest = await settle_round(plan, tasks, answered=0, merged=False)

    assert late == [ToolAnswer('{"hours":"9-5"}', {"hours": "9-5"})]
    assert rest == [(first, '{"hours":"9-5"}'), (second, INTERRUPTED)]
    assert [task.cancelled() for task in tasks] == [False, True]
    assert executor.active == 0
    assert await settle_round(plan, tasks, answered=1, merged=True) == ([], [(second, INTERRUPTED)])
    assert await settle_round(plan, tasks, answered=2, merged=True) == ([], [])


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
) -> tuple[list[asyncio.Task[ToolAnswer]], TemplateScope]:
    """Start ``calls`` on a holding executor and wait until every one of them runs."""
    scope = TemplateScope(variables={"shop": "Bella Pasta"})
    tasks = start_tools(executor, calls, scope)
    async with executor.changed:
        await executor.changed.wait_for(lambda: executor.active == len(calls))
    return tasks, scope


async def finish(executor: RecordingExecutor, tasks: list[asyncio.Task[ToolAnswer]]) -> None:
    executor.release.set()
    await asyncio.gather(*tasks)


async def test_the_line_is_the_first_speech_rendered_with_the_call_arguments() -> None:
    executor = RecordingExecutor(
        hold=True, speeches={"track": "Tracking {{input.order}} at {{shop}}.", "lookup": "Looking."}
    )
    silent, tracked = planned("c1", "quiet"), planned("c2", "track", '{"order": "A-7"}')
    tasks, scope = await held_round(executor, silent, tracked, planned("c3"))
    lines = ToolLines(clock=ManualClock(), enabled=True)

    assert (
        await lines.line(executor, [silent, tracked, planned("c3")], tasks, scope)
        == "Tracking A-7 at Bella Pasta."
    )
    await finish(executor, tasks)


async def test_a_speech_with_unreadable_arguments_renders_without_them() -> None:
    executor = RecordingExecutor(hold=True, speeches={"lookup": "Checking{{input.city}}."})
    broken = planned("c1", "lookup", '{"city": "Par')
    tasks, scope = await held_round(executor, broken)
    lines = ToolLines(clock=ManualClock(), enabled=True)

    assert await lines.line(executor, [broken], tasks, scope) == "Checking."
    await finish(executor, tasks)


@pytest.mark.parametrize("template", ["{{input.city", "   "])
async def test_an_invalid_or_blank_speech_falls_back_to_the_filler(template: str) -> None:
    executor = RecordingExecutor(hold=True, speeches={"lookup": template})
    tasks, scope = await held_round(executor, planned("c1"))
    clock = ManualClock()
    lines = ToolLines(clock=clock, enabled=True)
    stream = json_log_stream()

    waiting = asyncio.create_task(lines.line(executor, [planned("c1")], tasks, scope))
    await clock.wait_for_sleep(FILLER_DELAY_S)
    await clock.run_for(FILLER_DELAY_S)

    assert await waiting == TOOL_FILLERS[0]
    assert len(events_named(stream, "tool_speech_invalid")) == int(template.startswith("{{"))
    await finish(executor, tasks)


async def test_disabled_lines_and_rounds_without_tasks_say_nothing() -> None:
    executor = RecordingExecutor(hold=True, speeches={"lookup": "Looking."})
    tasks, scope = await held_round(executor, planned("c1"))

    assert (
        await ToolLines(clock=ManualClock(), enabled=False).line(executor, [planned("c1")], tasks, scope)
        is None
    )
    assert await ToolLines(clock=ManualClock(), enabled=True).line(executor, [], [], scope) is None
    await finish(executor, tasks)


async def test_fillers_rotate_while_a_tool_outlasts_the_delay() -> None:
    executor = RecordingExecutor(hold=True)
    clock = ManualClock()
    lines = ToolLines(clock=clock, enabled=True)
    spoken: list[str | None] = []

    for index in range(len(TOOL_FILLERS) + 1):
        tasks, scope = await held_round(executor, planned(f"c{index}"))
        waiting = asyncio.create_task(lines.line(executor, [planned(f"c{index}")], tasks, scope))
        await clock.wait_for_sleep(FILLER_DELAY_S)
        await clock.run_for(FILLER_DELAY_S)
        spoken.append(await waiting)
        await finish(executor, tasks)
        executor.release.clear()

    assert spoken == [*TOOL_FILLERS, TOOL_FILLERS[0]]


async def test_tools_finishing_within_the_delay_get_no_filler() -> None:
    executor = RecordingExecutor(hold=True)
    clock = ManualClock()
    lines = ToolLines(clock=clock, enabled=True)
    tasks, scope = await held_round(executor, planned("c1"), planned("c2"))

    waiting = asyncio.create_task(lines.line(executor, [planned("c1"), planned("c2")], tasks, scope))
    await clock.wait_for_sleep(FILLER_DELAY_S)
    await finish(executor, tasks)

    assert await waiting is None
    assert clock.sleepers == 0
    assert await lines.line(executor, [planned("c1")], tasks, scope) is None


async def test_lines_without_an_executor_have_no_speech() -> None:
    done = asyncio.create_task(asyncio.sleep(0, ToolAnswer("{}")))
    await done

    assert (
        await ToolLines(clock=ManualClock(), enabled=True).line(
            None, [planned("c1")], [done], TemplateScope()
        )
        is None
    )
