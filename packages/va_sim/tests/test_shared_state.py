"""Redis-shared control plane (specs/30 §3 Decision P03-T15c): two ``SimState`` objects on one fake Redis
stand for two sim processes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import pytest
from fakeredis import FakeAsyncRedis, FakeServer
from redis.exceptions import ConnectionError as RedisConnectionError
from service_helpers import json_log_stream, log_events
from sim_helpers import control_client, new_state, scenario, scenario_yaml
from telnyx_helpers import CALLEE, RecordingTransport, create_app, dial, telnyx_client, telnyx_state

from va_sim.audio.marker import MAX_UTTERANCE_ID
from va_sim.settings import PROFILES, SimName
from va_sim.shared import SharedState, SharedStateUnavailable, UtteranceLimitError
from va_sim.state import SimState
from va_sim.utterances import Utterance

YAML = {"content-type": "application/yaml"}


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
async def clients() -> AsyncIterator[list[FakeAsyncRedis]]:
    opened: list[FakeAsyncRedis] = []
    yield opened
    for client in opened:
        await client.aclose()


@pytest.fixture
def attach(server: FakeServer, clients: list[FakeAsyncRedis]) -> Any:
    async def attach(state: SimState | None = None) -> SimState:
        process = state if state is not None else telnyx_state(RecordingTransport())
        client = FakeAsyncRedis(server=server)
        clients.append(client)
        await process.attach_shared(SharedState(client))
        return process

    return attach


def control(state: SimState, sim: SimName = "telnyx") -> AbstractAsyncContextManager[httpx.AsyncClient]:
    return control_client(sim, state)


async def register(state: SimState, scenario_id: str, *, to: str = CALLEE) -> httpx.Response:
    async with control(state) as client:
        return await client.post(
            "/sim/telnyx/scenarios", content=scenario_yaml(scenario_id, to=to), headers=YAML
        )


# Scenarios.


async def test_scenario_registered_in_one_process_is_served_and_dialled_by_another(attach: Any) -> None:
    a, b = await attach(), await attach()
    assert (await register(a, "shared")).status_code == 201
    async with control(b) as client:
        listing = await client.get("/sim/telnyx/scenarios")
        detail = await client.get("/sim/telnyx/scenarios/shared")
    assert [item["id"] for item in listing.json()["scenarios"]] == ["shared"]
    assert detail.json()["match"]["to"] == CALLEE
    async with telnyx_client(b) as telnyx:
        app = await create_app(telnyx)
        call = await dial(telnyx, app["id"])
        found = b.telnyx.call(call["call_control_id"])
        assert found is not None
        assert found[1].scenario_id == "shared"


async def test_replacing_a_scenario_rebinds_its_number_everywhere(attach: Any) -> None:
    a, b = await attach(), await attach()
    await register(a, "moving", to="+14155550111")
    assert (await register(b, "moving", to="+14155550122")).status_code == 201
    await a.sync()
    assert a.scenario_for_destination("+14155550111") is None
    moved = a.scenario_for_destination("+14155550122")
    assert moved is not None
    assert moved.id == "moving"


async def test_number_bindings_follow_registration_order_after_a_reload(attach: Any) -> None:
    a = await attach()
    await a.add_scenario(scenario("first"))
    await a.add_scenario(scenario("second"))
    joined = await attach()
    bound = joined.scenario_for_destination(CALLEE)
    assert bound is not None
    assert bound.id == "second"
    await a.add_scenario(scenario("first"))
    await joined.sync()
    rebound = joined.scenario_for_destination(CALLEE)
    assert rebound is not None
    assert rebound.id == "first"


async def test_a_joining_process_adopts_the_shared_records_without_wiping_them(attach: Any) -> None:
    a = await attach()
    await a.add_scenario(scenario("kept"))
    await a.set_latencies(tts_ttfa_ms=333)
    joined = await attach()
    assert sorted(joined.scenarios) == ["kept"]
    assert joined.latencies.tts_ttfa_ms == 333
    await a.sync()
    assert sorted(a.scenarios) == ["kept"]


async def test_own_writes_do_not_force_a_reload(attach: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    a = await attach()
    await a.add_scenario(scenario("mine"))
    loads = 0
    assert a._shared is not None
    original = a._shared.load

    async def counting() -> Any:
        nonlocal loads
        loads += 1
        return await original()

    monkeypatch.setattr(a._shared, "load", counting)
    await a.sync()
    assert loads == 0
    await (await attach()).add_scenario(scenario("theirs"))
    await a.sync()
    assert loads == 1
    assert sorted(a.scenarios) == ["mine", "theirs"]


# Reset and seed.


async def test_reset_in_one_process_resets_every_process(attach: Any) -> None:
    a, b = await attach(), await attach()
    await a.add_scenario(scenario("gone"))
    await a.set_latencies(ttft_ms=999)
    await b.allocate_utterance("hello", "en", ("hello",))
    async with telnyx_client(b) as telnyx:
        await create_app(telnyx)
        assert b.snapshot()["telnyx"]["accounts"] == 1
        async with control(a) as client:
            assert (await client.post("/sim/reset", json={"seed": 7})).status_code == 204
        async with control(b) as client:
            snapshot = (await client.get("/sim/state")).json()
        assert (snapshot["seed"], snapshot["scenarios"], snapshot["utterances"]) == (7, [], 0)
        assert snapshot["latencies"]["ttft_ms"] == b._base_latencies.ttft_ms
        assert b.telnyx.accounts["sim-telnyx-key"].apps == {}
        assert b.utterances == {}
        async with control(b) as client:
            assert (await client.post("/sim/reset")).status_code == 204
        await a.sync()
        assert (a.seed, b.seed) == (7, 7)


async def test_reset_without_any_stored_seed_keeps_each_process_seed(attach: Any) -> None:
    a = await attach(new_state(seed=3))
    b = await attach(new_state(seed=5))
    await a.reset_all()
    await b.sync()
    assert (a.seed, b.seed) == (3, 5)


# Latencies.


async def test_latency_overrides_and_profiles_are_shared(attach: Any) -> None:
    a, b = await attach(), await attach()
    async with control(a, "elevenlabs") as client:
        patched = await client.patch("/sim/elevenlabs/config", json={"tts_ttfa_ms": 123})
    assert patched.json()["tts_ttfa_ms"] == 123
    async with control(b, "elevenlabs") as client:
        assert (await client.get("/sim/elevenlabs/config")).json()["tts_ttfa_ms"] == 123
    async with control(b, "llm") as client:
        profiled = await client.post("/sim/llm/config/profile", json={"profile": "realistic"})
    assert profiled.status_code == 200
    async with control(a, "llm") as client:
        current = (await client.get("/sim/llm/config")).json()
    realistic = PROFILES["realistic"]
    assert current == {"ttft_ms": realistic.ttft_ms, "tokens_per_s": realistic.tokens_per_s}
    assert a.latencies == realistic


# Utterances.


async def test_utterance_ids_are_unique_across_processes(attach: Any) -> None:
    a, b = await attach(), await attach()
    ids = [
        await a.allocate_utterance("one", "en", ("one",)),
        await b.allocate_utterance("two", "de", ("two",)),
        await a.allocate_utterance("three", "en", ("three",)),
    ]
    assert ids == [1, 2, 3]
    await a.sync()
    assert a.snapshot()["utterances"] == 3


async def test_a_worker_thread_resolves_another_process_utterance(attach: Any) -> None:
    a, b = await attach(), await attach()
    utterance_id = await a.allocate_utterance("good morning", "en", ("good", "morning"))
    assert b.resolve_utterance(utterance_id) is None  # never blocks the loop thread
    resolved = await asyncio.to_thread(b.resolve_utterance, utterance_id)
    assert resolved == Utterance("good morning", "en", ("good", "morning"))
    assert b.utterances[utterance_id] == resolved
    assert await asyncio.to_thread(b.resolve_utterance, 999) is None
    assert await b.known_utterances() == {utterance_id: resolved}


async def test_the_shared_utterance_id_space_is_bounded(attach: Any, server: FakeServer) -> None:
    a = await attach()
    raw = FakeAsyncRedis(server=server)
    await raw.set("sim:state:utterance_next", MAX_UTTERANCE_ID - 1)
    await raw.aclose()
    assert await a.allocate_utterance("last", "en", ("last",)) == MAX_UTTERANCE_ID
    with pytest.raises(UtteranceLimitError, match="all 65535 utterance ids"):
        await a.allocate_utterance("over", "en", ("over",))
    with pytest.raises(ValueError, match="reset the sim"):
        await a.allocate_utterance("over", "en", ("over",))
    await a.sync()
    assert a.snapshot()["utterances"] == MAX_UTTERANCE_ID


# Change notifications.


async def test_the_subscriber_applies_remote_writes_without_a_request(attach: Any) -> None:
    a, b = await attach(), await attach()
    subscriber = asyncio.create_task(b.run_subscriber())
    try:
        await b.wait_applied(b.applied_version or 0)
        await a.add_scenario(scenario("pushed"))
        assert a.applied_version is not None
        await asyncio.wait_for(b.wait_applied(a.applied_version), timeout=5)
        assert sorted(b.scenarios) == ["pushed"]
    finally:
        subscriber.cancel()
        with pytest.raises(asyncio.CancelledError):
            await subscriber


class _FlakyRedis(FakeAsyncRedis):
    """``pubsub()`` fails ``failures`` times before working."""

    failures = 1

    def pubsub(self, **kwargs: Any) -> Any:
        if self.failures:
            self.failures -= 1
            msg = "pubsub connection refused"
            raise RedisConnectionError(msg)
        return super().pubsub(**kwargs)


async def test_the_subscriber_reconnects_with_backoff_and_resyncs(server: FakeServer) -> None:
    redis = _FlakyRedis(server=server)
    redis.failures = 2
    sleeps: list[float] = []
    changes = asyncio.Event()

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async def on_change() -> None:
        changes.set()

    shared = SharedState(redis, sleep=sleep)
    stream = json_log_stream()
    task = asyncio.create_task(shared.subscribe(on_change))
    try:
        await asyncio.wait_for(changes.wait(), timeout=5)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await redis.aclose()
    assert sleeps == [1.0, 2.0]
    lost = [event for event in log_events(stream) if event["event"] == "sim.shared_state.subscriber_lost"]
    assert [event["retry_s"] for event in lost] == [1.0, 2.0]


# Redis failures.


async def test_redis_failures_answer_503_for_writes_and_keep_the_local_copy_for_reads(
    attach: Any, server: FakeServer
) -> None:
    a = await attach()
    await a.add_scenario(scenario("local"))
    stream = json_log_stream()
    server.connected = False
    try:
        registered = await register(a, "lost")
        async with control(a) as client:
            listing = await client.get("/sim/telnyx/scenarios")
            reset = await client.post("/sim/reset")
        async with control(a, "llm") as client:
            patched = await client.patch("/sim/llm/config", json={"ttft_ms": 1})
            profiled = await client.post("/sim/llm/config/profile", json={"profile": "fast"})
        with pytest.raises(SharedStateUnavailable):
            await a.allocate_utterance("x", "en", ("x",))
        assert await asyncio.to_thread(a.resolve_utterance, 42) is None
        assert await a.known_utterances() == {}
    finally:
        server.connected = True
    for response in (registered, reset, patched, profiled):
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "state_unavailable"
    assert [item["id"] for item in listing.json()["scenarios"]] == ["local"]
    events = [event["event"] for event in log_events(stream)]
    assert "sim.shared_state.sync_failed" in events
    assert "sim.shared_state.utterance_lookup_failed" in events


async def test_attaching_while_redis_is_down_starts_from_the_local_state(
    server: FakeServer, attach: Any
) -> None:
    server.connected = False
    state = await attach()
    assert state.applied_version is None
    server.connected = True
    await state.sync()
    assert state.applied_version == 0


# Unshared states behave as before.


async def test_unshared_state_keeps_local_semantics() -> None:
    state = new_state()
    await state.sync()
    await state.run_subscriber()
    assert not state.shared
    assert await state.allocate_utterance("a", "en", ("a",)) == 1
    assert state.register_utterance("b", "en", ("b",)) == 2
    assert state.resolve_utterance(1) == Utterance("a", "en", ("a",))
    assert await asyncio.to_thread(state.resolve_utterance, 3) is None
    await state.add_scenario(scenario("solo"))
    await state.set_latencies(ttft_ms=5)
    assert state.latencies.ttft_ms == 5
    await state.set_profile("fast")
    assert state.latencies == PROFILES["fast"]
    await state.reset_all(4)
    assert (state.seed, state.scenarios, state.utterances) == (4, {}, {})
