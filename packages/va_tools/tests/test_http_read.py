"""Where the shared HTTP executor parses and searches an answer (specs/13 §9 Decision P08-FIX-25): on the
event loop when its paths are cheap for its size, else on the process's reader thread, with one outcome,
and for no longer than the request's ``timeout_ms``.

The executors here run on a manual clock: the read's deadline passes only when a test moves it."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from typing import Any

import httpx
import orjson
import pytest
import respx
from service_helpers import json_log_stream, log_events
from tools_helpers import API, call, held_reader, manual_executor, read_waits, thread_recorder

import va_tools.http
from va_common.logging import bound_context
from va_tools.http import MAX_RESPONSE_BYTES
from va_tools.jsonpath import INLINE_SCAN_BYTES
from va_tools.offload import READER_THREAD

DESCENDANT = (("x", "$..x"),)
COUNTING = (("tiers", "$.data.items[*].tier"),)
DIRECT = (("plan", "$.data.plan"),)
SLICE = ("some", "$.data.items[0:1].tier")
FILTER = ("xs", "$.data.items[?(@.x > 1)].x")
"""Weighs 3: the path, and for its one expression 1 and the step of ``@.x``."""
FIRST = ("first", "$.data.items[0].tier")


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


@dataclass(slots=True)
class _Threads:
    """The threads the executor's parse and search ran on."""

    parsed: list[str]
    searched: list[str]

    def on_the_loop(self) -> bool:
        return all(name == threading.current_thread().name for name in self.parsed + self.searched)

    def on_the_reader(self) -> bool:
        return all(name.startswith(READER_THREAD) for name in self.parsed + self.searched)


@pytest.fixture
def threads(monkeypatch: pytest.MonkeyPatch) -> _Threads:
    return _Threads(
        parsed=thread_recorder(monkeypatch, va_tools.http, "_parsed"),
        searched=thread_recorder(monkeypatch, va_tools.http, "extract_all"),
    )


def _sized(size: int) -> bytes:
    """A JSON answer of exactly ``size`` bytes."""
    answer: dict[str, Any] = {
        "data": {"plan": "gold", "items": [{"tier": "a", "x": 1}, {"tier": "b", "x": 2}]},
        "pad": "",
    }
    answer["pad"] = "p" * (size - len(orjson.dumps(answer)))
    content = orjson.dumps(answer)
    assert len(content) == size
    return content


