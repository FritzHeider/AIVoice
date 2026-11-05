"""A task-mode brain running real tool calls through :class:`CallToolExecutor` (P08-T02b1).

The tool sends real HTTP to a socket receiver; the dialogue is a scripted source, so the test reads the
``tool`` message the follow-up request carries.
"""

from __future__ import annotations

import json
from typing import Any

import orjson
from dialogue_helpers import FakeSource, counter
from task_helpers import drain, task_context, user_turn
from tools_helpers import plain_executor
from webhook_receiver import Receiver, Reply, served_receiver

from va_common.time import FrozenClock
from va_domain.org import OrgSettings
from va_engine.system_tools import facts_for_call
from va_engine.task_runner import TaskBrain
from va_llm.models import ToolCall
from va_tools.executor import CallToolExecutor
from va_tools.models import ToolSpec
from va_tools.runner import ToolRunner


def _spec(port: int, **fields: Any) -> ToolSpec:
    values: dict[str, Any] = {
        "tool_id": None,
        "name": "lookup_order",
        "description": "Looks up an order.",
        "speech": None,
        "url": f"http://127.0.0.1:{port}/orders",
        "method": "POST",
        "body": {"order": "{{input.order}}", "customer": "{{name}}"},
        "input_schema": {"type": "object", "properties": {"order": {"type": "string"}}},
        "response_map": {"status": "$.status"},
    }
    return ToolSpec(**{**values, **fields})


async def _turn(spec: ToolSpec, source: FakeSource) -> tuple[TaskBrain, list[Any]]:
    async with plain_executor(allow_http=True) as (http, clock):
        executor = CallToolExecutor([spec], runner=ToolRunner(http, clock=clock))
        context = task_context(first_sentence="Hi.", variables={"name": "Ada"})
        settings = OrgSettings.model_validate({"engine": {"tool_fillers": False}})
        brain = TaskBrain(
            context=context,
            settings=settings,
            gateway=source,
            counter=counter(),
            facts=facts_for_call(context, settings, sms_capable=False),
            clock=FrozenClock(),
            executor=executor,
        )
        await drain(brain.start())
        outputs = await drain(brain.on_user_turn(user_turn(1, "where is order A7")))
    return brain, outputs


def _call() -> ToolCall:
    return ToolCall(id="call_1", name="lookup_order", arguments_json=orjson.dumps({"order": "A7"}).decode())


async def test_a_tool_timeout_answer_reaches_the_model() -> None:
    receiver = Receiver(Reply(hold=True))
    source = FakeSource().reply(calls=[_call()]).reply("Sorry, the order system is slow.")
    async with served_receiver(receiver) as port:
        _, outputs = await _turn(_spec(port, timeout_ms=500), source)
    assert outputs == [("text", "Sorry, the order system is slow.")]
    [answer] = [m for m in source.requests[-1].messages if m.role == "tool"]
    assert json.loads(answer.content or "") == {"status": 0, "error": "timeout"}


async def test_a_tool_answer_and_its_extraction_reach_the_model_and_state() -> None:
    receiver = Receiver(Reply(body=b'{"status": "shipped"}', headers={"content-type": "application/json"}))
    source = FakeSource().reply(calls=[_call()]).reply("It shipped.")
    async with served_receiver(receiver) as port:
        brain, outputs = await _turn(_spec(port), source)
    assert outputs == [("text", "It shipped.")]
    assert json.loads(receiver.requests[0].body) == {"order": "A7", "customer": "Ada"}
    [answer] = [m for m in source.requests[-1].messages if m.role == "tool"]
    assert json.loads(answer.content or "") == {
        "status": 200,
        "data": {"status": "shipped"},
        "extracted": {"status": "shipped"},
    }
    assert brain.state.variables["status"] == "shipped"
