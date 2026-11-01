"""Redis-shared simulator control plane (specs/30-testing-and-evals.md §3 Decision P03-T15c).

With ``SIM_STATE_REDIS_URL`` set, every sim process using that Redis shares the registered scenarios, the
latency overrides, reset + seed, and the utterance registry. Each write is one Lua script (mutate, bump
``sim:state:version`` when the reloadable state changed, publish on ``sim:state:events``) so a reader never
sees half a write. Utterance allocations do not bump the version: utterances are resolved lazily by id.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Final

from redis.asyncio import Redis
from redis.exceptions import RedisError

from va_common import json
from va_common.logging import get_logger
from va_sim.audio.marker import MAX_UTTERANCE_ID
from va_sim.scenario import Scenario
from va_sim.settings import Latencies
from va_sim.utterances import Utterance

__all__ = [
    "CHANNEL",
    "SHARED_NAMESPACE",
    "SharedHead",
    "SharedSnapshot",
    "SharedState",
    "SharedStateUnavailable",
    "UtteranceLimitError",
    "utterance_limit_message",
]

SHARED_NAMESPACE: Final = "sim:state"
CHANNEL: Final = f"{SHARED_NAMESPACE}:events"
_VERSION: Final = f"{SHARED_NAMESPACE}:version"
_SEED: Final = f"{SHARED_NAMESPACE}:seed"
_GENERATION: Final = f"{SHARED_NAMESPACE}:generation"
_SCENARIOS: Final = f"{SHARED_NAMESPACE}:scenarios"
_SCENARIO_ORDER: Final = f"{SHARED_NAMESPACE}:scenario_order"
_LATENCIES: Final = f"{SHARED_NAMESPACE}:latencies"
_UTTERANCES: Final = f"{SHARED_NAMESPACE}:utterances"
_UTTERANCE_NEXT: Final = f"{SHARED_NAMESPACE}:utterance_next"

_RECONNECT_FIRST_S: Final = 1.0
_RECONNECT_MAX_S: Final = 10.0

# KEYS: version, scenarios, scenario_order; ARGV: channel, id, scenario JSON.
_PUT_SCENARIO_LUA: Final = """
local version = redis.call('INCR', KEYS[1])
redis.call('HSET', KEYS[2], ARGV[2], ARGV[3])
redis.call('ZADD', KEYS[3], version, ARGV[2])
redis.call('PUBLISH', ARGV[1], version)
return version
"""

# KEYS: version, latencies; ARGV: channel, latencies JSON.
_PUT_LATENCIES_LUA: Final = """
local version = redis.call('INCR', KEYS[1])
redis.call('SET', KEYS[2], ARGV[2])
redis.call('PUBLISH', ARGV[1], version)
return version
"""

# KEYS: version, seed, generation, scenarios, scenario_order, latencies, utterances, utterance_next;
# ARGV: channel, seed ('' keeps the stored seed).
# Returns {seed (-1 when none is stored), generation, version}.
_RESET_LUA: Final = """
if ARGV[2] ~= '' then redis.call('SET', KEYS[2], ARGV[2]) end
redis.call('DEL', KEYS[4], KEYS[5], KEYS[6], KEYS[7], KEYS[8])
local generation = redis.call('INCR', KEYS[3])
local version = redis.call('INCR', KEYS[1])
redis.call('PUBLISH', ARGV[1], version)
return {tonumber(redis.call('GET', KEYS[2]) or '-1'), generation, version}
"""

# KEYS: utterance_next, utterances; ARGV: max id, utterance JSON. Returns the id, or -1 when exhausted.
_ALLOCATE_LUA: Final = """
local next_id = redis.call('INCR', KEYS[1])
if next_id > tonumber(ARGV[1]) then
  redis.call('SET', KEYS[1], ARGV[1])
  return -1
