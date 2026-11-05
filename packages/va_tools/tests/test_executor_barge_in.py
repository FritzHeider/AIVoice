"""A barge-in during a system tool run (Decisions P08-FIX-13, P08-FIX-21 and P08-FIX-22 under specs/13 §6):
the TaskBrain stops waiting. A tool that detaches is answered ``in_progress``, its run completes once, and the
executor's ``aclose`` waits for it; a tool that does not is cancelled with the wait and answered
``interrupted``."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import orjson
from dialogue_helpers import FakeSource, counter
from task_helpers import task_context, user_turn
from tools_helpers import plain_executor

from va_common.time import FrozenClock
from va_domain.org import OrgSettings
from va_engine.system_tools import facts_for_call
from va_engine.task_runner import TaskBrain
from va_engine.task_tools import IN_PROGRESS, INTERRUPTED
from va_llm.models import ToolCall
from va_tools.executor import CallToolExecutor, SystemOutcome
from va_tools.runner import ToolRunner
from va_voice.pipeline import SpeakStatic, SpeakText


@dataclass(slots=True)
class _HeldSms:
    """A ``send_sms`` tool whose run waits for ``gate`` and records the messages it sent."""

    name: str = "send_sms"
    detaches: bool = True
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    sent: list[str] = field(default_factory=list)
    cancelled: bool = False

    async def run(self, call: ToolCall) -> SystemOutcome:
        try:
            await self.gate.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        self.sent.append(orjson.loads(call.arguments_json)["message"])
        return SystemOutcome('{"status":"sent","segments":1}')


def _tool_messages(brain: TaskBrain) -> list[str | None]:
    return [m.content for m in brain.messages if m.role == "tool"]


async def _barge_in(sms: _HeldSms) -> tuple[TaskBrain, CallToolExecutor, ToolCall]:
    """Ask for a text, read the reply up to the filler of the held ``sms`` run, then barge in."""
    clock = FrozenClock()
    call = ToolCall(id="call_1", name="send_sms", arguments_json='{"message": "Your code is 42."}')
    source = FakeSource().reply(calls=[call]).reply("Sent.")
    context = task_context()
    settings = OrgSettings()
    async with plain_executor() as (http, http_clock):
        executor = CallToolExecutor([], runner=ToolRunner(http, clock=http_clock), system=[sms])
    brain = TaskBrain(
        context=context,
        settings=settings,
        gateway=source,
        counter=counter(),
        facts=facts_for_call(context, settings, sms_capable=True),
        clock=clock,
        executor=executor,
    )
    reply = brain.on_user_turn(user_turn(1, "text me the code"))
    output = await anext(reply)
    while not isinstance(output, SpeakStatic):  # the round's (empty) text, read like the pipeline does
        assert isinstance(output, SpeakText)
        async for _token in output.tokens:
            pass
        output = await anext(reply)
    # the frozen clock plays the filler at once while the tool is held
    await reply.aclose()  # the barge-in stops the reply and its tool tasks
    return brain, executor, call


async def test_a_barge_in_leaves_the_system_tool_run_in_progress() -> None:
    sms = _HeldSms()

    brain, executor, call = await _barge_in(sms)

    assert sms.sent == []
    assert not sms.cancelled
    assert executor.detached(call)
    assert _tool_messages(brain) == [IN_PROGRESS]
    sms.gate.set()
    await executor.aclose()
    assert sms.sent == ["Your code is 42."]
    assert _tool_messages(brain) == [IN_PROGRESS]


async def test_a_barge_in_cancels_a_system_tool_that_does_not_detach() -> None:
    sms = _HeldSms(detaches=False)

    brain, executor, call = await _barge_in(sms)

    assert sms.cancelled
    assert not executor.detached(call)
    assert _tool_messages(brain) == [INTERRUPTED]
    sms.gate.set()
    await executor.aclose()
    assert sms.sent == []
    assert _tool_messages(brain) == [INTERRUPTED]
