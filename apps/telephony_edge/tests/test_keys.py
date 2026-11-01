"""Route-selected webhook keys and their cache (specs/06-telephony-telnyx.md §4.1 Decision P05-T01)."""

from __future__ import annotations

import uuid

import pytest
from edge_helpers import Keypair, ProbeContainer, edge_settings, envelope, post
from service_helpers import json_log_stream, log_events, make_client

from va_common.time import FrozenClock
from va_db.repositories import TelnyxWebhookKey
from va_edge.keys import KeyCachePolicy, UnknownAccountError, WebhookKeys
from va_edge.main import create_app

PLATFORM = Keypair.generate()


class Lookup:
    def __init__(self, found: dict[uuid.UUID, TelnyxWebhookKey]) -> None:
        self.found = found
        self.calls: list[uuid.UUID] = []

    async def __call__(self, account_id: uuid.UUID) -> TelnyxWebhookKey | None:
        self.calls.append(account_id)
        return self.found.get(account_id)


def _keys(lookup: Lookup, clock: FrozenClock) -> WebhookKeys:
    return WebhookKeys(PLATFORM.signing.verify_key, lookup, clock=clock)


async def test_platform_route_uses_the_platform_key_without_lookup() -> None:
    lookup = Lookup({})
    key = await _keys(lookup, FrozenClock()).resolve(None)
    assert (bytes(key.key), key.org_id, lookup.calls) == (bytes(PLATFORM.signing.verify_key), None, [])


async def test_account_keys_are_cached_for_five_minutes() -> None:
    account, org, byo = uuid.uuid4(), uuid.uuid4(), Keypair.generate()
    lookup = Lookup({account: TelnyxWebhookKey(public_key=byo.public_b64, org_id=org)})
    clock = FrozenClock()
    keys = _keys(lookup, clock)
    first = await keys.resolve(str(account))
    clock.advance(299)
    assert await keys.resolve(str(account)) == first
    assert len(lookup.calls) == 1
    clock.advance(1)
    await keys.resolve(str(account))
    assert len(lookup.calls) == 2
    assert (bytes(first.key), first.org_id) == (bytes(byo.signing.verify_key), org)


async def test_unknown_accounts_are_cached_for_thirty_seconds() -> None:
    account = uuid.uuid4()
    lookup = Lookup({})
    clock = FrozenClock()
    keys = _keys(lookup, clock)
    for _ in range(3):
        with pytest.raises(UnknownAccountError):
            await keys.resolve(str(account))
    assert len(lookup.calls) == 1
    clock.advance(30)
    lookup.found[account] = TelnyxWebhookKey(public_key=PLATFORM.public_b64, org_id=uuid.uuid4())
    assert (await keys.resolve(str(account))).org_id == lookup.found[account].org_id


async def test_malformed_account_id_and_stored_key_are_unknown() -> None:
    account, org = uuid.uuid4(), uuid.uuid4()
    lookup = Lookup({account: TelnyxWebhookKey(public_key="not-a-key", org_id=org)})
    keys = _keys(lookup, FrozenClock())
    stream = json_log_stream()
    with pytest.raises(UnknownAccountError, match="does not name a Telnyx account id"):
        await keys.resolve("../../etc")
    with pytest.raises(UnknownAccountError, match="no active Telnyx account"):
        await keys.resolve(str(account))
    [line] = [line for line in log_events(stream) if line["event"] == "telnyx.account_key_invalid"]
    assert (line["telnyx_account_id"], line["org_id"]) == (str(account), str(org))


@pytest.mark.parametrize("path", ["/telnyx/voice", "/telnyx/voice/0195f0c2-7a51-7c3e-9a4b-2f6d1e8c9b01"])
async def test_database_outage_answers_503(path: str) -> None:
    app = create_app(
        edge_settings(PLATFORM.public_b64), container_factory=ProbeContainer(), init_observability=False
    )
    stream = json_log_stream()
    async with make_client(app) as client:
        response = await post(client, path, envelope(), PLATFORM)
    assert (response.status_code, response.json()) == (503, {"error": "unavailable"})
    events = {line["event"] for line in log_events(stream)}
    assert events & {"telnyx.webhook_unavailable", "telnyx.webhook_store_failed"}


async def test_a_flood_of_unknown_ids_stays_bounded_and_keeps_known_keys() -> None:
    account, org, byo = uuid.uuid4(), uuid.uuid4(), Keypair.generate()
    lookup = Lookup({account: TelnyxWebhookKey(public_key=byo.public_b64, org_id=org)})
    keys = WebhookKeys(
        PLATFORM.signing.verify_key,
        lookup,
        clock=FrozenClock(),
        policy=KeyCachePolicy(max_known=2, max_unknown=3),
    )
    await keys.resolve(str(account))
    for _ in range(50):
        with pytest.raises(UnknownAccountError):
            await keys.resolve(str(uuid.uuid4()))
    assert keys.cached == (1, 3)
    calls = len(lookup.calls)
    assert (await keys.resolve(str(account))).org_id == org
    assert len(lookup.calls) == calls


async def test_known_cache_evicts_the_least_recently_used_account() -> None:
    accounts = [uuid.uuid4() for _ in range(3)]
    lookup = Lookup(
        {a: TelnyxWebhookKey(public_key=PLATFORM.public_b64, org_id=uuid.uuid4()) for a in accounts}
    )
    keys = WebhookKeys(
        PLATFORM.signing.verify_key, lookup, clock=FrozenClock(), policy=KeyCachePolicy(max_known=2)
    )
    first, second, third = (str(a) for a in accounts)
    await keys.resolve(first)
    await keys.resolve(second)
    await keys.resolve(first)
    await keys.resolve(third)
    assert keys.cached == (2, 0)
    lookup.calls.clear()
    await keys.resolve(first)
    await keys.resolve(third)
    assert lookup.calls == []
    await keys.resolve(second)
    assert lookup.calls == [accounts[1]]


async def test_an_account_that_appears_moves_out_of_the_unknown_cache() -> None:
    account = uuid.uuid4()
    lookup = Lookup({})
    clock = FrozenClock()
    keys = _keys(lookup, clock)
    with pytest.raises(UnknownAccountError):
        await keys.resolve(str(account))
    clock.advance(30)
    lookup.found[account] = TelnyxWebhookKey(public_key=PLATFORM.public_b64, org_id=uuid.uuid4())
    await keys.resolve(str(account))
    assert keys.cached == (1, 0)
    clock.advance(300)
    del lookup.found[account]
    with pytest.raises(UnknownAccountError):
        await keys.resolve(str(account))
    assert keys.cached == (0, 1)


def test_a_cache_needs_room() -> None:
    with pytest.raises(ValueError, match="at least one entry"):
        WebhookKeys(
            PLATFORM.signing.verify_key, Lookup({}), clock=FrozenClock(), policy=KeyCachePolicy(max_unknown=0)
        )
