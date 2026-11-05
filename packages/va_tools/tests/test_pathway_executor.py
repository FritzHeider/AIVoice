"""The per-node tool executor of pathway calls (specs/13 §4; Decisions P08-FIX-17 and P08-T02c2) over
respx."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import respx
from graph_helpers import graph, node, start
from service_helpers import json_log_stream, log_events
from tools_helpers import API, GatedSystem, RecordedEvents, plain_executor

from va_engine.templating import TemplateScope
from va_llm.models import ToolCall
from va_tools.executor import SystemOutcome
from va_tools.models import ToolSpec
from va_tools.pathway_executor import PathwayCallExecutor, graph_tool_refs, tool_ref_id
from va_tools.runner import ToolRunner

SCOPE = TemplateScope()
STORED_ID = uuid.UUID("0192f3a0-0000-7000-8000-00000000000a")
OTHER_ID = uuid.UUID("0192f3a0-0000-7000-8000-00000000000b")


def stored(tool_id: uuid.UUID, name: str = "lookup", *, path: str = "/stored", **fields: Any) -> ToolSpec:
    values: dict[str, Any] = {
        "tool_id": f"TL-{tool_id}",
        "name": name,
        "description": f"Stored {name}.",
        "speech": None,
        "url": f"{API}{path}",
        "method": "POST",
    }
    return ToolSpec(**{**values, **fields})


def inline(name: str = "lookup", *, path: str = "/inline", **fields: Any) -> dict[str, Any]:
    return {"name": name, "description": f"Inline {name}.", "url": f"{API}{path}", **fields}


def lookup_call(call_id: str = "c1") -> ToolCall:
    return ToolCall(id=call_id, name="lookup", arguments_json="{}")


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        (f"TL-{STORED_ID}", STORED_ID),
        (str(STORED_ID), STORED_ID),
        (str(STORED_ID).upper(), STORED_ID),
        (f"TL-{str(STORED_ID).upper()}", STORED_ID),
        (f"KB-{STORED_ID}", None),
        (f"tl-{STORED_ID}", None),
        (f" {STORED_ID}", None),
        ("TL-1", None),
        ("lookup", None),
        ("", None),
    ],
    ids=[
        "public",
        "bare",
        "bare-upper",
        "public-upper",
        "other-prefix",
        "lower-prefix",
        "padded",
        "short",
        "name",
        "empty",
    ],
)
def test_a_string_ref_names_a_stored_tool_by_its_public_or_bare_id(
    ref: str, expected: uuid.UUID | None
) -> None:
    assert tool_ref_id(ref) == expected


def test_a_graphs_refs_are_its_nodes_tools_then_the_global_tools() -> None:
    first, second = inline("a"), inline("b")
    pathway = graph(
        [start("s", prompt="Help.", tools=["TL-1", first]), node("n", prompt="More.", tools=[second])],
        [],
        {"globalTools": [str(STORED_ID)]},
    )
    assert graph_tool_refs(pathway) == ["TL-1", first, second, str(STORED_ID)]
    assert graph_tool_refs(graph([start("s", text="Hi.")], [])) == []


async def test_two_tools_of_one_name_on_different_nodes_run_their_own_definition(
    api: respx.MockRouter,
) -> None:
    stored_route = api.post("/stored").respond(200, json={"from": "stored"})
    inline_route = api.post("/inline").respond(200, json={"from": "inline"})
    events = RecordedEvents()
    async with plain_executor() as (http, clock):
        tools = PathwayCallExecutor(runner=ToolRunner(http, clock=clock), events=events)
        tools.add([stored(STORED_ID, speech="Checking A.")])
        node_a = tools.for_refs([f"TL-{STORED_ID}"])
        node_b = tools.for_refs([inline(speech="Checking B.")])

        assert [d.description for d in node_a.definitions()] == ["Stored lookup."]
        assert [d.description for d in node_b.definitions()] == ["Inline lookup."]
        assert (node_a.speech("lookup"), node_b.speech("lookup")) == ("Checking A.", "Checking B.")
        answer_a = await node_a.execute(lookup_call("c1"), SCOPE)
        answer_b = await node_b.execute(lookup_call("c2"), SCOPE)

    assert json.loads(answer_a.content)["data"] == {"from": "stored"}
    assert json.loads(answer_b.content)["data"] == {"from": "inline"}
    assert (stored_route.call_count, inline_route.call_count) == (1, 1)
    assert [data["tool_id"] for data in events.tools] == [f"TL-{STORED_ID}", None]


async def test_a_node_executor_handles_only_what_its_refs_name() -> None:
    sms = GatedSystem("send_sms", SystemOutcome('{"status":"sent"}'))
    async with plain_executor() as (http, clock):
        tools = PathwayCallExecutor(runner=ToolRunner(http, clock=clock), system=[sms])
        tools.add([stored(STORED_ID, speech="One moment.")])
        bare = tools.for_refs([])

    assert bare.definitions() == ()
    assert not bare.handles("lookup")
    assert bare.speech("lookup") is None
    assert bare.tool_speech is None
    assert bare.handles("send_sms")
    assert tools.handles("send_sms")
    assert not tools.handles("lookup")
    assert not tools.handles("schedule_callback")


async def test_refs_resolve_in_order_and_what_resolves_to_nothing_is_skipped() -> None:
    stream = json_log_stream()
    broken = {"name": "broken", "description": "No url."}
    refs: list[Any] = [
        str(STORED_ID),
        f"TL-{OTHER_ID}",
        "not-a-tool-id",
        broken,
        inline("end_call"),
        inline("track", path="/track"),
        inline("lookup", path="/shadowed"),
        f"TL-{STORED_ID}",
    ]
    async with plain_executor() as (http, clock):
        tools = PathwayCallExecutor(runner=ToolRunner(http, clock=clock))
        tools.add([stored(STORED_ID)])
        first = tools.for_refs(refs)
        second = tools.for_refs(refs)

    assert (tools.knows(STORED_ID), tools.knows(OTHER_ID)) == (True, False)
    for scoped in (first, second):
        assert [(d.name, d.description) for d in scoped.definitions()] == [
            ("lookup", "Stored lookup."),
            ("track", "Inline track."),
        ]
        assert not scoped.handles("end_call")
        assert not scoped.handles("broken")
    logged = log_events(stream)
    [invalid] = [e for e in logged if e["event"] == "tools.inline_tool_invalid"]
    assert invalid["errors"] == 1
    skipped = [(e["tool"], e["reason"]) for e in logged if e["event"] == "tools.tool_skipped"]
    # The invalid and the reserved inline tool are reported once per call; the shadowed definition once per
    # node executor; the stored tool listed twice is not reported at all.
    assert skipped == [("end_call", "reserved"), ("lookup", "duplicate"), ("lookup", "duplicate")]
    assert "No url." not in stream.getvalue()


async def test_an_inline_tool_that_cannot_be_written_as_json_resolves_to_nothing() -> None:
    stream = json_log_stream()
    huge = inline("huge", body={"n": 2**70})
    also_huge = inline("larger", body={"n": 2**80})
    pathway = graph(
        [start("a", prompt="Help.", tools=[huge, also_huge, inline("track", speech="Tracking.")])], []
    )
    refs = graph_tool_refs(pathway)
    async with plain_executor() as (http, clock):
        tools = PathwayCallExecutor(runner=ToolRunner(http, clock=clock))
        tools.add([], refs)
        scoped = [tools.for_refs(refs), tools.for_refs(refs)]

    assert [[d.name for d in executor.definitions()] for executor in scoped] == [["track"], ["track"]]
    assert tools.tool_speech == "Tracking."
    # Such refs have no cache key: they are reported once per call, however often they are resolved.
    invalid = [e["errors"] for e in log_events(stream) if e["event"] == "tools.inline_tool_invalid"]
    assert invalid == [1]
    assert str(2**70) not in stream.getvalue()


async def test_node_executors_share_the_system_runs_and_one_drain() -> None:
    sms = GatedSystem("send_sms", SystemOutcome('{"status":"sent"}'))
    events = RecordedEvents()
    call = ToolCall(id="c1", name="send_sms", arguments_json="{}")
    async with plain_executor() as (http, clock):
        tools = PathwayCallExecutor(runner=ToolRunner(http, clock=clock), events=events, system=[sms])
        node_a, node_b = tools.for_refs([]), tools.for_refs([inline()])
    waiting = asyncio.create_task(node_a.execute(call, SCOPE))
    await sms.started.wait()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert node_b.detached(call)
    assert not node_b.detached(ToolCall(id="c1", name="send_sms", arguments_json="{}"))
    draining = asyncio.create_task(tools.aclose())
    await asyncio.sleep(0)
    assert not draining.done()
    sms.gate.set()
    await draining

    assert (sms.finished, sms.cancelled) == (["c1"], False)
    await tools.aclose(timeout_s=0)
    assert sms.finished == ["c1"]


async def test_a_failed_system_run_is_reported_once_through_the_calls_events() -> None:
    callback = GatedSystem(
        "schedule_callback", SystemOutcome('{"error":"callback_failed"}', "workflow_start")
    )
    callback.gate.set()
    events = RecordedEvents()
    async with plain_executor() as (http, clock):
        tools = PathwayCallExecutor(runner=ToolRunner(http, clock=clock), events=events, system=[callback])
        answer = await tools.for_refs([]).execute(
            ToolCall(id="c1", name="schedule_callback", arguments_json="{}"), SCOPE
        )

    assert answer.content == '{"error":"callback_failed"}'
    assert (events.tools, events.failures) == ([], ["schedule_callback: workflow_start"])


async def test_the_tool_speech_is_the_first_speech_among_the_refs() -> None:
    silent, spoken = stored(STORED_ID), stored(OTHER_ID, "track", speech="Tracking it.")
    speeches: list[str | None] = []
    async with plain_executor() as (http, clock):
        tools = PathwayCallExecutor(runner=ToolRunner(http, clock=clock))
        speeches.append(tools.tool_speech)
        tools.add([silent, spoken], [inline("end_call", speech="Reserved."), f"TL-{STORED_ID}"])
        speeches.append(tools.tool_speech)
        tools.add(
            [],
            [inline(speech="  "), "TL-unknown", inline("hours", speech="Checking hours."), f"TL-{OTHER_ID}"],
        )
        speeches.append(tools.tool_speech)
        tools.add([], [f"TL-{OTHER_ID}"])
        speeches.append(tools.tool_speech)

    assert speeches == [None, None, "Checking hours.", "Checking hours."]


async def test_a_stored_tool_added_twice_keeps_its_first_definition() -> None:
    async with plain_executor() as (http, clock):
        tools = PathwayCallExecutor(runner=ToolRunner(http, clock=clock))
        tools.add(
            [
                stored(STORED_ID),
                ToolSpec(tool_id=None, name="x", description="d", speech=None, url=API, method="GET"),
            ]
        )
        tools.add([stored(STORED_ID, description="Changed.")])
        [definition] = tools.for_refs([str(STORED_ID)]).definitions()

    assert definition.description == "Stored lookup."
