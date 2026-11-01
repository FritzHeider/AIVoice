"""The provider probe run when an org saves its provider (specs/10-llm-gateway.md §1; Decision P03-T13a
rule 4).

One non-streaming completion on the provider's ``router`` model with a forced ``va_probe`` call and
``max_tokens=16`` (a forced call's arguments do not fit in one token). A tool call in the answer means the
provider honours ``tool_choice``; an answer without one, or a 400/422 rejecting the tools, is retried once
without tools to tell "works, no forced tools" from "does not work".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import httpx
import openai

from va_common.time import Clock, SystemClock
from va_llm.clients import openai_client
from va_llm.errors import map_openai_error
from va_llm.providers import LlmProvider

__all__ = ["PROBE_TIMEOUT_S", "PROBE_TOOL", "ProbeResult", "probe_provider"]

PROBE_TIMEOUT_S: Final = 10.0
PROBE_TOOL: Final = "va_probe"
_REJECTED: Final = frozenset({400, 422})
_PROMPT: Final = f"Reply by calling {PROBE_TOOL} with ok set to true."
_TOOL: Final = {
    "type": "function",
    "function": {
        "name": PROBE_TOOL,
        "description": "Confirms that the provider can call functions.",
        "parameters": {"type": "object", "properties": {"ok": {"type": "boolean"}}},
    },
}


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """``error`` is ``None`` when the provider answered; otherwise ``"<kind>: <message>"`` (never the key)."""

    supports_tool_choice: bool
    error: str | None


class _Probe:
    def __init__(self, provider: LlmProvider, http: httpx.AsyncClient, clock: Clock) -> None:
        self._provider = provider
        self._clock = clock
        self._model = provider.models["router"]
        self._client = openai_client(provider, http, timeout=httpx.Timeout(PROBE_TIMEOUT_S))

    async def ask(self, **extra: Any) -> Any:
        return await self._client.chat.completions.create(
            model=self._model,
            messages=[{"role": "user", "content": _PROMPT}],
            temperature=0,
            max_tokens=16,
            **extra,
        )

    def describe(self, exc: openai.OpenAIError) -> str:
        error = map_openai_error(exc, provider_id=self._provider.id, model=self._model, now=self._clock.now())
        message = error.message.replace(self._provider.api_key.get_secret_value(), "***")
        return f"{error.error_kind}: {message}"


def _has_tool_call(completion: Any) -> bool:
    choices = getattr(completion, "choices", None)
    if not isinstance(choices, list) or not choices:
        return False
    message = getattr(choices[0], "message", None)
    return bool(getattr(message, "tool_calls", None))


async def probe_provider(
    provider: LlmProvider, http: httpx.AsyncClient, *, clock: Clock | None = None
) -> ProbeResult:
    """Whether ``provider`` answers and honours a forced tool call."""
    probe = _Probe(provider, http, clock or SystemClock())
    try:
        forced = await probe.ask(
            tools=[_TOOL], tool_choice={"type": "function", "function": {"name": PROBE_TOOL}}
        )
    except openai.APIStatusError as exc:
        if exc.status_code not in _REJECTED:
            return ProbeResult(supports_tool_choice=False, error=probe.describe(exc))
    except openai.OpenAIError as exc:
        return ProbeResult(supports_tool_choice=False, error=probe.describe(exc))
    else:
        if _has_tool_call(forced):
            return ProbeResult(supports_tool_choice=True, error=None)
    try:
        await probe.ask()
    except openai.OpenAIError as exc:
        return ProbeResult(supports_tool_choice=False, error=probe.describe(exc))
    return ProbeResult(supports_tool_choice=False, error=None)
