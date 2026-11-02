"""Worker container: refuses to start without dialing settings, wires the main queue without network I/O
(specs/01-architecture.md §1 Decision P05-T06d)."""

from __future__ import annotations

import pytest
from service_helpers import UNREACHABLE, track_closes
from temporalio import activity
from temporalio.contrib.pydantic import pydantic_data_converter
from worker_helpers import dialing_settings

from va_common.http import http_client
from va_common.metrics import Service, build_metrics, new_registry
from va_telnyx.settings import TELNYX_HTTP_TIMEOUT, telnyx_http
from va_worker import main as worker_main
from va_worker.main import build_container
from va_workflows.registration import CALL_WORKFLOWS, POSTCALL_WORKFLOWS

MAIN_ACTIVITIES = {
    "prefetch_call_context",
    "dial",
    "hangup_call",
    "end_call",
    "watch_call_heartbeat",
    "call_presence",
    "schedule_retry_call",
}
POSTCALL_ACTIVITIES = [
    "finalize_call_record",
    "find_saved_recording",
    "report_recording_missing",
    "mark_postcall_step",
    "finalize_billing",
    "emit_completed",
    "store_recording",
]


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
        registration, postcall = container.registrations
        names = {activity._Definition.must_from_callable(fn).name for fn in registration.activities}
        assert registration.task_queue == queue
        assert tuple(registration.workflows) == CALL_WORKFLOWS
        assert names == MAIN_ACTIVITIES
        assert len(registration.activities) == len(MAIN_ACTIVITIES)
        assert postcall.task_queue == queue.removesuffix("-main") + "-postcall"
        assert tuple(postcall.workflows) == POSTCALL_WORKFLOWS
        postcall_names = [activity._Definition.must_from_callable(fn).name for fn in postcall.activities]
        assert postcall_names == POSTCALL_ACTIVITIES


async def test_post_call_steps_observe_into_the_given_registry() -> None:
    metrics = build_metrics(Service.WORKER, new_registry(process_collectors=False))
    async with build_container(dialing_settings(**UNREACHABLE), metrics) as container:
        post_call = container.activities.post_call
        assert post_call.has_storage
        assert post_call._step_ms is metrics.histogram("va_postcall_duration_ms")


async def test_recordings_go_to_the_configured_bucket_with_sse_per_kms_provider() -> None:
    settings = dialing_settings(**UNREACHABLE, s3_bucket_recordings="rec-bucket")
    async with build_container(settings) as container:
        recording = container.activities.recording
        assert recording is not None
        assert recording._bucket == "rec-bucket"
        assert recording._sse is None


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
    assert sorted(closed) == ["engine", "engine", "engine", "http", "http", "redis"]


async def test_the_telnyx_pool_uses_the_spec_timeouts() -> None:
    async with telnyx_http(dialing_settings()) as http:
        assert http.timeout == TELNYX_HTTP_TIMEOUT
        assert (http.timeout.connect, http.timeout.read, http.timeout.write, http.timeout.pool) == (
            2,
            8,
            5,
            2,
        )


async def test_the_prefetch_activity_prerenders_with_the_configured_tts_model() -> None:
    settings = dialing_settings(**UNREACHABLE, elevenlabs_tts_model="eleven_turbo_v2_5")
    async with build_container(settings) as container:
        prerender = container.activities.context.prerender
        assert prerender is not None
        assert prerender.model_id == "eleven_turbo_v2_5"


async def test_the_elevenlabs_client_uses_its_socket_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    timeouts: list[object] = []
    original = http_client

    def recording(settings: object, *, timeout: object, **kwargs: object) -> object:
        timeouts.append(timeout)
        return original(settings, timeout=timeout, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(worker_main, "http_client", recording)
    async with build_container(dialing_settings(**UNREACHABLE)):
        assert timeouts == [worker_main.ELEVENLABS_HTTP_TIMEOUT]
