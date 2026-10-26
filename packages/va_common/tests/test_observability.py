from __future__ import annotations

import io
import json
from typing import Any

import pytest
import sentry_sdk
import structlog
from obs_helpers import make_settings
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sentry_sdk.integrations.asyncio import AsyncioIntegration
from sentry_sdk.integrations.httpx import HttpxIntegration

from va_common import observability
from va_common.logging import RedactionPolicy, get_logger
from va_common.metrics import Service
from va_common.observability import SENTRY_FLUSH_TIMEOUT_S, init, init_sentry, sentry_before_send

PHONE = "+14155550123"
MASKED = "+1415***0123"


def _dsn() -> str:
    return "https://" + "public" + "@example.invalid/1"


@pytest.fixture
def sentry_calls(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[Any]]:
    calls: dict[str, list[Any]] = {"init": [], "flush": []}
    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: calls["init"].append(kwargs))
    monkeypatch.setattr(sentry_sdk, "flush", lambda **kwargs: calls["flush"].append(kwargs))
    return calls


def _init(
    settings: Any, **kwargs: Any
) -> tuple[observability.Observability, InMemorySpanExporter, io.StringIO]:
    exporter, stream = InMemorySpanExporter(), io.StringIO()
    obs = init(
        "telephony_edge",
        settings,
        version="3.1.0",
        span_processor=SimpleSpanProcessor(exporter),
        set_global=False,
        environ={"POD_ID": "edge-0"},
        stream=stream,
        **kwargs,
    )
    return obs, exporter, stream


def test_init_wires_logging_tracing_metrics(sentry_calls: dict[str, list[Any]]) -> None:
    obs, exporter, stream = _init(make_settings(otel_traces_sampler_arg=1.0), service=Service.TELEPHONY_EDGE)
    get_logger("va_edge").info("ready")
    line = json.loads(stream.getvalue().splitlines()[-1])
    assert (line["service"], line["version"], line["pod"]) == ("telephony_edge", "3.1.0", "edge-0")
    with obs.tracer_provider.get_tracer("t").start_as_current_span("telnyx.webhook call.initiated"):
        pass
    assert [span.name for span in exporter.get_finished_spans()] == ["telnyx.webhook call.initiated"]
    assert obs.tracer_provider.resource.attributes["k8s.pod.name"] == "edge-0"
    assert obs.metrics is not None
    assert obs.metrics.service is Service.TELEPHONY_EDGE
    assert obs.sentry_enabled is False
    assert sentry_calls["init"] == []
    obs.shutdown()
    assert sentry_calls["flush"] == []


def test_init_without_service_has_no_metrics(sentry_calls: dict[str, list[Any]]) -> None:
    obs, _, _ = _init(make_settings())
    assert obs.metrics is None
    obs.shutdown()


def test_init_with_dsn_enables_sentry_and_flushes(sentry_calls: dict[str, list[Any]]) -> None:
    obs, _, _ = _init(make_settings(sentry_dsn=_dsn(), va_env="staging"))
    assert obs.sentry_enabled is True
    (kwargs,) = sentry_calls["init"]
    assert kwargs["dsn"] == _dsn()
    assert kwargs["environment"] == "staging"
    assert kwargs["release"] == "3.1.0"
    assert kwargs["traces_sample_rate"] == 0.0
    assert kwargs["send_default_pii"] is False
    assert [type(i) for i in kwargs["integrations"]] == [AsyncioIntegration, HttpxIntegration]
    assert callable(kwargs["before_send"])
    obs.shutdown()
    assert sentry_calls["flush"] == [{"timeout": SENTRY_FLUSH_TIMEOUT_S}]


def test_blank_dsn_does_not_initialise(sentry_calls: dict[str, list[Any]]) -> None:
    assert init_sentry(make_settings(sentry_dsn="   "), version="1") is False
    assert sentry_calls["init"] == []


def _hook() -> Any:
    return sentry_before_send(RedactionPolicy(mask_phones=True, allow_transcripts=False))


