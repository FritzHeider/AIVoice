"""FakeLLM fault injection: control-route faults and rule errors; timeouts and disconnects on real sockets."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import orjson
import pytest
from llm_helpers import (
    API_KEY,
    add_rules,
    chat_body,
    deltas,
    llm_client,
    llm_state,
    realtime_llm_state,
    served_llm,
    sse_events,
    wait_held,
)

from va_sim.state import SimState

AUTH = {"authorization": f"Bearer {API_KEY}"}
TOOLS: list[dict[str, Any]] = [{"type": "function", "function": {"name": "book"}}]


async def arm(client: httpx.AsyncClient, **fault: object) -> None:
    response = await client.put("/sim/llm/fault", json=fault)
    assert response.status_code == 204, response.text


async def test_rate_limit_with_retry_after_counts_down() -> None:
    state = llm_state()
    async with llm_client(state) as client:
        await arm(client, kind="rate_limit", times=2, retry_after_s=3)
        first = await client.post("/v1/chat/completions", json=chat_body("hi"))
        second = await client.post("/v1/chat/completions", json=chat_body("hi"))
        third = await client.post("/v1/chat/completions", json=chat_body("hi"))
    assert (first.status_code, second.status_code, third.status_code) == (429, 429, 200)
    assert first.headers["retry-after"] == "3"
    assert first.json()["error"]["type"] == "rate_limit_error"
    assert first.json()["error"]["code"] == "rate_limit_exceeded"
    assert [record.fault for record in state.llm.requests] == ["rate_limit", "rate_limit", None]


async def test_rate_limit_default_retry_after_and_server_errors() -> None:
    async with llm_client(llm_state()) as client:
        await arm(client, kind="rate_limit")
        assert (await client.post("/v1/chat/completions", json=chat_body("hi"))).headers["retry-after"] == "1"
        await arm(client, kind="server_error", status=503)
        unavailable = await client.post("/v1/chat/completions", json=chat_body("hi"))
        await arm(client, kind="server_error")
        default = await client.post("/v1/chat/completions", json=chat_body("hi"))
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["type"] == "server_error"
    assert default.status_code == 500


async def test_faulted_requests_do_not_advance_call_index() -> None:
    async with llm_client(llm_state()) as client:
        await add_rules(
            client, 'rules:\n  - id: first\n    match: {call_index: 0}\n    respond: {content: "One."}\n'
        )
        await arm(client, kind="rate_limit")
        assert (await client.post("/v1/chat/completions", json=chat_body("hi"))).status_code == 429
        retried = await client.post("/v1/chat/completions", json=chat_body("hi"))
    assert retried.json()["choices"][0]["message"]["content"] == "One."


async def test_malformed_tool_args_waits_for_a_tool_call_reply() -> None:
    state = llm_state()
    async with llm_client(state) as client:
        await add_rules(
            client,
            "rules:\n  - id: t\n    match: {tool_name: book}\n"
            "    respond:\n      tool_calls: [{name: book, arguments: {party: 4}}]\n",
        )
        await arm(client, kind="malformed_tool_args")
        text = await client.post("/v1/chat/completions", json=chat_body("hi"))
        broken = await client.post("/v1/chat/completions", json=chat_body("hi", tools=TOOLS, stream=True))
        fine = await client.post("/v1/chat/completions", json=chat_body("hi", tools=TOOLS))
    assert text.json()["choices"][0]["message"]["content"] == "Okay. You said: hi."
    arguments = "".join(
        part["function"]["arguments"]
        for delta in deltas(sse_events(broken.text))
        for part in delta.get("tool_calls", [])
    )
    assert arguments == '{"par'
    with pytest.raises(orjson.JSONDecodeError):
        orjson.loads(arguments)
    assert orjson.loads(fine.json()["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]) == {
        "party": 4
    }
    assert [record.fault for record in state.llm.requests] == [None, "malformed_tool_args", None]


async def test_rule_errors() -> None:
    rules = """\
rules:
  - id: custom
    match: {last_user_regex: custom}
    respond:
      error: {status: 503, body: {error: {message: overloaded}}, headers: {retry-after: "7"}}
  - id: default-body
    match: {last_user_regex: default}
    respond: {error: {status: 429}, delay_ms: 30}
