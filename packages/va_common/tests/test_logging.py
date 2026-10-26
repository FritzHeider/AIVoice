from __future__ import annotations

import io
import json
import logging
import socket
from collections.abc import Mapping
from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

import pytest
from opentelemetry import trace
from opentelemetry.trace import NonRecordingSpan, SpanContext

from va_common.logging import (
    MAX_DEPTH,
    RedactionPolicy,
    RedactProcessor,
    bind_call_context,
    bind_turn,
    bound_context,
    clear_context,
    component_levels,
    configure_logging,
    get_logger,
    otel_trace_processor,
    redact,
    transcripts_allowed,
)
from va_common.settings import BaseServiceSettings

PHONE = "+14155550123"
MASKED = "+1415***0123"
ORG = UUID("0192f0a0-0000-7000-8000-000000000001")
CALL = UUID("0192f0a0-0000-7000-8000-000000000002")


class ApiSettings(BaseServiceSettings):
    service_name: ClassVar[str] = "api"


def make_settings(**overrides: Any) -> ApiSettings:
    values: dict[str, Any] = {"_env_file": None, "log_format": "json", **overrides}
    return ApiSettings(**values)


def fail(exc: Exception) -> None:
    raise exc


class Capture:
    """Configure logging into a buffer and read back the rendered lines."""

    def __init__(self, settings: BaseServiceSettings, **kwargs: Any) -> None:
        self.stream = io.StringIO()
        kwargs.setdefault("environ", {})
        kwargs.setdefault("pod", "pod-1")
        configure_logging(settings, stream=self.stream, **kwargs)

    def text(self) -> str:
        return self.stream.getvalue()

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.text().splitlines() if line]

    def last(self) -> dict[str, Any]:
        lines = self.lines()
        assert lines, "no log output"
        return lines[-1]


def capture(**overrides: Any) -> Capture:
    return Capture(make_settings(**overrides))


# --- standard fields ------------------------------------------------------------------------------


def test_json_line_has_standard_fields() -> None:
    out = Capture(make_settings(va_env="staging"), version="1.2.3")
    get_logger("va_api.test").info("hello", extra_field=1)
    line = out.last()
    assert line["event"] == "hello"
    assert line["level"] == "info"
    assert line["logger"] == "va_api.test"
    assert (line["service"], line["env"], line["version"], line["pod"]) == (
        "api",
        "staging",
        "1.2.3",
        "pod-1",
    )
    assert line["extra_field"] == 1
    ts = datetime.fromisoformat(line["ts"])
    assert ts.utcoffset() is not None
    assert ts.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert "trace_id" not in line


def test_version_defaults_to_unknown() -> None:
    out = capture()
    get_logger().info("x")
    assert out.last()["version"] == "unknown"


def test_pod_from_pod_id_then_hostname() -> None:
    out = Capture(make_settings(), pod=None, environ={"POD_ID": "media-worker-7"})
    get_logger().info("x")
    assert out.last()["pod"] == "media-worker-7"
    out = Capture(make_settings(), pod=None, environ={})  # no POD_ID: hostname
    get_logger().info("x")
    assert out.last()["pod"] == socket.gethostname()


def test_pod_falls_back_to_process_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POD_ID", "from-os-environ")
    stream = io.StringIO()
    configure_logging(make_settings(), stream=stream)
    get_logger().info("x")
    assert json.loads(stream.getvalue())["pod"] == "from-os-environ"


def test_defaults_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging(make_settings(), pod="p", environ={})
    get_logger().info("to stdout")
    assert json.loads(capsys.readouterr().out)["event"] == "to stdout"


def test_initial_values_are_bound() -> None:
    out = capture()
    get_logger("x", component="stt").info("x")
    assert out.last()["component"] == "stt"


# --- context binding ------------------------------------------------------------------------------