class _Chunks(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            yield chunk


@dataclass(frozen=True, slots=True)
class _Case:
    """An answer and what the executor makes of it: ``(kind, status, body, truncated, extracted)``;
    ``reads`` lists what the read runs (``parse``, ``search``)."""

    answer: Callable[[], httpx.Response]
    extract: tuple[tuple[str, str], ...]
    expected: tuple[str, int, Any, bool, dict[str, Any]]
    reads: tuple[str, ...] = ("parse", "search")
    max_bytes: int | None = None


CASES = {
    "ok": _Case(
        lambda: httpx.Response(200, json={"account": {"status": "active"}, "items": [1, 2]}),
        (("status", "$.account.status"), ("items", "$.items[*]"), ("none", "$.nope")),
        (
            "ok",
            200,
            {"account": {"status": "active"}, "items": [1, 2]},
            False,
            {"status": "active", "items": [1, 2], "none": None},
        ),
    ),
    "text": _Case(
        lambda: httpx.Response(200, text="gone"),
        (("x", "$.x"),),
        ("ok", 200, "gone", False, {"x": None}),
        reads=("parse",),
    ),
    "truncated": _Case(
        lambda: httpx.Response(200, stream=_Chunks(b"a" * 6, b"b" * 6, b"c" * 6)),
        (("x", "$.x"),),
        ("ok", 200, "aaaaaabbbb", True, {"x": None}),
        reads=("parse",),
        max_bytes=10,
    ),
    "miss": _Case(
        lambda: httpx.Response(200, json={"a": 1}),
        (("x", "$.nope"), ("y", "$.a.b")),
        ("ok", 200, {"a": 1}, False, {"x": None, "y": None}),
    ),
    "empty": _Case(
        lambda: httpx.Response(204),
        (("x", "$.x"),),
        ("ok", 204, None, False, {"x": None}),
        reads=(),
    ),
    "json-null": _Case(
        lambda: httpx.Response(200, content=b"null"),
        (("x", "$.x"),),
        ("ok", 200, None, False, {"x": None}),
        reads=("parse",),
    ),
    "error-body": _Case(
        lambda: httpx.Response(500, json={"error": {"code": "E1"}}),
        (("code", "$.error.code"),),
        ("http_error", 500, {"error": {"code": "E1"}}, False, {"code": "E1"}),
    ),
    "no-paths": _Case(
        lambda: httpx.Response(200, json={"a": [1, 2]}),
        (),
        ("ok", 200, {"a": [1, 2]}, False, {}),
    ),
}


@pytest.mark.parametrize("inline", [True, False], ids=["on-the-loop", "on-the-reader"])
@pytest.mark.parametrize("name", sorted(CASES))
async def test_the_read_gives_the_same_outcome_on_the_loop_and_on_the_reader(
    api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, threads: _Threads, name: str, *, inline: bool
) -> None:
    case = CASES[name]
    monkeypatch.setattr(va_tools.http, "inline_read", lambda _size, _paths: inline)
    stream = json_log_stream()
    api.post("/lookup").mock(return_value=case.answer())

    async with manual_executor(max_bytes=case.max_bytes) as (executor, _clock):
        outcome = await executor.execute(call(extract=case.extract))

    assert (outcome.kind, outcome.status, outcome.body, outcome.truncated, outcome.extracted) == case.expected
    assert (len(threads.parsed), len(threads.searched)) == ("parse" in case.reads, "search" in case.reads)
    assert threads.on_the_loop() if inline else threads.on_the_reader()
    (line,) = [event for event in log_events(stream) if event["event"] == "tools.http_request"]
    assert line["offloaded"] is (bool(case.reads) and not inline)


# The rule itself (no patch).


async def test_a_descendant_path_is_read_on_the_reader_whatever_the_size(
    api: respx.MockRouter, threads: _Threads
) -> None:
    api.post("/lookup").respond(200, json={"a": {"x": 1}, "b": [{"x": 2}]})
    async with manual_executor() as (executor, _clock):
        outcome = await executor.execute(call(extract=DESCENDANT))

    assert outcome.extracted == {"x": [1, 2]}
    assert (len(threads.parsed), len(threads.searched)) == (1, 1)
    assert threads.on_the_reader()


@pytest.mark.parametrize(
    ("size", "paths", "inline"),
    [
        (INLINE_SCAN_BYTES, COUNTING, True),
        (INLINE_SCAN_BYTES + 1, COUNTING, False),
        (INLINE_SCAN_BYTES // 2, (*COUNTING, SLICE), True),
        (INLINE_SCAN_BYTES // 2 + 1, (*COUNTING, SLICE), False),
        (INLINE_SCAN_BYTES // 4, (*COUNTING, FILTER), True),
        (INLINE_SCAN_BYTES // 4 + 1, (*COUNTING, FILTER), False),
        (INLINE_SCAN_BYTES + 1, (*COUNTING, *DESCENDANT), False),
        (INLINE_SCAN_BYTES, (*DIRECT, *COUNTING, *DIRECT), True),
    ],
    ids=[
        "one-at",
        "one-above",
        "two-at",
        "two-above",
        "filter-at",
        "filter-above",
        "with-descendant",
        "direct-do-not-count",
    ],
)
async def test_a_counting_path_is_read_on_the_loop_up_to_the_threshold(
    api: respx.MockRouter, threads: _Threads, size: int, paths: tuple[tuple[str, str], ...], *, inline: bool
) -> None:
    api.post("/lookup").respond(200, content=_sized(size))
    async with manual_executor() as (executor, _clock):
        outcome = await executor.execute(call(extract=paths))

    assert outcome.extracted["tiers"] == ["a", "b"]
    assert (len(threads.parsed), len(threads.searched)) == (1, 1)
    assert threads.on_the_loop() if inline else threads.on_the_reader()


MULTIPLYING = [
    ("$.data.items[*].$.data.plan", ["gold", "gold"]),
    ("$.data.items[*].`parent`[0].tier", ["a", "a"]),
    ("$.data['plan','plan']", ["gold", "gold"]),
    ("$.data.items[?(@.tier =~ 'a')].x", 1),
    ("$.data.items.`len`", 2),
]


@pytest.mark.parametrize(("path", "value"), MULTIPLYING, ids=["root", "parent", "names", "regex", "len"])
async def test_a_path_outside_the_descending_class_is_read_on_the_reader_whatever_the_size(
    api: respx.MockRouter, threads: _Threads, path: str, value: Any
) -> None:
    """Review finding: root re-entry, ``parent`` and multi-name steps multiply the matches (seconds on
    answers under 2 KiB), so no size keeps them on the loop."""
    api.post("/lookup").respond(200, content=_sized(96))
    async with manual_executor() as (executor, clock):
        outcome = await executor.execute(call(extract=(("value", path),)))

    assert outcome.extracted == {"value": value}
    assert (len(threads.parsed), len(threads.searched)) == (1, 1)
    assert threads.on_the_reader()
    assert clock.sleepers == 0


async def test_direct_paths_stay_on_the_loop_at_the_size_cap(
    api: respx.MockRouter, threads: _Threads
) -> None:
    api.post("/lookup").respond(200, content=_sized(MAX_RESPONSE_BYTES))
    async with manual_executor() as (executor, _clock):
        outcome = await executor.execute(call(extract=(*DIRECT, FIRST)))

    assert (outcome.truncated, outcome.extracted) == (False, {"plan": "gold", "first": "a"})
    assert (len(threads.parsed), len(threads.searched)) == (1, 1)
    assert threads.on_the_loop()


async def test_an_answer_without_paths_stays_on_the_loop(api: respx.MockRouter, threads: _Threads) -> None:
    api.post("/lookup").respond(200, content=_sized(MAX_RESPONSE_BYTES))
    async with manual_executor() as (executor, _clock):
        outcome = await executor.execute(call())

    assert isinstance(outcome.body, dict)
    assert (len(threads.parsed), len(threads.searched), outcome.extracted) == (1, 1, {})
    assert threads.on_the_loop()


# Cancellation and logs.


async def test_a_request_cancelled_while_its_read_is_held_does_not_wait(
    api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, threads: _Threads
) -> None:
    """The cancelled request returns while the reader is still busy, and its read, still queued, never
    runs."""
    api.post("/lookup").respond(200, json={"a": {"x": 1}})
    entered = read_waits(monkeypatch, va_tools.http)
    async with manual_executor() as (executor, _clock):
        async with held_reader() as gate:
            running = asyncio.create_task(executor.execute(call(extract=DESCENDANT)))
            async with asyncio.timeout(5):
                await entered.wait()
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
            assert not gate.is_set()
        outcome = await executor.execute(call(extract=DESCENDANT))

    assert outcome.extracted == {"x": 1}
    assert (len(threads.parsed), len(threads.searched)) == (1, 1)
    assert threads.on_the_reader()


async def test_a_read_still_held_at_the_request_s_timeout_is_a_timeout_and_never_runs(
    api: respx.MockRouter, monkeypatch: pytest.MonkeyPatch, threads: _Threads
) -> None:
    """Review finding: a tool or Webhook node read queued behind another job waits no longer than the
    request's ``timeout_ms``; the answered request is not sent again, and the queued read never runs."""
    route = api.post("/lookup").respond(200, json={"a": {"x": 1}})
    entered = read_waits(monkeypatch, va_tools.http)
    stream = json_log_stream()
    async with manual_executor() as (executor, clock):
        async with held_reader() as gate:
            running = asyncio.create_task(
                executor.execute(call(extract=DESCENDANT, timeout_ms=3_000, retries=2))
            )
            async with asyncio.timeout(5):
                await entered.wait()
            await clock.wait_for_sleep(3.0)
            await clock.run_for(2.5)
            assert not running.done()
            await clock.run_for(0.5)
            outcome = await running
            assert not gate.is_set()
        marker = await executor.execute(call(extract=DESCENDANT))

    assert (outcome.kind, outcome.status, outcome.body, outcome.truncated) == ("timeout", None, None, False)
    assert (outcome.extracted, outcome.attempts, outcome.duration_ms) == ({"x": None}, 1, 3_000)
    assert route.call_count == 2
    assert marker.extracted == {"x": 1}
    assert (len(threads.parsed), len(threads.searched)) == (1, 1)
    first, _ = [event for event in log_events(stream) if event["event"] == "tools.http_request"]
    assert (first["outcome"], first["status"], first["offloaded"]) == ("timeout", None, True)


async def test_a_read_that_finishes_in_time_leaves_no_deadline_behind(
    api: respx.MockRouter, threads: _Threads
) -> None:
    api.post("/lookup").respond(200, json={"a": {"x": 1}})
    async with manual_executor() as (executor, clock):
        outcome = await executor.execute(call(extract=DESCENDANT, timeout_ms=3_000))
        await clock.run_for(3.0)

    assert (outcome.kind, outcome.extracted, clock.sleepers) == ("ok", {"x": 1}, 0)
    assert threads.on_the_reader()


@pytest.mark.parametrize(("paths", "offloaded"), [(DIRECT, False), (DESCENDANT, True)])
async def test_the_log_says_whether_the_read_left_the_loop(
    api: respx.MockRouter, paths: tuple[tuple[str, str], ...], *, offloaded: bool
) -> None:
    api.post("/lookup").respond(200, json={"data": {"plan": "gold"}})
    stream = json_log_stream()
    async with manual_executor() as (executor, _clock):
        await executor.execute(call(extract=paths))

    (line,) = [event for event in log_events(stream) if event["event"] == "tools.http_request"]
    assert line["offloaded"] is offloaded


async def test_a_refused_request_reads_nothing(threads: _Threads) -> None:
    stream = json_log_stream()
    async with manual_executor() as (executor, _clock):
        outcome = await executor.execute(call(base="ftp://files.acme.test", extract=DESCENDANT))

    assert (outcome.kind, outcome.body, outcome.extracted) == ("blocked", None, {"x": None})
    assert (threads.parsed, threads.searched) == ([], [])
    (line,) = [event for event in log_events(stream) if event["event"] == "tools.http_request"]
    assert line["offloaded"] is False


async def test_a_log_line_from_the_reader_keeps_the_call_context(api: respx.MockRouter) -> None:
    api.post("/lookup").respond(200, json={"a": 1})
    stream = json_log_stream()
    async with manual_executor() as (executor, _clock):
        with bound_context(call_id="call-7"):
            outcome = await executor.execute(call(extract=(("x", "$..?("),)))

    assert outcome.extracted == {"x": None}
    (line,) = [event for event in log_events(stream) if event["event"] == "tools.jsonpath_invalid"]
    assert line["call_id"] == "call-7"