end
redis.call('HSET', KEYS[2], next_id, ARGV[2])
return next_id
"""

_log: Final = get_logger(__name__)


def utterance_limit_message() -> str:
    return f"all {MAX_UTTERANCE_ID} utterance ids (16-bit marker) are in use; reset the sim"


class UtteranceLimitError(ValueError):
    """Every 16-bit utterance id is in use."""

    def __init__(self) -> None:
        super().__init__(utterance_limit_message())


class SharedStateUnavailable(RuntimeError):  # noqa: N818 - the name of the documented failure, not an *Error
    """The shared state Redis failed a read or write."""


@dataclass(frozen=True, slots=True)
class SharedHead:
    """What every entry point reads first: the state version and the number of allocated utterances."""

    version: int
    utterances: int


@dataclass(frozen=True, slots=True)
class SharedSnapshot:
    """The reloadable shared records at ``version`` (scenarios in registration order)."""

    version: int
    generation: int
    seed: int | None
    scenarios: tuple[Scenario, ...]
    latencies: Latencies | None


@dataclass(frozen=True, slots=True)
class ResetResult:
    seed: int | None
    generation: int
    version: int


def _int(raw: Any) -> int:
    return int(raw) if raw is not None else 0


def _utterance_json(utterance: Utterance) -> bytes:
    return json.dumps(
        {"text": utterance.text, "language": utterance.language, "words": list(utterance.words)}
    )


def _utterance_of(raw: bytes | str) -> Utterance:
    data = json.loads(raw)
    return Utterance(text=data["text"], language=data["language"], words=tuple(data["words"]))


def _latencies_of(raw: bytes | str) -> Latencies:
    data = json.loads(raw)
    return Latencies(
        stt_final_ms=int(data["stt_final_ms"]),
        tts_ttfa_ms=int(data["tts_ttfa_ms"]),
        ttft_ms=int(data["ttft_ms"]),
        tokens_per_s=float(data["tokens_per_s"]),
    )


def _key(raw: bytes | str) -> str:
    return raw.decode() if isinstance(raw, bytes) else raw


class SharedState:
    """Reads and atomic writes of the shared records in ``redis`` (keys under ``sim:state:``).

    Every failure of the Redis surfaces as :class:`SharedStateUnavailable`. ``sleep`` is the reconnect
    backoff of :meth:`subscribe` (a test seam).
    """

    def __init__(self, redis: Redis, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._redis = redis
        self._sleep = sleep
        self._put_scenario = redis.register_script(_PUT_SCENARIO_LUA)
        self._put_latencies = redis.register_script(_PUT_LATENCIES_LUA)
        self._reset = redis.register_script(_RESET_LUA)
        self._allocate = redis.register_script(_ALLOCATE_LUA)

    async def head(self) -> SharedHead:
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.get(_VERSION)
                pipe.get(_UTTERANCE_NEXT)
                version, allocated = await pipe.execute()
        except (RedisError, OSError) as exc:
            raise SharedStateUnavailable(str(exc)) from exc
        return SharedHead(version=_int(version), utterances=min(_int(allocated), MAX_UTTERANCE_ID))

    async def load(self) -> SharedSnapshot:
        """Every reloadable record, read in one ``MULTI`` (a consistent cut)."""
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.get(_VERSION)
                pipe.get(_GENERATION)
                pipe.get(_SEED)
                pipe.hgetall(_SCENARIOS)
                pipe.zrange(_SCENARIO_ORDER, 0, -1)
                pipe.get(_LATENCIES)
                version, generation, seed, scenarios, order, latencies = await pipe.execute()
        except (RedisError, OSError) as exc:
            raise SharedStateUnavailable(str(exc)) from exc
        documents: Mapping[str, bytes] = {_key(key): value for key, value in scenarios.items()}
        ordered = [_key(member) for member in order]
        return SharedSnapshot(
            version=_int(version),
            generation=_int(generation),
            seed=int(seed) if seed is not None else None,
            scenarios=tuple(
                Scenario.model_validate(json.loads(documents[scenario_id]))
                for scenario_id in ordered
                if scenario_id in documents
            ),
            latencies=_latencies_of(latencies) if latencies is not None else None,
        )

    async def _run(self, script: Any, keys: list[str], args: list[Any]) -> Any:
        try:
            return await script(keys=keys, args=args)
        except (RedisError, OSError) as exc:
            raise SharedStateUnavailable(str(exc)) from exc

    async def put_scenario(self, scenario: Scenario) -> int:
        """Store ``scenario`` (replacing one with the same id); returns the new version."""
        document = json.dumps(scenario.model_dump(mode="json"))
        keys = [_VERSION, _SCENARIOS, _SCENARIO_ORDER]
        return int(await self._run(self._put_scenario, keys, [CHANNEL, scenario.id, document]))

    async def put_latencies(self, latencies: Latencies) -> int:
        """Store the latency overrides; returns the new version."""
        document = json.dumps(asdict(latencies))
        return int(await self._run(self._put_latencies, [_VERSION, _LATENCIES], [CHANNEL, document]))

    async def reset(self, seed: int | None) -> ResetResult:
        """Drop every shared record and start a new generation.

        ``seed`` None keeps the stored seed; the result's seed is None when no seed was ever stored, so each
        process keeps its own.
        """
        keys = [
            _VERSION,
            _SEED,
            _GENERATION,
            _SCENARIOS,
            _SCENARIO_ORDER,
            _LATENCIES,
            _UTTERANCES,
            _UTTERANCE_NEXT,
        ]
        result = await self._run(self._reset, keys, [CHANNEL, "" if seed is None else str(seed)])
        stored_seed, generation, version = (int(value) for value in result)
        return ResetResult(
            seed=stored_seed if stored_seed >= 0 else None, generation=generation, version=version
        )

    async def allocate_utterance(self, utterance: Utterance) -> int:
        """The next utterance id across every process; :class:`UtteranceLimitError` past 65 535."""
        keys = [_UTTERANCE_NEXT, _UTTERANCES]
        utterance_id = int(
            await self._run(self._allocate, keys, [MAX_UTTERANCE_ID, _utterance_json(utterance)])
        )
        if utterance_id < 0:
            raise UtteranceLimitError
        return utterance_id

    async def fetch_utterance(self, utterance_id: int) -> Utterance | None:
        try:
            raw = await self._redis.hget(_UTTERANCES, str(utterance_id))
        except (RedisError, OSError) as exc:
            raise SharedStateUnavailable(str(exc)) from exc
        return _utterance_of(raw) if raw is not None else None

    async def utterances(self) -> dict[int, Utterance]:
        """The whole utterance registry (batch STT resolves every marker of a recording)."""
        try:
            raw = await self._redis.hgetall(_UTTERANCES)
        except (RedisError, OSError) as exc:
            raise SharedStateUnavailable(str(exc)) from exc
        return {int(_key(key)): _utterance_of(value) for key, value in raw.items()}

    async def subscribe(self, on_change: Callable[[], Awaitable[None]]) -> None:
        """Call ``on_change`` after (re)subscribing and on every change message, until cancelled.

        A lost connection is retried after 1 s, 2 s, 4 s, ... (capped at 10 s); the backoff restarts once a
        subscription succeeds.
        """
        delay = _RECONNECT_FIRST_S
        while True:
            try:
                async with self._redis.pubsub() as pubsub:
                    await pubsub.subscribe(CHANNEL)
                    delay = _RECONNECT_FIRST_S
                    await on_change()
                    async for message in pubsub.listen():
                        if message.get("type") == "message":
                            await on_change()
            except (RedisError, OSError) as exc:
                _log.warning("sim.shared_state.subscriber_lost", error=type(exc).__name__, retry_s=delay)
                await self._sleep(delay)
                delay = min(delay * 2, _RECONNECT_MAX_S)
