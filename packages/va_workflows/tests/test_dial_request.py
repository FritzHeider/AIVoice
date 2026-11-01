"""The outbound ``DialRequest`` (specs/06 §6 step 5, Decision P05-T06b)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from resolved_configs import resolved_config
from telephony_rig import expected_dial_body

from va_common.ids import new_id
from va_common.stream_tokens import mint_stream_token, stream_token_expiry, verify_stream_token
from va_domain.calls import SendCallConfig
from va_telnyx.client_state import decode
from va_workflows.activities.dial_request import (
    DialSettings,
    amd_mode,
    build_dial_request,
    dial_command_id,
    stream_url,
    time_limit_secs,
)

NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
CALL_ID = new_id()
ORG_ID = new_id()
SECRET = secrets.token_bytes(32)
SETTINGS = DialSettings.for_env(
    "production",
    call_control_app_id="app-1",
    media_public_host="media.example.net",
    stream_secret=SECRET,
    allowed_countries=frozenset({"US"}),
)


def _config(name: str = "01_task_basic.json", **overrides: Any) -> SendCallConfig:
    raw = resolved_config(name)
    fields = SendCallConfig.model_fields
    return SendCallConfig.model_validate({**{k: v for k, v in raw.items() if k in fields}, **overrides})


def _body(config: SendCallConfig, token: str = "tok") -> dict[str, Any]:  # noqa: S107 - not a secret
    request = build_dial_request(
        CALL_ID,
        ORG_ID,
        config,
        to="+14155550100",
        from_="+14155550190",
        connection_id="app-1",
        timeout_secs=45,
        stream_token=token,
        settings=SETTINGS,
    )
    return request.body()


def test_the_body_of_a_recorded_task_call_has_every_field() -> None:
    body = _body(_config())

    client_state = body.pop("client_state")
    url = body.pop("stream_url")
    assert body == expected_dial_body(CALL_ID, connection_id="app-1", to="+14155550100", from_="+14155550190")
    state = decode(client_state)
    assert (state.call_id, state.leg, state.org_id) == (CALL_ID, "primary", ORG_ID)
    assert url == f"wss://media.example.net/ws/telnyx?call_id={CALL_ID}&token=tok"


def test_hd_audio_streams_l16_at_16_khz_and_no_recording_omits_the_record_fields() -> None:
    body = _body(_config("05_voice_audio.json"))

    assert (
        body["stream_codec"],
        body["stream_bidirectional_codec"],
        body["stream_bidirectional_sampling_rate"],
    ) == (
        "L16",
        "L16",
        16000,
    )
    assert not {"record", "record_channels", "record_format", "record_track"} & body.keys()
    assert body["time_limit_secs"] == 1830


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, "premium"),
        ({"ivr_mode": True}, "disabled"),
        ({"answered_by_enabled": False}, "disabled"),
        ({"voicemail": {"action": "ignore", "sensitive": False}}, "disabled"),
        ({"voicemail": {"action": "ignore", "sensitive": True}}, "premium"),
        ({"voicemail": {"action": "leave_message", "sensitive": False}}, "premium"),
    ],
)
def test_amd_mode(overrides: dict[str, Any], expected: str) -> None:
    config = _config(**overrides)

    assert amd_mode(config) == expected
    body = _body(config)
    assert body["answering_machine_detection"] == expected
    assert ("answering_machine_detection_config" in body) == (expected == "premium")


@pytest.mark.parametrize(
    ("minutes", "seconds"), [(0.5, 60), (5, 330), (1.01, 91), (239.5, 14_400), (240, 14_400)]
)
def test_time_limit_is_capped_at_four_hours(minutes: float, seconds: int) -> None:
    assert time_limit_secs(minutes) == seconds


@pytest.mark.parametrize(("va_env", "scheme"), [("local", "ws"), ("test", "ws"), ("staging", "wss")])
def test_the_stream_scheme_follows_va_env(va_env: str, scheme: str) -> None:
    settings = DialSettings.for_env(
        va_env,
        call_control_app_id="a",
        media_public_host="m.test",
        stream_secret=SECRET,
        allowed_countries=frozenset({"US"}),
    )

    assert stream_url(settings, CALL_ID, "t").startswith(f"{scheme}://m.test/ws/telnyx?")


def test_the_stream_token_verifies_for_the_call_until_its_expiry() -> None:
    config = _config()
    expires_at = stream_token_expiry(NOW, config.max_duration_min)
    body = _body(config, mint_stream_token(CALL_ID, expires_at, SECRET))

    query = parse_qs(urlsplit(body["stream_url"]).query)
    assert query["call_id"] == [str(CALL_ID)]
    claims = verify_stream_token(query["token"][0], secrets=(SECRET,), now=NOW, call_id=CALL_ID)
    assert claims.expires_at == NOW + timedelta(seconds=5 * 60 + 600)


def test_settings_hide_the_stream_secret_and_the_command_id_is_per_call() -> None:
    assert SECRET.hex() not in repr(SETTINGS)
    assert "stream_secret" not in repr(SETTINGS)
    assert dial_command_id(CALL_ID) == f"dial-{CALL_ID}"
