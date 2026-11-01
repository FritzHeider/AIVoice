"""Circuit breakers per ``(provider_id, model)`` (specs/10-llm-gateway.md §2.2; Decision P03-T13a
rule 7).

Closed → open on the ``failures``-th failure within ``window_s``; open → half-open after ``open_s``; half-open
admits exactly one probe; its success closes the breaker (failure window cleared), its failure reopens it.
Failures are timeouts, 5xx answers, and connect errors; any other HTTP answer means the server is up and
counts as a success. State lives in the process.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from typing import Final, Literal

from va_common.logging import get_logger
from va_common.time import Clock

__all__ = ["BREAKER_STATE_VALUES", "BreakerBoard", "BreakerState", "CircuitBreaker", "TransitionHook"]

type BreakerState = Literal["closed", "half_open", "open"]
type TransitionHook = Callable[[BreakerState, BreakerState], None]

BREAKER_STATE_VALUES: Final[dict[BreakerState, int]] = {"closed": 0, "half_open": 1, "open": 2}

_log = get_logger(__name__)


class CircuitBreaker:
    """One breaker; :meth:`allow` before a request, then exactly one :meth:`record` or :meth:`abandon`."""

    def __init__(
        self,
        clock: Clock,
        *,
        failures: int = 5,
        window_s: float = 30.0,
        open_s: float = 20.0,
        on_transition: TransitionHook | None = None,
    ) -> None:
        self._clock = clock
        self._threshold = failures
        self._window_s = window_s
        self._open_s = open_s
        self._on_transition = on_transition
        self._failures: deque[float] = deque()
        self._state: BreakerState = "closed"
        self._opened_at = 0.0
        self._probing = False

    @property
    def state(self) -> BreakerState:
        return self._state

    def allow(self) -> bool:
        """Whether a request may go out now (an open breaker turns half-open once ``open_s`` passed)."""
        if self._state == "open" and self._clock.monotonic() - self._opened_at >= self._open_s:
            self._move("half_open")
        if self._state == "closed":
            return True
        if self._state == "half_open" and not self._probing:
            self._probing = True
            return True
        return False

    def record(self, *, success: bool) -> None:
        """The verdict of a request that :meth:`allow` admitted."""
        if self._state == "half_open":
            self._probing = False
            if success:
                self._failures.clear()
                self._move("closed")
            else:
                self._open()
            return
        if success or self._state == "open":
            return
        now = self._clock.monotonic()
        self._failures.append(now)
        while self._failures and now - self._failures[0] > self._window_s:
            self._failures.popleft()
        if len(self._failures) >= self._threshold:
            self._open()

    def abandon(self) -> None:
        """An admitted request ended without a verdict (its caller was cancelled): free the half-open
        probe."""
        self._probing = False

    def _open(self) -> None:
        self._opened_at = self._clock.monotonic()
        self._failures.clear()
        self._move("open")

    def _move(self, state: BreakerState) -> None:
        previous, self._state = self._state, state
        if self._on_transition is not None and previous != state:
            self._on_transition(previous, state)


class BreakerBoard:
    """The breakers of one process, created on first use; transitions are logged ``llm.breaker_transition``
    and passed to ``on_state`` (the ``va_llm_breaker_state`` gauge)."""

    def __init__(
        self, clock: Clock, on_state: Callable[[str, str, BreakerState], None] | None = None
    ) -> None:
        self._clock = clock
        self._on_state = on_state
        self._breakers: dict[tuple[str, str], CircuitBreaker] = {}

    def get(self, provider_id: str, model: str) -> CircuitBreaker:
        key = (provider_id, model)
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = CircuitBreaker(self._clock, on_transition=self._hook(provider_id, model))
            self._breakers[key] = breaker
            if self._on_state is not None:
                self._on_state(provider_id, model, "closed")
        return breaker

    def _hook(self, provider_id: str, model: str) -> TransitionHook:
        def transition(previous: BreakerState, state: BreakerState) -> None:
            _log.warning(
                "llm.breaker_transition", provider=provider_id, model=model, **{"from": previous, "to": state}
            )
            if self._on_state is not None:
                self._on_state(provider_id, model, state)

        return transition
