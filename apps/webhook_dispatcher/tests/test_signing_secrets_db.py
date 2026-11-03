"""Dual signing during a secret rotation (specs/19-events-and-webhooks.md §10 Decision P06-T04a, §11)."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from dispatch_helpers import Rig, dispatch_rig
from webhook_receiver import Received, signature_ok

from va_common.time import FrozenClock
from va_db.webhook_secrets import ROTATION_OVERLAP, rotate_signing_secret
from va_webhooks.signing_secrets import SECRET_TTL_S, SecretUnavailableError, SigningSecrets

pytestmark = pytest.mark.integration


def _versions(received: Received) -> tuple[str, list[str]]:
    items = [item.split("=", 1) for item in received.headers["x-va-signature"].split(",")]
    timestamp = next(value for key, value in items if key == "t")
    return timestamp, [value for key, value in items if key == "v1"]


def _hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


async def _rotate(rig: Rig, org_id: uuid.UUID, now: datetime) -> str:
    async with rig.database.global_session() as session:
        rotated = await rotate_signing_secret(session, rig.envelope, org_id, now=now)
    return rotated.secret


async def test_a_rotated_org_is_signed_with_both_secrets_new_first(dispatcher_db_url: str) -> None:
    async with dispatch_rig(dispatcher_db_url) as rig:
        org = await rig.org()
        new_secret = await _rotate(rig, org.id, datetime.now(UTC))
        await rig.delivery(org, rig.url("/rotated"))
        assert await rig.dispatcher.run_once() == 1
    (received,) = rig.receiver.requests
    timestamp, versions = _versions(received)
    signed = f"{timestamp}.".encode() + received.body
    assert versions == [_hex(new_secret, signed), _hex(org.secret, signed)]
    assert signature_ok(received, new_secret)
    assert received.headers["x-webhook-signature"] == _hex(new_secret, received.body)


async def test_the_previous_secret_stops_signing_at_its_expiry(dispatcher_db_url: str) -> None:
    async with dispatch_rig(dispatcher_db_url) as rig:
        org = await rig.org()
        start = datetime.now(UTC)
        new_secret = await _rotate(rig, org.id, start)
        clock = FrozenClock(start + ROTATION_OVERLAP - timedelta(seconds=10))
        secrets = SigningSecrets(rig.database, rig.envelope, clock=clock, ttl_s=SECRET_TTL_S)
        assert await secrets.get(org.id) == (new_secret, org.secret)
        clock.advance(9)
        assert await secrets.get(org.id) == (new_secret, org.secret)
        # The cached entry lives only until the previous secret expires, not for the full TTL.
        clock.advance(1)
        assert await secrets.get(org.id) == (new_secret,)


async def test_without_a_rotation_window_the_cache_keeps_the_full_ttl(dispatcher_db_url: str) -> None:
    async with dispatch_rig(dispatcher_db_url) as rig:
        org = await rig.org()
        clock = FrozenClock(datetime.now(UTC))
        secrets = SigningSecrets(rig.database, rig.envelope, clock=clock, ttl_s=SECRET_TTL_S)
        assert await secrets.get(org.id) == (org.secret,)
        new_secret = await _rotate(rig, org.id, clock.now())
        clock.advance(SECRET_TTL_S - 1)
        assert await secrets.get(org.id) == (org.secret,)
        clock.advance(1)
        assert await secrets.get(org.id) == (new_secret, org.secret)


async def test_an_unknown_org_is_unavailable(dispatcher_db_url: str) -> None:
    async with dispatch_rig(dispatcher_db_url) as rig:
        org = await rig.org()
        secrets = SigningSecrets(rig.database, rig.envelope, clock=FrozenClock(datetime.now(UTC)))
        with pytest.raises(SecretUnavailableError, match="org not found"):
            await secrets.get(uuid.UUID(int=org.id.int ^ 1))
