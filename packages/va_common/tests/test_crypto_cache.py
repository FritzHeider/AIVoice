from __future__ import annotations

import os
import uuid

import pytest

from va_common.crypto import DataKeyCache, WrappedDataKey
from va_common.time import FrozenClock


def wrapped(tag: int = 0) -> WrappedDataKey:
    return WrappedDataKey(kek_id="local:v1", encrypted_data_key=bytes([tag]) * 40)


def test_miss_then_hit() -> None:
    cache = DataKeyCache(clock=FrozenClock())
    org = uuid.uuid4()
    key = os.urandom(32)
    assert cache.get(org, wrapped()) is None
    cache.put(org, wrapped(), key)
    assert cache.get(org, wrapped()) == key
    assert len(cache) == 1


def test_entries_are_distinct_per_org_and_key() -> None:
    cache = DataKeyCache(clock=FrozenClock())
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    cache.put(org_a, wrapped(1), b"a" * 32)
    assert cache.get(org_b, wrapped(1)) is None
    assert cache.get(org_a, wrapped(2)) is None
    assert cache.get(org_a, WrappedDataKey("other", wrapped(1).encrypted_data_key)) is None


def test_ttl_expiry_evicts() -> None:
    clock = FrozenClock()
    cache = DataKeyCache(ttl_seconds=600, clock=clock)
    org = uuid.uuid4()
    cache.put(org, wrapped(), b"k" * 32)
    clock.advance(599.9)
    assert cache.get(org, wrapped()) == b"k" * 32
    clock.advance(0.1)
    assert cache.get(org, wrapped()) is None
    assert len(cache) == 0


def test_put_refreshes_ttl() -> None:
    clock = FrozenClock()
    cache = DataKeyCache(ttl_seconds=10, clock=clock)
    org = uuid.uuid4()
    cache.put(org, wrapped(), b"1" * 32)
    clock.advance(8)
    cache.put(org, wrapped(), b"2" * 32)
    clock.advance(8)
    assert cache.get(org, wrapped()) == b"2" * 32


def test_lru_eviction_order_and_get_refreshes_recency() -> None:
    cache = DataKeyCache(max_entries=2, clock=FrozenClock())
    org = uuid.uuid4()
    cache.put(org, wrapped(1), b"1" * 32)
    cache.put(org, wrapped(2), b"2" * 32)
    assert cache.get(org, wrapped(1)) == b"1" * 32
    cache.put(org, wrapped(3), b"3" * 32)
    assert len(cache) == 2
    assert cache.get(org, wrapped(2)) is None
    assert cache.get(org, wrapped(1)) == b"1" * 32
    assert cache.get(org, wrapped(3)) == b"3" * 32


def test_clear() -> None:
    cache = DataKeyCache()
    cache.put(uuid.uuid4(), wrapped(), b"k" * 32)
    cache.clear()
    assert len(cache) == 0


@pytest.mark.parametrize(("max_entries", "ttl"), [(0, 600.0), (10, 0.0), (10, -1.0)])
def test_rejects_invalid_limits(max_entries: int, ttl: float) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        DataKeyCache(max_entries=max_entries, ttl_seconds=ttl)
