"""Worker settings (specs/02-repo-layout-and-conventions.md §6) and task queue names (specs/01 §1)."""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from pydantic import ValidationError
from service_helpers import settings_of
from worker_helpers import dialing_settings

from va_domain.post_call import postcall_task_queue
from va_worker.settings import SIM_TELNYX_API_KEY, WorkerSettings
from va_worker.task_queues import task_queue_names


def test_defaults_match_spec() -> None:
    settings = settings_of(WorkerSettings)
    assert settings.service_name == "worker"
    assert (settings.temporal_host, settings.temporal_namespace) == ("localhost:7233", "default")
    assert settings.temporal_task_queue == "va-main"
    assert settings.database_url == "postgresql+asyncpg://va:va@localhost:5432/va"
    assert settings.smtp_config().port == 1025
    assert settings.email_from == "VoiceAgents <no-reply@voiceagents.local>"
    assert settings.telnyx_api_base == "http://localhost:9100/v2"
    assert settings.telnyx_api_key.get_secret_value() == SIM_TELNYX_API_KEY == "sim-telnyx-key"
    assert (settings.telnyx_call_control_app_id, settings.media_public_host) == ("", "localhost:8200")
    assert settings.stream_token_secret.get_secret_value() == ""
    assert settings.allowed_destination_countries() >= {"US", "CA", "GB", "SG"}
    assert len(settings.allowed_destination_countries()) == 21


def test_task_queue_names_from_main_queue() -> None:
    assert task_queue_names("va-main") == ("va-main", "va-postcall", "va-ingest", "va-system")
    assert task_queue_names("staging-va-main")[1] == "staging-va-postcall"


@pytest.mark.parametrize("base", ["va-main", "staging-va-main"])
def test_the_postcall_child_queue_is_a_polled_queue(base: str) -> None:
    assert postcall_task_queue(base) == task_queue_names(base)[1]


@pytest.mark.parametrize("base", ["va", "va-postcall", "-main", ""])
def test_task_queue_names_require_main_suffix(base: str) -> None:
    with pytest.raises(ValueError, match="-main"):
        task_queue_names(base)


def _deployed(env: str, **overrides: Any) -> WorkerSettings:
    """``env`` settings that would otherwise construct (secure SMTP, AWS KMS in production)."""
    values: dict[str, Any] = {
        "va_env": env,
        "smtp_url": "smtps://mail.voiceagents.test:465",
        "kms_provider": "aws",
        "aws_kms_key_id": "alias/voiceagents",
        "stream_token_secret": secrets.token_hex(32),
        "telnyx_call_control_app_id": "app-live",
        "telnyx_api_key": f"KEY{secrets.token_hex(8)}",
        **overrides,
    }
    return settings_of(WorkerSettings, **values)


@pytest.mark.parametrize("env", ["staging", "production"])
def test_deployed_settings_with_everything_configured(env: str) -> None:
    assert _deployed(env).dial_settings().media_ws_scheme == "wss"


@pytest.mark.parametrize("env", ["staging", "production"])
@pytest.mark.parametrize(
    ("field", "value", "variable"),
    [
        ("stream_token_secret", "", "STREAM_TOKEN_SECRET"),
        ("telnyx_call_control_app_id", "  ", "TELNYX_CALL_CONTROL_APP_ID"),
        ("telnyx_api_key", "", "TELNYX_API_KEY"),
    ],
)
def test_deployed_settings_refuse_missing_dialing_values(
    env: str, field: str, value: str, variable: str
) -> None:
    with pytest.raises(ValidationError, match=f"{variable} must be configured when VA_ENV={env}"):
        _deployed(env, **{field: value})


@pytest.mark.parametrize("env", ["staging", "production"])
def test_deployed_settings_refuse_the_simulator_key(env: str) -> None:
    with pytest.raises(ValidationError, match="TELNYX_API_KEY must not be the simulator key") as caught:
        _deployed(env, telnyx_api_key=SIM_TELNYX_API_KEY)
    assert SIM_TELNYX_API_KEY not in str(caught.value)


def test_missing_values_are_listed_together() -> None:
    with pytest.raises(ValidationError, match="STREAM_TOKEN_SECRET, TELNYX_CALL_CONTROL_APP_ID must be"):
        _deployed("staging", stream_token_secret="", telnyx_call_control_app_id="")


def test_a_malformed_stream_secret_is_rejected_without_echo() -> None:
    bad = "not-hex-" + secrets.token_hex(8)
    with pytest.raises(ValidationError, match="STREAM_TOKEN_SECRET must be 64 hex") as caught:
        settings_of(WorkerSettings, stream_token_secret=bad)
    assert bad not in str(caught.value)


@pytest.mark.parametrize("env", ["local", "test"])
def test_dev_settings_dial_over_plain_websockets(env: str) -> None:
    secret = secrets.token_hex(32)
    dial = dialing_settings(
        va_env=env,
        stream_token_secret=secret,
        media_public_host="media.test:8200",
        international_allowed_countries="US, ca",
    ).dial_settings()
    assert (dial.call_control_app_id, dial.media_public_host, dial.media_ws_scheme) == (
        "app-test",
        "media.test:8200",
        "ws",
    )
    assert dial.stream_secret == bytes.fromhex(secret)
    assert dial.allowed_countries == frozenset({"US", "CA"})
    assert secret not in repr(dial)


def test_dial_settings_require_the_stream_secret() -> None:
    with pytest.raises(RuntimeError, match="STREAM_TOKEN_SECRET is not configured"):
        dialing_settings(stream_token_secret="").dial_settings()


def test_dial_settings_require_the_call_control_app() -> None:
    with pytest.raises(RuntimeError, match="TELNYX_CALL_CONTROL_APP_ID is not configured"):
        dialing_settings(telnyx_call_control_app_id="").dial_settings()