def test_bind_call_context_and_turn() -> None:
    out = capture()
    log = get_logger()
    bind_call_context(org_id=ORG, call_id=CALL)
    log.info("a")
    first = out.last()
    assert (first["org_id"], first["call_id"]) == (str(ORG), str(CALL))
    assert "turn_index" not in first
    bind_turn(3)
    log.info("b")
    assert out.last()["turn_index"] == 3
    bind_call_context(org_id="org-x", call_id="call-y", turn_index=0)
    log.info("c")
    assert (out.last()["org_id"], out.last()["turn_index"]) == ("org-x", 0)
    clear_context()
    log.info("d")
    assert not {"org_id", "call_id", "turn_index"} & out.last().keys()


def test_bound_context_restores_on_exit() -> None:
    out = capture()
    log = get_logger()
    with bound_context(request_id="req-1"):
        log.info("inside")
    log.info("outside")
    inside, outside = out.lines()
    assert inside["request_id"] == "req-1"
    assert "request_id" not in outside


# --- phone masking --------------------------------------------------------------------------------


def test_phones_masked_everywhere() -> None:
    out = capture()
    get_logger().info(f"dialing {PHONE}", to=PHONE, meta={"numbers": [PHONE, ("x", PHONE)]})
    line = out.last()
    assert line["event"] == f"dialing {MASKED}"
    assert line["to"] == MASKED
    assert line["meta"] == {"numbers": [MASKED, ["x", MASKED]]}
    assert PHONE not in out.text()


def test_phones_unmasked_only_for_debug_in_local() -> None:
    out = capture(va_env="local", log_level="DEBUG")
    get_logger().debug("dial", to=PHONE)
    assert out.last()["to"] == PHONE


@pytest.mark.parametrize(("env", "level"), [("staging", "DEBUG"), ("local", "INFO"), ("production", "DEBUG")])
def test_phones_masked_outside_local_debug(env: str, level: str) -> None:
    out = capture(va_env=env, log_level=level)
    get_logger().warning("dial", to=PHONE)
    assert out.last()["to"] == MASKED


# --- secrets and raw media ------------------------------------------------------------------------


def test_secrets_redacted_and_counters_kept() -> None:
    out = capture()
    get_logger().info(
        "request",
        authorization="Bearer abc",
        api_key="sk_live",
        **{"xi-api-key": "el-key"},
        headers={"Authorization": "Bearer nested", "Accept": "json"},
        api_key_prefix="va_ab12",
        prompt_tokens=12,
    )
    line = out.last()
    assert line["authorization"] == "[REDACTED]"
    assert line["api_key"] == "[REDACTED]"
    assert line["xi-api-key"] == "[REDACTED]"
    assert line["headers"] == {"Authorization": "[REDACTED]", "Accept": "json"}
    assert (line["api_key_prefix"], line["prompt_tokens"]) == ("va_ab12", 12)
    for secret in ("abc", "sk_live", "el-key", "nested"):
        assert secret not in out.text()


def test_camel_case_secrets_redacted_in_nested_payload() -> None:
    out = capture()
    get_logger().info(
        "vendor_response",
        body={
            "accessToken": "at-123",
            "clientSecret": "cs-456",
            "apiKeyPrefix": "va_cd34",
            "promptTokens": 7,
        },
    )
    body = out.last()["body"]
    assert body == {
        "accessToken": "[REDACTED]",
        "clientSecret": "[REDACTED]",
        "apiKeyPrefix": "va_cd34",
        "promptTokens": 7,
    }
    for secret in ("at-123", "cs-456"):
        assert secret not in out.text()


def test_raw_media_dropped_and_bytes_summarised() -> None:
    out = capture()
    get_logger().info("frame", audio=b"\xff" * 160, payload="base64==", chunk=b"\x00" * 20, buf=bytearray(3))
    line = out.last()
    assert "audio" not in line
    assert "payload" not in line
    assert (line["chunk"], line["buf"]) == ("<20 bytes>", "<3 bytes>")


# --- transcripts ----------------------------------------------------------------------------------


def test_transcripts_kept_in_local_when_enabled() -> None:
    out = capture(va_env="local", log_transcripts=True)
    get_logger().info("turn", text="hello there", transcript="full")
    line = out.last()
    assert (line["text"], line["transcript"]) == ("hello there", "full")


