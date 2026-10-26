"""Per-pod call admission."""

from __future__ import annotations

import asyncio

import pytest

from va_media.calls import CallRegistry, CapacityError


def test_admit_release_and_capacity() -> None:
    registry = CallRegistry(2)
    assert registry.admit("a")
    assert not registry.admit("a"), "duplicate admission refused"
    assert registry.admit("b")
    assert registry.at_capacity
    assert not registry.admit("c")
    registry.release("a")
    assert (registry.active, registry.at_capacity) == (1, False)
    registry.release("missing")
    assert registry.active == 1


def test_max_calls_validated() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        CallRegistry(0)


async def test_capacity_check() -> None:
    registry = CallRegistry(1)
    await registry.capacity_check()
    registry.admit("a")
    with pytest.raises(CapacityError, match="1 of 1"):
        await registry.capacity_check()


async def test_wait_idle_returns_immediately_when_empty() -> None:
    assert await CallRegistry(1).wait_idle(0.01)


async def test_wait_idle_true_when_last_call_released() -> None:
    registry = CallRegistry(3)
    registry.admit("a")
    registry.admit("b")
    waiter = asyncio.ensure_future(registry.wait_idle(5))
    registry.release("a")
    await asyncio.sleep(0)
    assert not waiter.done()
    registry.release("b")
    assert await waiter


async def test_wait_idle_times_out_while_calls_remain() -> None:
    registry = CallRegistry(1)
    registry.admit("a")
    assert not await registry.wait_idle(0.01)
