"""Worker container: refuses to start without dialing settings, wires the main queue without network I/O
(specs/01-architecture.md §1 Decision P05-T06d)."""

from __future__ import annotations

import pytest
from service_helpers import UNREACHABLE, track_closes
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from worker_helpers import dialing_settings

from va_worker import main as worker_main
from va_worker.activities import TELNYX_HTTP_TIMEOUT, telnyx_http
from va_worker.main import build_container
from va_workflows.registration import CALL_WORKFLOWS

MAIN_ACTIVITIES = {
    "prefetch_call_context",
    "dial",
    "hangup_call",
    "end_call",
    "watch_call_heartbeat",
    "call_presence",
}


def _forbid_opening(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    opened: list[str] = []

    def record(*_args: object, **_kwargs: object) -> None:
        opened.append("dependencies")
        msg = "nothing may be opened"
        raise AssertionError(msg)

    monkeypatch.setattr(worker_main, "open_dependencies", record)
    return opened


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"stream_token_secret": ""}, "STREAM_TOKEN_SECRET is not configured"),
        ({"telnyx_call_control_app_id": ""}, "TELNYX_CALL_CONTROL_APP_ID is not configured"),
        ({"local_master_key": ""}, "LOCAL_MASTER_KEY is not configured"),
    ],
)
async def test_missing_configuration_fails_before_anything_opens(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, str], message: str
) -> None:
    opened = _forbid_opening(monkeypatch)
    with pytest.raises(RuntimeError, match=message):
        async with build_container(dialing_settings(**UNREACHABLE, **overrides)):
            pytest.fail("the container must not be yielded")
    assert opened == []


@pytest.mark.parametrize("queue", ["va-main", "staging-va-main"])
async def test_the_main_queue_runs_call_workflow_with_its_activities(queue: str) -> None:
    settings = dialing_settings(**UNREACHABLE, temporal_task_queue=queue)
    async with build_container(settings) as container:
        [registration] = container.registrations
        names = {activity._Definition.must_from_callable(fn).name for fn in registration.activities}
        assert registration.task_queue == queue
        assert tuple(registration.workflows) == CALL_WORKFLOWS
        assert names == MAIN_ACTIVITIES
        assert len(registration.activities) == len(MAIN_ACTIVITIES)


async def test_container_wires_the_pydantic_client_and_readiness_checks() -> None:
    async with build_container(
        dialing_settings(**{**UNREACHABLE, "temporal_host": "temporal:7233"})
    ) as container:
        assert container.temporal.host == "temporal:7233"
        assert container.temporal.data_converter is pydantic_data_converter
        assert list(container.readiness_checks()) == ["postgres", "redis", "temporal"]


async def test_container_closes_what_it_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = track_closes(monkeypatch)
    async with build_container(dialing_settings(**UNREACHABLE)):
        assert closed == []
    assert sorted(closed) == ["engine", "engine", "engine", "http", "redis"]


async def test_the_telnyx_pool_uses_the_spec_timeouts() -> None:
    async with telnyx_http(dialing_settings()) as http:
        assert http.timeout == TELNYX_HTTP_TIMEOUT
        assert (http.timeout.connect, http.timeout.read, http.timeout.write, http.timeout.pool) == (
            2,
            8,
            5,
            2,
        )
