"""FakeLLM state (specs/30 §3.4; Decision P03-T06a rules 1, 4, 9-11): accepted keys, models, rules, the
request log, the armed fault, and the release signal of requests a ``timeout`` fault holds.

``sim-llm-key`` (the specs/02 §6 ``LLM_DEFAULT_API_KEY`` dev value) always authenticates; more keys and models
can be added at runtime and are dropped by ``reset``, which also drops runtime rules, ``call_index`` counters,
the log, and the fault and releases every held request. Rules loaded from ``SIM_LLM_RULES`` files stay.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Final, Literal

from pydantic import JsonValue

from va_sim.llm.rules import RuleBook

__all__ = ["DEFAULT_API_KEY", "FaultKind", "LlmFault", "LlmRequest", "LlmStore"]

type FaultKind = Literal["rate_limit", "server_error", "timeout", "malformed_tool_args", "disconnect"]

DEFAULT_API_KEY: Final = "sim-llm-key"
_REQUEST_PREFIX: Final = "llmreq_"


@dataclass(slots=True)
class LlmFault:
    """An armed fault: fires ``remaining`` more times."""

    kind: FaultKind
    remaining: int
    status: int | None = None
    retry_after_s: int | None = None
    after_tokens: int | None = None

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(slots=True)
class LlmRequest:
    """One chat completion as served at ``GET /sim/llm/requests``."""

    id: str
    received_at: str
    model: str
    call_id: str | None
    stream: bool
    messages: list[JsonValue]
    tools: list[JsonValue] | None
    tool_choice: JsonValue
    temperature: float | None
    max_tokens: int | None
    response_format: JsonValue
    rule_id: str | None = None
    fault: FaultKind | None = None
    status: int | None = None
    finish_reason: str | None = None

    def to_json(self) -> dict[str, object]:
        return asdict(self)


class LlmStore:
    """Keys, models, rules, request log, fault, and held-request release for FakeLLM."""

    def __init__(self, model_ids: Sequence[str]) -> None:
        self._base_models = tuple(dict.fromkeys(model_ids))
        self.models: list[str] = list(self._base_models)
        self.api_keys: list[str] = []
        self.rules = RuleBook()
        self.requests: list[LlmRequest] = []
        self.fault: LlmFault | None = None
        self.held = 0
        self._release = asyncio.Event()
        self._held_changed = asyncio.Event()

    def accepts(self, key: str) -> bool:
        return key == DEFAULT_API_KEY or key in self.api_keys

    def add_key(self, key: str) -> None:
        if key != DEFAULT_API_KEY and key not in self.api_keys:
            self.api_keys.append(key)

    def add_model(self, model_id: str) -> None:
        if model_id not in self.models:
            self.models.append(model_id)

    def next_request_id(self) -> str:
        return f"{_REQUEST_PREFIX}{len(self.requests) + 1}"

    def arm_fault(
        self,
        kind: FaultKind,
        *,
        times: int,
        status: int | None = None,
        retry_after_s: int | None = None,
        after_tokens: int | None = None,
    ) -> None:
        self.fault = LlmFault(
            kind=kind, remaining=times, status=status, retry_after_s=retry_after_s, after_tokens=after_tokens
        )

    def take_fault(self, kinds: frozenset[FaultKind]) -> LlmFault | None:
        """Fire the armed fault when its kind is in ``kinds``."""
        fault = self.fault
        if fault is None or fault.kind not in kinds:
            return None
        fault.remaining -= 1
        if fault.remaining == 0:
            self.fault = None
        return fault

    async def hold(self, disconnected: asyncio.Future[Any]) -> None:
        """Wait until ``release_held`` runs or ``disconnected`` completes (a ``timeout`` fault)."""
        released: asyncio.Future[Any] = asyncio.ensure_future(self._release.wait())
        self._set_held(self.held + 1)
        try:
            await asyncio.wait({released, disconnected}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._set_held(self.held - 1)
            released.cancel()
            await asyncio.gather(released, return_exceptions=True)

    def _set_held(self, count: int) -> None:
        self.held = count
        self._held_changed.set()

    async def wait_held(self, count: int) -> None:
        """Wait until exactly ``count`` requests are held (tests synchronise on timeouts with this)."""
        while self.held != count:
            self._held_changed.clear()
            await self._held_changed.wait()

    def release_held(self) -> None:
        """Let every held request go; later holds wait for the next release."""
        self._release.set()
        self._release = asyncio.Event()

    def reset(self) -> None:
        self.release_held()
        self.rules.reset()
        self.requests.clear()
        self.fault = None
        self.api_keys.clear()
        self.models = list(self._base_models)
