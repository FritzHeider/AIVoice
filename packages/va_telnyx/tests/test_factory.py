"""TelnyxClientFactory: platform vs BYO clients, the 5-minute key cache, single-flight resolves, invalidation
(specs/06-telephony-telnyx.md §1; Decision 2026-11-01 P03-T08b rule 6)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import fakeredis
import httpx
import pytest
import respx
from clock_helpers import RecordingClock
from telnyx_client_helpers import API_KEY, BASE_URL

from va_telnyx import PLATFORM_ACCOUNT, TelnyxClientFactory, TelnyxRateLimiter, UnknownTelnyxAccount

ACCOUNT = uuid.UUID("0192f3a0-1111-7000-8000-000000000001")
OTHER = uuid.UUID("0192f3a0-1111-7000-8000-000000000002")
BYO_KEY = "KEY-byo-account"
BALANCE = {"data": {"balance": "1", "credit_limit": "0", "available_credit": "1", "currency": "USD"}}


@dataclass
class Resolver:
    """Resolves from ``keys``; with ``hold_until`` set, every resolve waits until that many have started."""

    keys: dict[uuid.UUID, str | None]
    calls: list[uuid.UUID] = field(default_factory=list)
    hold_until: int = 0
    gate: asyncio.Event = field(default_factory=asyncio.Event)

    async def __call__(self, account_id: uuid.UUID) -> str | None:
        self.calls.append(account_id)
        if len(self.calls) >= self.hold_until:
            self.gate.set()
        await self.gate.wait()
        return self.keys.get(account_id)


@dataclass
class Call:
    config: dict[str, Any]


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE_URL, assert_all_called=False, assert_all_mocked=True) as router:
        router.get("/balance").respond(200, json=BALANCE)
        yield router


def _factory(http: httpx.AsyncClient, resolver: Resolver, clock: RecordingClock) -> TelnyxClientFactory:
    return TelnyxClientFactory(
        base_url=BASE_URL,
        http=http,
        limiter=TelnyxRateLimiter(fakeredis.FakeAsyncRedis(), clock=clock),
        platform_api_key=API_KEY,
        resolve_key=resolver,
        clock=clock,
    )


async def test_platform_client_for_none_and_absent_config(api: respx.MockRouter) -> None:
    resolver = Resolver({})
    async with httpx.AsyncClient() as http:
        factory = _factory(http, resolver, RecordingClock())
        assert await factory.for_account(None) is factory.platform
        assert await factory.for_call(Call({})) is factory.platform
        assert await factory.for_call(Call({"telnyx_account_id": None})) is factory.platform
        assert factory.platform.account == PLATFORM_ACCOUNT
        await factory.platform.get_balance()
    assert resolver.calls == []
    assert api.calls[0].request.headers["authorization"] == f"Bearer {API_KEY}"


async def test_byo_client_uses_the_resolved_key_and_account_label(api: respx.MockRouter) -> None:
    resolver = Resolver({ACCOUNT: BYO_KEY})
    async with httpx.AsyncClient() as http:
        factory = _factory(http, resolver, RecordingClock())
        client = await factory.for_call(Call({"telnyx_account_id": str(ACCOUNT)}))
        assert client.account == str(ACCOUNT)
        assert await factory.for_call(Call({"telnyx_account_id": ACCOUNT})) is client
        await client.get_balance()
    assert api.calls[0].request.headers["authorization"] == f"Bearer {BYO_KEY}"
    assert resolver.calls == [ACCOUNT]


async def test_keys_are_cached_for_five_minutes(api: respx.MockRouter) -> None:
    resolver = Resolver({ACCOUNT: BYO_KEY})
    clock = RecordingClock()
    async with httpx.AsyncClient() as http:
        factory = _factory(http, resolver, clock)
        first = await factory.for_account(ACCOUNT)
        clock.advance(299)
        assert await factory.for_account(ACCOUNT) is first
        clock.advance(2)
        assert await factory.for_account(ACCOUNT) is not first
    assert resolver.calls == [ACCOUNT, ACCOUNT]
    assert len(api.calls) == 0


async def test_concurrent_lookups_resolve_once() -> None:
    resolver = Resolver({ACCOUNT: BYO_KEY, OTHER: "KEY-other"}, hold_until=2)
    async with httpx.AsyncClient() as http:
        factory = _factory(http, resolver, RecordingClock())
        tasks = [asyncio.create_task(factory.for_account(ACCOUNT)) for _ in range(10)]
        other = asyncio.create_task(factory.for_account(OTHER))
        clients = await asyncio.gather(*tasks)
        assert (await other).account == str(OTHER)
    assert len({id(client) for client in clients}) == 1
    assert sorted(resolver.calls) == sorted([ACCOUNT, OTHER])


async def test_unknown_account_raises_and_is_not_cached() -> None:
    resolver = Resolver({ACCOUNT: None})
    async with httpx.AsyncClient() as http:
        factory = _factory(http, resolver, RecordingClock())
        for _ in range(2):
            with pytest.raises(UnknownTelnyxAccount) as caught:
                await factory.for_account(ACCOUNT)
            assert caught.value.telnyx_account_id == ACCOUNT
            assert isinstance(caught.value, LookupError)
    assert resolver.calls == [ACCOUNT, ACCOUNT]


async def test_invalidate_forces_a_new_resolve() -> None:
    resolver = Resolver({ACCOUNT: BYO_KEY})
    async with httpx.AsyncClient() as http:
        factory = _factory(http, resolver, RecordingClock())
        first = await factory.for_account(ACCOUNT)
        factory.invalidate(ACCOUNT)
        factory.invalidate(OTHER)
        resolver.keys[ACCOUNT] = "KEY-rotated"
        assert await factory.for_account(ACCOUNT) is not first
    assert resolver.calls == [ACCOUNT, ACCOUNT]


async def test_an_invalid_account_id_in_the_call_config_is_rejected() -> None:
    async with httpx.AsyncClient() as http:
        factory = _factory(http, Resolver({}), RecordingClock())
        with pytest.raises(ValueError, match="not a UUID"):
            await factory.for_call(Call({"telnyx_account_id": "acct-1"}))


async def test_repr_hides_keys() -> None:
    async with httpx.AsyncClient() as http:
        factory = _factory(http, Resolver({ACCOUNT: BYO_KEY}), RecordingClock())
        client = await factory.for_account(ACCOUNT)
        text = repr(factory) + repr(client) + repr(factory.platform)
    assert API_KEY not in text
    assert BYO_KEY not in text
    assert "cached_accounts=1" in repr(factory)
