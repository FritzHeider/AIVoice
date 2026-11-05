"""A barge-in during a system tool run (Decision P08-FIX-13 under specs/13 §6): the TaskBrain stops waiting,
the run itself completes once, and the executor's ``aclose`` waits for it."""

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
from va_engine.task_tools import INTERRUPTED
from va_llm.models import ToolCall
from va_tools.executor import CallToolExecutor, SystemOutcome
from va_tools.runner import ToolRunner
from va_voice.pipeline import SpeakStatic, SpeakText


@dataclass(slots=True)
class _HeldSms:
    """A ``send_sms`` tool whose run waits for ``gate`` and records the messages it sent."""

    name: str = "send_sms"
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    sent: list[str] = field(default_factory=list)

    async def run(self, call: ToolCall) -> SystemOutcome:
        await self.gate.wait()
        self.sent.append(orjson.loads(call.arguments_json)["message"])
        return SystemOutcome('{"status":"sent","segments":1}')


async def test_a_barge_in_does_not_cancel_the_system_tool_run() -> None:
    clock = FrozenClock()
    sms = _HeldSms()
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

    assert sms.sent == []
    sms.gate.set()
    await executor.aclose()
    assert sms.sent == ["Your code is 42."]
    assert [m.content for m in brain.messages if m.role == "tool"] == [INTERRUPTED]