def test_before_send_redacts_sections_and_strips_user_pii() -> None:
    event: dict[str, Any] = {
        "message": "boom",
        "extra": {"api_key": "k-123", "to": PHONE, "transcript": "hello"},
        "contexts": {"call": {"from": PHONE}},
        "request": {"headers": {"authorization": "Bearer abc", "accept": "json"}, "data": {"password": "pw"}},
        "breadcrumbs": {"values": [{"message": f"dial {PHONE}", "data": {"token": "t", "n": 1}}, "odd"]},
        "user": {"id": "u1", "email": "a@example.test", "ip_address": "10.0.0.1"},
    }
    result = _hook()(event, {})
    assert result is not None
    assert result["extra"] == {"api_key": "[REDACTED]", "to": MASKED}
    assert result["contexts"] == {"call": {"from": MASKED}}
    assert result["request"]["headers"] == {"authorization": "[REDACTED]", "accept": "json"}
    assert result["request"]["data"] == {"password": "[REDACTED]"}
    crumb = result["breadcrumbs"]["values"][0]
    assert crumb == {"message": f"dial {MASKED}", "data": {"token": "[REDACTED]", "n": 1}}
    assert result["breadcrumbs"]["values"][1] == "odd"
    assert result["user"] == {"id": "u1"}


def test_before_send_accepts_list_breadcrumbs_and_missing_sections() -> None:
    event: dict[str, Any] = {"breadcrumbs": [{"message": PHONE}], "extra": "not-a-mapping"}
    result = _hook()(event, {})
    assert result is not None
    assert result["breadcrumbs"] == [{"message": MASKED}]
    assert result["extra"] == "not-a-mapping"
    assert "tags" not in result


def test_before_send_ignores_unexpected_breadcrumb_shape() -> None:
    result = _hook()({"breadcrumbs": {"values": "none"}}, {})
    assert result == {"breadcrumbs": {"values": "none"}}


def test_before_send_tags_bound_org_and_call() -> None:
    structlog.contextvars.bind_contextvars(org_id="org-1", call_id="call-9", turn_index=3)
    result = _hook()({"tags": {"call_id": "explicit"}}, {})
    assert result is not None
    assert result["tags"] == {"call_id": "explicit", "org_id": "org-1"}


def test_before_send_masks_message_logentry_and_exception_value() -> None:
    event: dict[str, Any] = {
        "message": f"dial {PHONE}",
        "logentry": {"message": "dial %s", "params": [PHONE], "formatted": f"dial {PHONE}"},
        "exception": {
            "values": [{"type": "ValueError", "module": "va.dial", "value": f"invalid number {PHONE}"}]
        },
    }
    result = _hook()(event, {})
    assert result is not None
    assert result["message"] == f"dial {MASKED}"
    assert result["logentry"] == {"message": "dial %s", "params": [MASKED], "formatted": f"dial {MASKED}"}
    assert result["exception"]["values"] == [
        {"type": "ValueError", "module": "va.dial", "value": f"invalid number {MASKED}"}
    ]


def test_before_send_redacts_exception_and_thread_frame_vars() -> None:
    def frame() -> dict[str, Any]:
        return {
            "filename": "va/dial.py",
            "function": "dial",
            "lineno": 42,
            "vars": {"api_key": "k-1", "to": PHONE, "transcript": "hello", "n": 3},
        }

    expected = {
        "filename": "va/dial.py",
        "function": "dial",
        "lineno": 42,
        "vars": {"api_key": "[REDACTED]", "to": MASKED, "n": 3},
    }
    event: dict[str, Any] = {
        "exception": {"values": [{"type": "KeyError", "stacktrace": {"frames": [frame()]}}]},
        "threads": {"values": [{"id": 1, "stacktrace": {"frames": [frame(), "odd"]}}]},
    }
    result = _hook()(event, {})
    assert result is not None
    assert result["exception"]["values"] == [{"type": "KeyError", "stacktrace": {"frames": [expected]}}]
    assert result["threads"]["values"] == [{"id": 1, "stacktrace": {"frames": [expected, "odd"]}}]


def test_before_send_tolerates_unexpected_stack_shapes() -> None:
    event: dict[str, Any] = {
        "message": {"not": "text"},
        "exception": {"values": ["odd", {"value": 7, "stacktrace": {"frames": "none"}}]},
        "threads": "none",
    }
    result = _hook()(event, {})
    assert result == {
        "message": {"not": "text"},
        "exception": {"values": ["odd", {"value": 7, "stacktrace": {"frames": "none"}}]},
        "threads": "none",
    }


def test_before_send_keeps_phones_when_policy_allows() -> None:
    hook: Any = sentry_before_send(RedactionPolicy(mask_phones=False, allow_transcripts=False))
    event: dict[str, Any] = {"message": PHONE, "exception": {"values": [{"value": PHONE}]}}
    assert hook(event, {}) == {"message": PHONE, "exception": {"values": [{"value": PHONE}]}}