def test_transcripts_dropped_when_disabled() -> None:
    out = capture(va_env="local", log_transcripts=False)
    get_logger().info("turn", text="hello", nested={"transcript": "x", "kept": 1})
    line = out.last()
    assert "text" not in line
    assert line["nested"] == {"kept": 1}


def test_transcripts_forced_off_in_production() -> None:
    settings = make_settings(va_env="production", log_transcripts=True)
    assert not transcripts_allowed(settings)
    out = Capture(settings)
    get_logger().info("turn", text="hello")
    assert "text" not in out.last()


def test_transcripts_dropped_for_hipaa_orgs() -> None:
    out = capture(va_env="local", log_transcripts=True)
    with bound_context(hipaa_mode=True):
        get_logger().info("turn", text="phi")
    assert "text" not in out.last()
    assert "phi" not in out.text()


def test_transcripts_allowed_matrix() -> None:
    assert transcripts_allowed(make_settings(va_env="staging", log_transcripts=True))
    assert not transcripts_allowed(make_settings(va_env="staging", log_transcripts=False))


# --- redact() as a pure function ------------------------------------------------------------------


def test_redact_does_not_mutate_input() -> None:
    data: dict[str, Any] = {"to": PHONE, "api_key": "k", "nested": {"text": "t"}, "audio": b"x"}
    snapshot = json.dumps(data, default=repr)
    result = redact(data, RedactionPolicy())
    assert json.dumps(data, default=repr) == snapshot
    assert result == {"to": MASKED, "api_key": "[REDACTED]", "nested": {}}


def test_redact_policy_switches() -> None:
    data = {"to": PHONE, "text": "t"}
    policy = RedactionPolicy(mask_phones=False, allow_transcripts=True)
    assert redact(data, policy) == data
    assert redact(data, policy, hipaa=True) == {"to": PHONE}


def test_redact_keeps_tuples_and_other_values() -> None:
    marker = object()
    result = redact({"t": (PHONE, 1), "n": None, "o": marker, "m": memoryview(b"ab")}, RedactionPolicy())
    assert result == {"t": (MASKED, 1), "n": None, "o": marker, "m": "<2 bytes>"}


def test_redact_depth_cap() -> None:
    deep: dict[str, Any] = {"leaf": PHONE}
    for _ in range(MAX_DEPTH + 2):
        deep = {"d": deep}
    result = redact(deep, RedactionPolicy())
    for _ in range(MAX_DEPTH - 1):
        result = result["d"]
    assert result == {"d": "[REDACTED]"}
    assert redact({"a": [[PHONE]]}, RedactionPolicy()) == {"a": [[MASKED]]}


def test_policy_from_settings() -> None:
    assert RedactionPolicy.from_settings(make_settings(va_env="local", log_level="DEBUG")) == RedactionPolicy(
        mask_phones=False, allow_transcripts=True
    )
    assert RedactionPolicy.from_settings(make_settings(va_env="production")) == RedactionPolicy(
        mask_phones=True, allow_transcripts=False
    )


def test_redact_processor_reads_hipaa_flag() -> None:
    processor = RedactProcessor(RedactionPolicy(allow_transcripts=True))
    assert processor(None, "info", {"text": "t"}) == {"text": "t"}
    assert processor(None, "info", {"text": "t", "hipaa_mode": True}) == {"hipaa_mode": True}


# --- OpenTelemetry --------------------------------------------------------------------------------


def test_trace_ids_from_current_span() -> None:
    out = capture()
    context = SpanContext(trace_id=0xABC, span_id=0x12, is_remote=False)
    with trace.use_span(NonRecordingSpan(context)):
        get_logger().info("traced")
    line = out.last()
    assert line["trace_id"] == f"{0xABC:032x}"
    assert line["span_id"] == f"{0x12:016x}"
    assert len(line["trace_id"]) == 32
    assert len(line["span_id"]) == 16


def test_otel_processor_without_span() -> None:
    assert otel_trace_processor(None, "info", {"event": "x"}) == {"event": "x"}


# --- levels ---------------------------------------------------------------------------------------