"""
    state = llm_state()
    async with llm_client(state) as client:
        await add_rules(client, rules)
        custom = await client.post("/v1/chat/completions", json=chat_body("custom"))
        start = state.clock.virtual_ms()
        default = await client.post("/v1/chat/completions", json=chat_body("default"))
        waited = state.clock.virtual_ms() - start
    assert custom.status_code == 503
    assert custom.json() == {"error": {"message": "overloaded"}}
    assert custom.headers["retry-after"] == "7"
    assert default.status_code == 429
    assert default.json()["error"] == {
        "message": "Injected error (rule default-body).",
        "type": "rate_limit_error",
        "param": None,
        "code": None,
    }
    assert waited == 30
    assert [(record.rule_id, record.status) for record in state.llm.requests] == [
        ("custom", 503),
        ("default-body", 429),
    ]


async def test_reset_releases_a_held_request() -> None:
    state = realtime_llm_state()
    async with llm_client(state) as client:
        await arm(client, kind="timeout")
        held = asyncio.create_task(client.post("/v1/chat/completions", json=chat_body("hi")))
        await wait_held(state, 1)
        assert state.snapshot()["llm"] == {"requests": 1, "rules": 0, "models": 4, "held": 1}
        assert (await client.post("/sim/reset")).status_code == 204
        response = await held
    assert response.status_code == 504
    assert response.json()["error"]["code"] == "timeout"
    assert state.llm.held == 0


async def test_timeout_before_headers_over_a_socket() -> None:
    state = realtime_llm_state()
    async with served_llm(state) as url:
        async with httpx.AsyncClient(base_url=url, headers=AUTH) as control:
            await arm(control, kind="timeout")
        async with httpx.AsyncClient(base_url=url, headers=AUTH, timeout=0.3) as client:
            with pytest.raises(httpx.ReadTimeout):
                await client.post("/v1/chat/completions", json=chat_body("hi"))
        # Closing the client disconnects; the simulator stops holding the request.
        await wait_held(state, 0)
        async with httpx.AsyncClient(base_url=url, headers=AUTH) as client:
            response = await client.post("/v1/chat/completions", json=chat_body("hi"))
    assert response.status_code == 200


async def collect_until_timeout(url: str, body: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    async with httpx.AsyncClient(base_url=url, headers=AUTH, timeout=0.3) as client:
        with pytest.raises(httpx.ReadTimeout):
            async with client.stream("POST", "/v1/chat/completions", json=body) as response:
                assert response.status_code == 200
                async for line in response.aiter_lines():
                    if line:
                        lines.append(line)
    return lines


async def test_timeout_after_tokens_stalls_the_stream() -> None:
    state = realtime_llm_state()
    async with served_llm(state) as url:
        async with httpx.AsyncClient(base_url=url, headers=AUTH) as control:
            await arm(control, kind="timeout", after_tokens=2)
        lines = await collect_until_timeout(url, chat_body("hello there", stream=True))
        await wait_held(state, 0)
    events = sse_events("".join(f"{line}\n\n" for line in lines))
    assert [delta.get("content") for delta in deltas(events)] == ["", "Okay. ", "You "]


async def test_timeout_after_tokens_on_a_non_streamed_request_holds_before_headers() -> None:
    state = realtime_llm_state()
    async with llm_client(state) as client:
        await arm(client, kind="timeout", after_tokens=1)
        held = asyncio.create_task(client.post("/v1/chat/completions", json=chat_body("hi")))
        await wait_held(state, 1)
        state.reset()
        assert (await held).status_code == 504


async def disconnected(state: SimState, body: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    async with served_llm(state) as url, httpx.AsyncClient(base_url=url, headers=AUTH) as client:
        await arm(client, kind="disconnect")
        with pytest.raises(httpx.RemoteProtocolError):
            async with client.stream("POST", "/v1/chat/completions", json=body) as response:
                async for line in response.aiter_lines():
                    if line:
                        lines.append(line)
        after = await client.post("/v1/chat/completions", json=chat_body("again"))
        assert after.status_code == 200
    return lines


async def test_disconnect_drops_a_stream_after_the_first_chunk() -> None:
    lines = await disconnected(realtime_llm_state(), chat_body("hi", stream=True))
    assert len(lines) == 1
    assert deltas(sse_events(f"{lines[0]}\n\n")) == [{"role": "assistant", "content": ""}]


async def test_disconnect_drops_a_non_streamed_response() -> None:
    assert await disconnected(realtime_llm_state(), chat_body("hi")) == []


@pytest.mark.parametrize(
    "fault",
    [
        {"kind": "rate_limit", "status": 500},
        {"kind": "server_error", "retry_after_s": 1},
        {"kind": "disconnect", "after_tokens": 1},
        {"kind": "server_error", "status": 504},
        {"kind": "timeout", "times": 0},
        {"kind": "explode"},
    ],
)
async def test_invalid_fault_bodies(fault: dict[str, object]) -> None:
    async with llm_client(llm_state()) as client:
        response = await client.put("/sim/llm/fault", json=fault)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


async def test_fault_route_reads_and_clears() -> None:
    async with llm_client(llm_state()) as client:
        assert (await client.get("/sim/llm/fault")).json() == {"fault": None}
        await arm(client, kind="timeout", after_tokens=3, times=2)
        assert (await client.get("/sim/llm/fault")).json() == {
            "fault": {
                "kind": "timeout",
                "remaining": 2,
                "status": None,
                "retry_after_s": None,
                "after_tokens": 3,
            }
        }
        assert (await client.delete("/sim/llm/fault")).status_code == 204
        assert (await client.get("/sim/llm/fault")).json() == {"fault": None}
