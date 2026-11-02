"""``read_carrier_state``: the edge's ``call:{<id>}:carrier_state`` hash as a snapshot (specs/07 §3
step 6).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import fakeredis

from va_domain.carrier import carrier_state_key
from va_media.carrier_state import CarrierSnapshot, read_carrier_state


async def test_snapshot_of_an_edge_shaped_hash() -> None:
    redis = fakeredis.FakeAsyncRedis()
    call_id = uuid.uuid4()
    await redis.hset(
        carrier_state_key(call_id),
        mapping={
            "answered": "1",
            "answered_at": "2026-11-02T12:00:01+00:00",
            "amd_result": "machine",
            "answered_by": "voicemail",
            "hangup_cause": "normal_clearing",
            "streaming": "1",
        },
    )
    snapshot = await read_carrier_state(redis, call_id)
    assert snapshot.answered
    assert snapshot.answered_at == datetime(2026, 11, 2, 12, 0, 1, tzinfo=UTC)
    assert (snapshot.amd_result, snapshot.answered_by, snapshot.hangup_cause) == (
        "machine",
        "voicemail",
        "normal_clearing",
    )
    assert snapshot.fields["streaming"] == "1"


async def test_empty_hash_means_nothing_happened_yet() -> None:
    snapshot = await read_carrier_state(fakeredis.FakeAsyncRedis(), uuid.uuid4())
    assert snapshot == CarrierSnapshot()
    assert not snapshot.answered
    assert (snapshot.answered_at, snapshot.amd_result, snapshot.answered_by, snapshot.hangup_cause) == (
        None,
        None,
        None,
        None,
    )


def test_unparsable_answered_at_reads_as_missing() -> None:
    assert CarrierSnapshot({"answered": "0", "answered_at": "yesterday"}).answered_at is None
    assert not CarrierSnapshot({"answered": "0"}).answered
