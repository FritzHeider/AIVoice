"""Building and storing the per-call context (specs/01 §2.1 step 2 Decision P05-T06a)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fakeredis import FakeAsyncRedis
from resolved_configs import RESOLVED_NAMES, resolved_config
from service_helpers import json_log_stream, log_events, reset_logging

from va_common.ids import new_id
from va_db.call_context import CallContextStore, build_call_context, config_of
from va_db.models import Call
from va_domain.call_context import CallContext, call_context_key
from va_domain.calls.config import SendCallConfig

__all__ = ["reset_logging"]

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)


def _call(config: dict[str, Any], **over: Any) -> Call:
    values: dict[str, Any] = {
        "id": new_id(),
        "org_id": new_id(),
        "direction": "outbound",
        "status": "queued",
        "runner_type": config["runner_type"],
        "config": config,
        "to_number": "+14155550123",
        "from_number": "+14155550124",
        "batch_id": None,
        "contact_id": None,
        "test_call": False,
        **over,
    }
    return Call(**values)


@pytest.fixture
async def redis() -> AsyncIterator[FakeAsyncRedis]:
    client = FakeAsyncRedis()
    yield client
    await client.aclose()


@pytest.mark.parametrize("name", RESOLVED_NAMES)
def test_every_resolved_config_builds_a_context(name: str) -> None:
    config = resolved_config(name)
    stored = {**config, "pricing": {"per_minute_micros": 90_000}, "postcall_steps": ["summary"]}
    call = _call(stored)

    context = build_call_context(call, now=NOW)

    assert context.config == SendCallConfig.model_validate(config)
    assert context.config.model_dump(mode="json", by_alias=True) == config
    assert (context.call_id, context.org_id, context.direction) == (call.id, call.org_id, "outbound")
    assert (context.to_number, context.from_number) == ("+14155550123", "+14155550124")
    assert context.created_at == NOW


def test_config_of_drops_keys_that_are_not_configuration() -> None:
    config = resolved_config()

    assert config_of(_call({**config, "pricing": {"x": 1}})) == SendCallConfig.model_validate(config)


def test_null_row_numbers_fall_back_to_the_configuration() -> None:
    config = resolved_config()

    context = build_call_context(_call(config, to_number=None, from_number=None), now=NOW)

    assert (context.to_number, context.from_number) == (config["phone_number"], config["from_number"])


def test_row_facts_and_the_trace_context_are_copied() -> None:
    batch_id, contact_id = uuid.uuid4(), uuid.uuid4()
    carrier = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
    call = _call(
        resolved_config(), direction="inbound", batch_id=batch_id, contact_id=contact_id, test_call=True
    )

    context = build_call_context(call, now=NOW, trace_context=carrier)

    assert (context.direction, context.batch_id, context.contact_id, context.test_call) == (
        "inbound",
        batch_id,
        contact_id,
        True,
    )
    assert context.trace_context == carrier
    assert context.trace_context is not carrier


async def test_save_sets_the_ttl_and_load_round_trips(redis: FakeAsyncRedis) -> None:
    store = CallContextStore(redis)
    context = build_call_context(_call(resolved_config()), now=NOW)

    await store.save(context)

    assert await redis.ttl(call_context_key(context.call_id)) == context.ttl_s() == 3900
    assert await store.load(context.call_id) == context


async def test_a_missing_context_loads_as_none(redis: FakeAsyncRedis) -> None:
    assert await CallContextStore(redis).load(uuid.uuid4()) is None


@pytest.mark.parametrize("raw", [b"not json", b'{"version": 1}', b"[]"], ids=["garbage", "schema", "array"])
async def test_an_unreadable_context_loads_as_none_and_is_logged(redis: FakeAsyncRedis, raw: bytes) -> None:
    stream = json_log_stream()
    call_id = uuid.uuid4()
    await redis.set(call_context_key(call_id), raw)

    assert await CallContextStore(redis).load(call_id) is None

    [event] = [line for line in log_events(stream) if line["event"] == "call_context.invalid"]
    assert event["call_id"] == str(call_id)
    assert event["error_type"] == CallContext.__name__


async def test_delete_removes_the_context(redis: FakeAsyncRedis) -> None:
    store = CallContextStore(redis)
    context = build_call_context(_call(resolved_config()), now=NOW)
    await store.save(context)

    await store.delete(context.call_id)

    assert await redis.exists(call_context_key(context.call_id)) == 0


async def test_the_stored_json_uses_the_spec_field_names(redis: FakeAsyncRedis) -> None:
    store = CallContextStore(redis)
    context = build_call_context(_call(resolved_config("09_voicemail_amd.json")), now=NOW)

    await store.save(context)

    raw = await redis.get(call_context_key(context.call_id))
    assert isinstance(raw, bytes)
    assert b'"from":' in raw
    assert b'"from_"' not in raw
    assert await store.load(context.call_id) == context
