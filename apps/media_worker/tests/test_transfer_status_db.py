"""``DbTransferStatus`` on Postgres: the ``transferring`` round trip of a cold transfer (specs/05 §8 Decision
P05-T13)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import cast

import pytest
from call_rows import load_call, seed_call
from media_helpers import BrokenDatabase
from service_helpers import json_log_stream, log_events
from tenant_rows import open_database

from va_db.session import Database
from va_media.transfer import DbTransferStatus


@pytest.fixture
async def database(media_db_url: str) -> AsyncIterator[Database]:
    async with open_database(media_db_url) as db:
        yield db


async def test_a_transfer_enters_transferring_and_a_failure_reverts(database: Database) -> None:
    call = await seed_call(database, status="in_progress", answered_at=datetime.now(UTC))
    status = DbTransferStatus(database, call.org_id)
    assert await status.start(call.call_id) == "started"
    assert (await load_call(database, call)).status == "transferring"
    assert await status.start(call.call_id) == "not_active"
    await status.revert(call.call_id)
    assert (await load_call(database, call)).status == "in_progress"


async def test_a_revert_of_a_call_that_moved_on_is_skipped(database: Database) -> None:
    stream = json_log_stream()
    call = await seed_call(database, status="transferred", answered_at=datetime.now(UTC))
    await DbTransferStatus(database, call.org_id).revert(call.call_id)
    assert (await load_call(database, call)).status == "transferred"
    [skipped] = [e for e in log_events(stream) if e["event"] == "media.transfer_revert_skipped"]
    assert skipped["status"] == "transferred"


async def test_database_failures_are_logged() -> None:
    stream = json_log_stream()
    status = DbTransferStatus(cast("Database", BrokenDatabase()), uuid.uuid4())
    assert await status.start(uuid.uuid4()) == "unavailable"
    await status.revert(uuid.uuid4())
    events = {e["event"]: e for e in log_events(stream)}
    assert events["media.transfer_status_failed"]["error_type"] == "OperationalError"
    assert events["media.transfer_revert_failed"]["error_type"] == "OperationalError"
