"""The worker's own wiring runs ``CallWorkflow`` end to end: real activities on the migrated database,
FakeTelnyx, fakeredis, and the Temporal time-skipping server (specs/01-architecture.md §1 Decision P05-T06d).

The workflow is started the way the api starts it: a JSON dict argument through a client with Temporal's
default converter, decoded by the worker's pydantic converter.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import timedelta

import pytest
from call_rows import SeededCall, load_call
from service_helpers import settings_of
from telephony_rig import Rig, rig
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.converter import DataConverter
from temporalio.testing import WorkflowEnvironment
from workflow_rig import workflow_env

from va_billing import keys as billing_keys
from va_common.settings import TemporalSettings
from va_db.session import Database
from va_domain.call_context import CallContext, call_context_key
from va_domain.call_workflow import CALL_WORKFLOW_NAME, CANCEL_SIGNAL, CallWorkflowInput
from va_domain.post_call import postcall_workflow_id
from va_worker.activities import WorkerActivities, main_registrations
from va_worker.runner import TemporalWorkers
from va_workflows.calls import CallWorkflowResult

pytestmark = pytest.mark.integration

TASK_QUEUE = "va-test-main"
POSTCALL_QUEUE = "va-test-postcall"
DIAL_WAIT_S = 30
SLOT_TTL = timedelta(minutes=10)


class _EnvClient:
    """The ``ClientSource`` of the worker runner over the test server's (pydantic) client."""

    def __init__(self, client: Client) -> None:
        self._client = client

    async def client(self) -> Client:
        return self._client


@asynccontextmanager
async def running_worker(env: WorkflowEnvironment, r: Rig) -> AsyncIterator[None]:
    """The worker's activities and main registration, polled by the worker's own runner."""
    activities = WorkerActivities.build(
        database=r.database, redis=r.redis, telnyx=r.factory, dial=r.settings, clock=r.clock
    )
    registrations = main_registrations(
        settings_of(TemporalSettings, temporal_task_queue=TASK_QUEUE), activities
    )
    workers = TemporalWorkers()
    running = asyncio.create_task(workers.run(_EnvClient(env.client), registrations))
    try:
        yield
    finally:
        await workers.shutdown()
        await running


def api_client(env: WorkflowEnvironment) -> Client:
    """A client with Temporal's default converter, as the api has."""
    config = env.client.config()
    config["data_converter"] = DataConverter.default
    return Client(**config)


async def _hold_slot(r: Rig, call: SeededCall) -> None:
    """The concurrency slot dispatch took for the call."""
    expires_ms = int((r.clock.now() + SLOT_TTL).timestamp() * 1000)
    await r.redis.zadd(billing_keys.concurrency_key(call.org_id), {str(call.call_id): expires_ms})


async def test_a_dialed_call_canceled_while_ringing(database: Database) -> None:
    async with rig(database, realtime=True) as r, workflow_env() as env, running_worker(env, r):
        call = await r.seed("queued")
        await _hold_slot(r, call)
        start = CallWorkflowInput(call_id=call.call_id, org_id=call.org_id)
        handle = await api_client(env).start_workflow(
            CALL_WORKFLOW_NAME, start.model_dump(mode="json"), id=start.workflow_id(), task_queue=TASK_QUEUE
        )
        async with asyncio.timeout(DIAL_WAIT_S):
            await r.dialed.wait()
        assert (await load_call(database, call)).status == "dialing"
        stored = await r.redis.get(call_context_key(call.call_id))
        assert stored is not None
        context = CallContext.model_validate_json(stored)
        assert (context.call_id, context.org_id) == (call.call_id, call.org_id)

        await handle.signal(CANCEL_SIGNAL)
        result = await env.client.get_workflow_handle(
            start.workflow_id(), result_type=CallWorkflowResult
        ).result()

        assert result.outcome == "canceled"
        row = await load_call(database, call)
        assert (row.status, row.end_reason, row.ended_by) == ("canceled", "canceled", "api")
        [dial] = r.dials()
        assert dial["connection_id"] == r.app_id
        assert len(r.commands("hangup")) == 1
        assert await r.limiter.active_calls(call.org_id) == 0
        child = await env.client.get_workflow_handle(postcall_workflow_id(call.call_id)).describe()
        assert (child.status, child.task_queue) == (WorkflowExecutionStatus.RUNNING, POSTCALL_QUEUE)