def test_component_levels_parsing() -> None:
    environ: Mapping[str, str] = {
        "LOG_LEVEL": "WARNING",
        "LOG_LEVEL_VA_MEDIA": "debug",
        "LOG_LEVEL_TELNYX": " Error ",
        "PATH": "/bin",
        "LOG_LEVEL_": "DEBUG",
    }
    assert component_levels(environ) == {"va_media": logging.DEBUG, "telnyx": logging.ERROR}


def test_component_levels_rejects_invalid_level() -> None:
    with pytest.raises(ValueError, match="LOG_LEVEL_TELNYX"):
        component_levels({"LOG_LEVEL_TELNYX": "LOUD"})


def test_configure_applies_component_levels() -> None:
    out = Capture(make_settings(log_level="INFO"), environ={"LOG_LEVEL_VA_MEDIA": "DEBUG"})
    get_logger("va_media").debug("media debug")
    get_logger("va_media.child").debug("child debug")
    get_logger("other").debug("filtered")
    logging.getLogger("va_media").debug("stdlib debug")
    events = [line["event"] for line in out.lines()]
    assert events == ["media debug", "child debug", "stdlib debug"]


def test_reconfigure_resets_component_levels() -> None:
    Capture(make_settings(), environ={"LOG_LEVEL_VA_MEDIA": "DEBUG"})
    assert logging.getLogger("va_media").level == logging.DEBUG
    Capture(make_settings(), environ={})
    assert logging.getLogger("va_media").level == logging.NOTSET


def test_root_level_filters() -> None:
    out = capture(log_level="WARNING")
    log = get_logger("va_api")
    log.info("dropped")
    log.warning("kept")
    assert [line["event"] for line in out.lines()] == ["kept"]


# --- stdlib integration, exceptions, idempotency --------------------------------------------------


def test_foreign_stdlib_logger_rendered_and_masked() -> None:
    out = capture()
    with bound_context(request_id="r-9"):
        logging.getLogger("thirdparty").warning("calling %s", PHONE)
    line = out.last()
    assert line["event"] == f"calling {MASKED}"
    assert (line["logger"], line["level"], line["service"], line["request_id"]) == (
        "thirdparty",
        "warning",
        "api",
        "r-9",
    )
    assert "_record" not in line
    assert "_from_structlog" not in line


def test_exception_rendered_in_json() -> None:
    out = capture()
    try:
        fail(RuntimeError(f"boom {PHONE}"))
    except RuntimeError:
        get_logger().exception("failed")
    line = out.last()
    assert line["level"] == "error"
    assert "RuntimeError: boom" in line["exception"]
    assert MASKED in line["exception"]
    assert PHONE not in out.text()


def test_foreign_exception_rendered_in_json() -> None:
    out = capture()
    try:
        fail(ValueError("bad"))
    except ValueError:
        logging.getLogger("thirdparty").exception("oops")
    assert "ValueError: bad" in out.last()["exception"]


def test_console_format_renders_plain_lines() -> None:
    settings = make_settings(log_format="console")
    out = Capture(settings)
    log = get_logger("va_api")
    log.info("console line", to=PHONE)
    try:
        fail(RuntimeError("console boom"))
    except RuntimeError:
        log.exception("failed")
    text = out.text()
    assert "console line" in text
    assert MASKED in text
    assert PHONE not in text
    assert "RuntimeError: console boom" in text
    with pytest.raises(json.JSONDecodeError):
        json.loads(text.splitlines()[0])


def test_non_json_values_serialised_leniently() -> None:
    out = capture()
    get_logger().info("odd", when=datetime(2026, 1, 2, 3, 4), ids={1: ORG}, obj=object)  # noqa: DTZ001
    line = out.last()
    assert line["when"] == "2026-01-02T03:04:00"
    assert line["ids"] == {"1": str(ORG)}
    assert line["obj"] == str(object)


def test_configure_twice_installs_one_handler() -> None:
    first = capture()
    second = capture()
    get_logger().info("once")
    assert first.text() == ""
    assert [line["event"] for line in second.lines()] == ["once"]
    assert logging.getLogger("uvicorn.access").disabled
